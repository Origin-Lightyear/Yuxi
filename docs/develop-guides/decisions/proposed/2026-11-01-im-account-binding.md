# IM 账号绑定

状态：proposed
类型：feature
Owner：`backend/package/yuxi/services/im_binding_service.py`（绑定会话、平台身份解析、绑定解析）；`backend/server/routers/agent_invocation_channel_router.py`（IM 消息的身份解析接入点）；`web/src/components/ImAccountBindingComponent.vue`（个人设置页入口）

## 问题

IM 机器人当前用「机器人归属人」的 API Key 执行所有消息：同一机器人下任何飞书/钉钉用户都以归属人身份运行，线程、知识库、附件和历史全部共享。`allowed_users` 只能拦人，不能表达「这条消息属于哪个 Yuxi 用户」。员工与 Yuxi 账号是一对一关系，需要把飞书/钉钉身份稳定映射到自己的 Yuxi 账号，使 IM 消息按本人权限与线程执行。

## 候选方案

- **A（已选）复用机器人凭据做平台 OAuth**：绑定入口在 Yuxi 内，用该平台唯一启用的机器人凭据发起飞书/钉钉扫码授权，回调换取平台用户 ID 写入映射表。零新增配置、多机器人天然隔离；代价是所属应用要配好回调地址与授权范围，换应用后 open_id 变化需重新绑定。
- **B 平台级 OAuth 配置**：新增独立于机器人的飞书/钉钉授权应用配置，绑定与机器人解耦；代价是同一套密钥在两处维护，后台表单与加密存储再做一份。
- **C 把飞书/钉钉做成 SSO 登录方式**：扫码即登录并顺带建立映射；代价是登录链路、会话与用户开通策略同时改动，绑定语义与登录语义混在一起，风险最高。

选 A 的理由：现有 `im_bot_configs` 已按平台加密保存 `app_id/app_secret`（飞书）与 `client_id/client_secret`（钉钉），且已有一个 OAuth 回调式流程（`/auth/oidc/*` 的 state + 换 code + 前端回调页）可作实现模板；当前每个平台只有一个机器人在跑，A 的代价最低。

## 决策

### 实现方案

**持久化**（新增两张表，随现有 `storage_migrations` 方式落地）：

- `im_account_bindings(id, user_id, platform, platform_user_id, bot_id, created_at, updated_at)`，`unique(platform, platform_user_id)` 与 `unique(user_id, platform)`：一个 Yuxi 用户在同一平台只保留一条绑定，一个平台身份不能被两个用户占用。
- `im_binding_sessions(id, user_id, platform, bot_id, state_hash, status, expires_at, used_at)`：绑定二维码背后的**一次性** state，只存哈希，短有效期（10 分钟）。
- `im_bot_configs.require_binding`（布尔，默认 `false`）：过渡期开关。关闭时未绑定身份回退到机器人归属人并附提示，打开后未绑定身份直接拦截。

**绑定链路**（入口在 Yuxi 个人设置）：

1. `POST /api/user/im-bindings/{platform}/sessions`：取该平台唯一启用的机器人凭据，生成 state，返回 `{bind_url, expires_at}`；前端用 `bind_url` 渲染二维码（前端库生成，后端不存图片）。
2. 用户用飞书/钉钉扫码授权，平台回调 `GET /api/im-bindings/callback/{platform}?code=&state=`（公开路由，靠 state 鉴权）：校验 state 未过期未使用 → 换 user access token → 取平台身份（飞书 `open_id`；钉钉 `unionId` 经 `topapi/v2/user/getbyunionid` 换成 `userid`）→ 写绑定，并返回一页最小 HTML 结果页（成功/失败文案），供手机端直接查看。
3. `GET /api/user/im-bindings` 列出本人绑定；`DELETE /api/user/im-bindings/{platform}` 解绑。

**消息链路**：`agent_invocation_channel_router` 保留「API Key = 机器人归属人」的传输身份，用它校验调用方确实是该 `account_id` 机器人（`bot.owner_uid == current_user.uid`，否则 403）；随后按 `(bot.platform, sender_id)` 查绑定，得到真实用户并以该用户身份提交 Run（线程、权限、模型配置、附件全按该用户走）。同一解析对 `/resume` 与 slash command 生效，避免两个入口身份不一致。未绑定时按机器人的 `require_binding` 分流：开关打开返回 `409 im_account_not_bound`，网关按 `run_active` 同样方式回一条「请先在 Yuxi 个人设置里绑定飞书/钉钉账号」；开关关闭（过渡期）仍以机器人归属人身份执行，响应里带 `identity_source=owner_fallback`，网关在正文后附一行「你还没绑定 Yuxi 账号，当前按机器人归属人身份执行」的提示。

**前端**：`SettingsModal.vue` 新增「IM 账号」tab（登录用户可见），展示飞书/钉钉绑定状态、绑定按钮、二维码弹窗与解绑；二维码用 `qrcode` 库在前端渲染，扫完轮询到成功自动刷新。

### 边界

- `allowed_users` 白名单继续作为入口门禁，与绑定相互独立。
- 一个平台同时启用多个机器人时，绑定会话直接报错要求管理员收敛，不静默挑选凭据。
- 回调地址取自创建会话时的请求 `base_url`，要求该域名在手机侧可达且已在平台后台登记。
- 绑定改变 uid 后 `channel_*` 线程 ID 随之变化，绑定前的 IM 会话历史不会续接。
- 钉钉授权拿到的 `unionId` 需换成消息侧使用的 `userid`（`topapi/v2/user/getbyunionid`）；该调用失败即整次绑定失败并回明确提示，不做降级匹配。
- 上线顺序：`require_binding` 默认关闭，先上线绑定能力与过渡期提示；等使用该机器人的同事完成绑定后，再由管理员逐个机器人打开开关，硬切后未绑定身份被拦截。

## 验收主张

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| `require_binding` 打开时，未绑定身份得到「请先绑定」提示且不产生 Run | 仍回落到机器人归属人身份，越权执行 | `agent_invocation_channel_router` 身份解析 | 真实 HTTP integration（channel 入口 + 未绑定 sender_id + 开关打开） | 移除开关判断后用例应产生 Run 并变红 | Not run |
| `require_binding` 关闭时，未绑定身份仍可执行且响应标记 `owner_fallback`、网关附带提示 | 过渡期静默以归属人身份执行，用户不知情 | 同上 + `runner.py` | integration 断言 `identity_source`，网关 unit 断言提示文案 | 去掉 `identity_source` 后用例变红 | Not run |
| 绑定后 IM 消息以绑定用户身份执行，线程与权限随该用户 | 仍用归属人身份，或线程串号 | `im_binding_service` + channel 入口 | E2E：两条不同 sender_id 的消息落在两个 uid 的线程，另一用户访问被拒 | 绑定解析改回归属人后用例变红 | Not run |
| 同一平台身份不能被两个用户绑定，同一用户同平台只有一条绑定 | 并发绑定产生双写或两条记录 | `unique(platform, platform_user_id)` / `unique(user_id, platform)` | 真实 PostgreSQL integration 并发插入 | 去掉唯一约束后用例应失败 | Not run |
| 过期或已使用的 state 无法完成绑定 | 重放 state 越权绑定 | `im_binding_sessions` 状态机 | integration：过期、重复使用各一例 | 去掉过期校验后用例变红 | Not run |
| 绑定/解绑、二维码生成在个人设置页可用且解绑后消息回到未绑定提示 | 前端显示已绑定但后端无记录 | `ImAccountBindingComponent` + 绑定接口 | 浏览器手工验证 + 接口断言的 unit | 解绑不删记录时用例变红 | Not run |

## 风险与未验证

- 飞书/钉钉应用的授权范围与回调地址需要在开放平台配置（飞书自建应用需开通获取用户 ID 权限；钉钉扫码登录需登记回调域名）。未配置前该平台绑定不可用，属部署前置条件。
- 钉钉 `unionId → userid` 依赖通讯录权限；若应用无该权限，绑定无法得到消息侧 `sender_staff_id`，需回到开放平台补权限。
- 绑定后历史线程不续接，属已知取舍；如需迁移旧会话需单独立项。
- 真实平台扫码链路需在有测试应用的钉钉/飞书租户中验证；无凭据环境下记为 `Not run`。
