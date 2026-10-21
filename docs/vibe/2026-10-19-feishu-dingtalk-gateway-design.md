# 飞书 / 钉钉机器人接入设计

日期：2026-10-19

## 背景

Linko AI 现有的纯文本 Channel 入口（`POST /api/agent-invocation/channel/messages`）目前只服务 `yuxi chat` CLI，尚未有 IM 适配层。用户希望参照 Hermes Agent 的「网关 + 平台适配器」思路，把飞书、钉钉机器人接入平台，让用户能在 IM 中调用平台内 Agent。

本次接入复用现有 Channel 入口与 Agent Run 链路，不新增 Agent 执行能力；差异只在平台协议适配（长连接收发、消息渲染、附件投递、交互卡片审批）与机器人配置管理。

## 目标

1. 新增一个独立常驻的 gateway 服务，以 WebSocket 长连接接入飞书与钉钉，无需公网 IP、域名或 webhook 端点。
2. 单聊为主（回复每条消息），群聊支持 @ 调用（仅被 @ 时响应），群内按用户隔离会话。
3. 正文按 markdown 渲染回发；Agent 交付物中的 Excel 作为文件附件发送。
4. ~~Agent 触发工具审批中断时，发送交互卡片，支持批准 / 拒绝。~~（2026-10-21 变更：IM 会话不再提供审批交互，见文末）
5. 在 Yuxi 后台提供机器人配置管理界面（凭证、目标 Agent、用户白名单、群策略）。
6. 断线可自动重连；单个机器人故障不影响其他机器人。

## 非目标

- 不改动 Agent Run 核心、agent-call 与 agent-eval 入口。
- 首版不支持入站文件 / 图片 / 语音，只接受纯文本。
- 不支持运行时切换 Agent，每个机器人固定一个 `agent_slug`。
- 不支持 webhook 连接模式，只做 WebSocket 长连接。
- 不做按 IM 用户映射到不同 Yuxi 账号：一个机器人对应一个 Yuxi 账号（API Key），IM 用户靠白名单限制。
- 不做多租户 SaaS 级机器人隔离，首版为平台级配置。

## 设计

### 1. 总体结构

- 新增独立容器 `gateway`（docker-compose 中与 `api` / `worker` 并列），常驻进程。
- gateway 只负责平台协议：长连接接入、事件归一化、消息回发、卡片回调。
- gateway 与 Yuxi 的交互全部走 HTTP API（API Key 认证），不直连数据库 / Redis。
- 机器人配置由 Yuxi 后台管理，gateway 通过内部接口拉取。

数据流：

```
IM 用户 → 平台 WS → gateway adapter → 归一化消息
       → POST /api/agent-invocation/channel/messages（API Key）
       → 订阅 GET /api/agent/runs/{run_id}/events
       → 正文 markdown + 下载 artifacts
       → 平台回发（消息 / 附件 / 审批卡片）
```

### 2. 机器人配置数据模型（后台管理）

新增表 `im_bot_config`，字段：

| 字段 | 说明 |
| --- | --- |
| `id` | 主键 |
| `platform` | `feishu` / `dingtalk` |
| `name` | 机器人名称 |
| `enabled` | 是否启用 |
| `credentials` | 平台凭证密文：飞书 `{app_id, app_secret, domain}`；钉钉 `{client_id, client_secret}` |
| `owner_uid` | 机器人归属用户；其身份用于调用 Yuxi API |
| `api_key_cipher` | 后端为该机器人自动创建的 API Key 密文 |
| `agent_slug` | 目标 Agent |
| `allowed_users` | 平台用户 ID 白名单；留空表示允许所有人 |
| `require_mention` | 群聊是否必须 @（默认 `true`） |
| `group_sessions_per_user` | 群内是否按用户隔离会话（默认 `true`） |
| `created_at` / `updated_at` | 审计字段 |

凭证需要可逆加密（gateway 要解出明文使用），与用户 API Key 的哈希存储不同，属于独立机密；加密密钥走现有服务端密钥配置。

API Key 由后台在保存机器人时**自动创建并绑定归属用户**，管理员只在表单里选择归属用户，不接触密钥。后端需保留该 Key 明文密文供 gateway 使用（存入 `api_key_cipher`）；同时在归属用户的 API Key 列表中可见、可吊销。更换归属用户时重建 API Key。

### 3. gateway 运行时

- 一个进程加载所有 `enabled` 机器人，每个机器人一个 adapter 实例与独立长连接。
- 飞书：使用官方 `lark-oapi` 的 WebSocket 客户端，跑在后台线程，事件 dispatch 到主 asyncio loop，SDK 负责心跳与重连。
- 钉钉：使用官方 `dingtalk-stream` SDK 的 Stream Mode（WebSocket）。
- 配置热更新：周期轮询后台配置接口，新增 / 删除 / 修改时重建对应 adapter，凭证变更时重连。
- 可靠性：断线指数退避重连；入站消息按平台 `message_id` 去重；同一会话串行处理。
- gateway 启动凭证：通过环境变量注入 Yuxi 内部地址与 gateway 服务令牌，用于拉取机器人配置；每个机器人的 API Key 用于后续 Channel / SSE / 附件调用。

### 4. 入站流程

1. 长连接收到事件后过滤：私聊全收；群聊仅在 @ 机器人且 `require_mention` 命中时处理。
2. 白名单校验：`allowed_users` 非空时发送者平台 ID 必须命中，留空表示允许所有人。
3. 去重：按平台 `message_id` 去重。
4. 解析会话键：私聊用平台 `chat_id`；群聊用 `chat_id + sender_id`（按用户隔离）。
5. 提交运行：`POST /api/agent-invocation/channel/messages`，携带 `channel`、`account_id`（机器人 ID）、`chat_id`（会话键）、`sender_id`、`message_id`、`request_id`、`agent_slug`、`message.text`。
6. 使用响应中的 `run_id` / `stream_url` 订阅 SSE；若处于排队，先订阅 request 事件直到派发。
7. 终态后取正文与 artifacts，回发消息与附件。

gateway 在本地按会话串行：同一会话上一条 run 未到终态前不提交下一条，因此不会与后端队列的 `steer` 语义冲突，沿用 Channel 默认 `queue_policy`。

Thread 映射由后端 `hash_id(uid:channel:account_id:chat_id)` 决定，gateway 无需维护本地会话表。

### 5. 出站

- 飞书：正文用 post 消息（内嵌 `md` 标签）以获得表格 / 代码块渲染；被平台拒绝时降级为纯文本。
- 钉钉：正文用 markdown 消息；表格渲染较弱，必要时降级。
- Excel 交付物：从线程 state 的 `artifacts` 取路径，经 `GET /api/chat/thread/{thread_id}/artifacts/{path}` 下载，再上传到平台作为文件附件。
- 发送目标：飞书用 `chat_id` 主动发送；钉钉优先用 `robotCode` 主动发送，session webhook 作为回退（长 run 后 session webhook 可能过期）。

### 6. 工具审批

- gateway 通过 SSE 的 interrupted 状态（`human_approval_required`）识别待审批运行。
- 发送交互卡片，按钮 value 携带 `{thread_id, run_id, decision}`。
- 飞书订阅 `card.action.trigger`（长连接内处理）；钉钉使用 AI 卡片 / 回调。
- 卡片回调 → gateway 提交审批决定：使用**新的 per-decision `request_id`**，重复点击按 decision 幂等去重。
- 提交后 gateway 订阅**恢复 run** 的 SSE，把最终结果发回 IM。
- 仅触发该 run 的**原发送者**可点击批准 / 拒绝，其他成员的点击忽略。
- 后端改动：现有 Channel 入口的审批分支只支持 `approve`，需要支持 `reject`（Agent Run 的 resume 已支持 `approve` / `reject` 两种 decision），在不破坏现有 `/approve` 语义的前提下补充拒绝通道。
- `ask_user_question_required` 等非审批中断回文本"暂不支持"，与现有 Channel 行为一致。

### 7. 后台管理与接口

- 新增 router `im_bot_router`，前缀 `/api/system/im-bots`，提供列表、创建、更新、删除、启停、测试连接。平台管理员可访问。
- 新增 service、repository 与 `im_bot_config` model，沿用现有分层。
- 保存机器人时，后台按 `owner_uid` 自动创建并绑定 API Key，明文密文供 gateway 使用。
- gateway 拉取配置走内部接口（服务令牌认证），返回解密后的凭证与 API Key。
- 前端：设置页新增「IM 机器人」Tab，列表 + 表单（平台、凭证、归属用户、目标 Agent、白名单、群策略），白名单留空时提示将允许所有人。

### 8. 部署

- 新增 compose 服务 `gateway`，与 `api` / `worker` 同镜像或独立构建，支持热重载。
- 依赖：`lark-oapi`、`websockets`、`dingtalk-stream`、`httpx`。
- 环境变量：Yuxi 内部地址、gateway 服务令牌、轮询间隔等。

## 错误处理

- 长连接断开：指数退避重连，日志记录。
- 凭证无效 / 应用停用：该 adapter 标记不可用并记录明确错误，不影响其他机器人。
- 白名单外用户：静默忽略。
- 钉钉 session webhook 过期：记录日志并提示只能回复最近消息。
- Agent 报错 / 中断（非审批）：回发文本错误提示。
- API Key 失效：该机器人不可用并记录。
- 附件下载或上传失败：正文照常发送，附件失败记录日志并在消息中提示。

## 验收标准

- [ ] 飞书单聊文本问答收发正常，markdown 正确渲染。
- [ ] 钉钉单聊文本问答收发正常。
- [ ] 群聊仅在被 @ 时响应，群内用户会话相互隔离。
- [ ] Agent 交付物中的 Excel 作为附件发送成功。
- [ ] Agent 触发工具审批时发送交互卡片，批准与拒绝均生效。
- [ ] 后台可增删改查机器人配置并启停，保存时按归属用户自动创建 API Key。
- [ ] 长连接断开后可自动重连，多机器人互不影响。
- [ ] `allowed_users` 非空时白名单外用户无法调用；留空时允许所有人。

## 测试计划

- 单元测试：事件归一化、白名单与 @ 判定、消息去重、会话键计算、markdown 降级、卡片 value 解析、凭证加解密。
- 集成测试：后台 CRUD 接口、gateway 与 Channel 入口的交互（mock 平台 SDK）。
- 手工联调：使用真实飞书 / 钉钉应用验证单聊、群 @、附件与审批主路径。

## Checklist

- [ ] 新增 `im_bot_config` 数据模型、repository 与 service。
- [ ] 新增后台 CRUD、归属用户 API Key 自动创建与内部配置拉取接口。
- [ ] 新增前端「IM 机器人」配置页面。
- [ ] 补齐 Channel 入口的审批拒绝通道。
- [ ] 实现 gateway 服务与飞书 adapter。
- [ ] 实现钉钉 adapter。
- [ ] 实现正文 markdown 回发与附件投递。
- [ ] 实现交互卡片审批闭环。
- [ ] 接入 docker-compose，配置依赖与环境变量。
- [ ] 更新 changelog 与相关文档。
- [ ] 在 Docker 环境完成单元 / 集成测试与手工联调。

## 变更记录

- 2026-10-21：去掉 IM 侧工具审批。钉钉的卡片平台模板按钮与飞书卡片都依赖平台侧额外配置，
  实际联调中钉钉 SDK 内置按钮卡片无法回传请求，审批卡片点不动、会话会停在等待审批。
  改为网关提交 run 时固定 `tool_approval_mode=always_trust`，敏感工具直接执行；
  运行若仍进入审批中断，网关只回一条提示。相应删除了网关的卡片、审批回调与 `/approve`、`/reject` 提交逻辑。
