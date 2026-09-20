# Agent Client 数据与内部接口文档

> 版本：v1  
> 更新时间：2026-08-20  
> 服务：SaaS Tenant（默认本地地址 `http://localhost:8002`）

## 1. 接入约定

### 1.1 响应结构

除员工权限快照接口因兼容 MCP 数据结构直接返回对象外，其余接口统一返回：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {}
}
```

所有业务成功和失败均返回 HTTP 200。客户端必须判断响应体 `code`，不能只判断 HTTP 状态。

### 1.2 员工上下文

除 `/api/v1/agent/inner/**` 外，所有目录、会话、消息、任务和时区接口必须携带：

```http
X-Tenant-Id: 100
X-Employee-Id: 200
```

服务端会校验该租户下员工存在且启用。员工停用后所有 Agent 数据接口均不可读写，重新启用后原数据恢复可访问。

> 安全限制：本期没有员工 token，也没有内部接口签名。上述请求头不能防止身份伪造，`/inner/**` 也仅依赖受控网络隔离。本版本不应直接暴露到不可信公网。

### 1.3 时间与删除

- 所有 `createdAt`、`updatedAt`、`lastMessageAt` 均为 UTC ISO 8601，例如 `2026-08-20T03:15:30.123Z`。
- Directory、Conversation 和 Schedule 删除均为数据库硬删除。
- 删除 Conversation 时，其全部 Message 在同一事务中硬删除。
- 删除非默认 Directory 时，其中的 Conversation 会先迁移到默认目录。
- 服务端不负责通知其他设备删除本地缓存；多端本地数据对账由 Agent Client 负责。

### 1.4 版本控制

Directory、Conversation 和 Schedule 创建时 `version=1`。修改成功后版本加一。

- 修改请求在 JSON 中携带 `version`。
- 删除请求在查询参数中携带 `version`。
- 版本落后时返回 `code=20308`，客户端应重新拉取最新数据后再操作。
- 每次追加 Message 也会推进 Conversation 版本；追加响应会返回最新 `conversationVersion`。

### 1.5 列表策略

本期不分页：

- 目录树一次返回全部目录和会话摘要。
- 打开会话时一次返回全部消息。
- Schedule 列表一次返回全部任务。

客户端不得为这些接口传入分页参数。

## 2. 员工登录与内部接口

内部接口统一前缀：

```text
/api/v1/agent/inner
```

旧 `/tenant/**` 和 `/inner/system-settings/**` 路径已删除。

### 2.1 员工登录

```http
POST /api/v1/agent/inner/auth
Content-Type: application/json
```

```json
{
  "mobile": "13800138000",
  "password": "password123"
}
```

同一手机号和密码可能匹配多个租户。每个租户条目会签发与该租户和员工绑定的 `agentSessionToken`；员工必须在 Agent Client 中选择一个租户。上次选择由 Agent Client 本地保存和提示。

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "mobile": "13800138000",
    "tenants": [
      {
        "tenantId": 100,
        "tenantName": "示例企业",
        "employeeId": 200,
        "employeeCode": "E001",
        "employeeName": "张三",
        "departmentName": "销售部",
        "enabled": 1,
        "llmKey": "<employee-llm-key>",
        "llmUrl": "https://new-api.example.com",
        "agentSessionToken": "<agent-session-token>",
        "agentSessionExpiresAt": "2026-08-20T15:00:00Z"
      }
    ]
  }
}
```

`AgentSession` 默认有效 12 小时。Agent Client 自身登录令牌的有效期不得超过 `agentSessionExpiresAt`；Session 缺失或过期时应清除本地登录态并要求员工重新登录。

### 2.2 员工权限快照

```http
GET /api/v1/agent/inner/employee-permission?tenantId=100&employeeId=200
```

该接口直接返回 MCP 使用的 `EmployeeAuth` 结构，不使用 `{code,msg,data}` 包装。

### 2.3 修改员工密码

```http
POST /api/v1/agent/inner/employee/change-password
Content-Type: application/json
```

```json
{
  "tenantId": 100,
  "employeeId": 200,
  "oldPwd": "oldPassword1",
  "newPwd": "newPassword1",
  "repeatPwd": "newPassword1"
}
```

### 2.4 读取员工 LLM 配置

```http
POST /api/v1/agent/inner/employee-llm-config
Content-Type: application/json
```

```json
{
  "tenantId": 100,
  "employeeId": 200
}
```

### 2.5 上报权限审计

```http
POST /api/v1/agent/inner/permission-audit
Content-Type: application/json
```

请求必须包含 `tenantId` 和 `employeeId`，其他字段沿用 MCP 审计载荷。

### 2.6 租户系统健康与接入地址

```http
GET /api/v1/agent/inner/system-settings/tenants/{tenantId}/health
GET /api/v1/agent/inner/system-settings/tenants/{tenantId}/endpoints
```

这两个接口主要供 Platform 和 MCP 服务调用，Agent Client 通常不需要直接调用。

## 3. 目录接口

目录仅支持单层结构，不存在子目录。每个员工首次使用时会自动创建：

```text
默认目录
```

默认目录固定排在第一位，不能改名或删除。

### 3.1 查询完整目录树

```http
GET /api/v1/agent/directories/tree
X-Tenant-Id: 100
X-Employee-Id: 200
```

```json
{
  "code": 0,
  "msg": "ok",
  "data": [
    {
      "id": 1,
      "name": "默认目录",
      "defaultDirectory": true,
      "sortOrder": 0,
      "version": 1,
      "createdAt": "2026-08-20T03:00:00Z",
      "updatedAt": "2026-08-20T03:00:00Z",
      "conversations": [
        {
          "id": 10,
          "directoryId": 1,
          "title": "采购订单查询",
          "messageCount": 2,
          "lastMessageAt": "2026-08-20T03:05:00Z",
          "version": 3,
          "createdAt": "2026-08-20T03:01:00Z",
          "updatedAt": "2026-08-20T03:05:00Z"
        }
      ]
    }
  ]
}
```

目录按 `sortOrder ASC, id ASC` 返回；目录内会话按 `updatedAt DESC, id DESC` 返回。

### 3.2 创建目录

```http
POST /api/v1/agent/directories
```

```json
{
  "name": "采购"
}
```

同一员工下目录名称不能重复，名称最多 100 个字符。

### 3.3 重命名目录

```http
PATCH /api/v1/agent/directories/{directoryId}
```

```json
{
  "name": "供应链",
  "version": 1
}
```

### 3.4 批量调整目录顺序

```http
PATCH /api/v1/agent/directories/order
```

```json
{
  "items": [
    {"id": 2, "version": 1, "sortOrder": 1},
    {"id": 3, "version": 4, "sortOrder": 2}
  ]
}
```

`id` 和正整数 `sortOrder` 在一次请求中均不能重复。任一目录版本冲突时整批回滚。

### 3.5 删除目录

```http
DELETE /api/v1/agent/directories/{directoryId}?version=2
```

目录内会话会迁移到默认目录并推进各自 Conversation 版本，然后目录被硬删除。

## 4. 会话接口

### 4.1 创建会话

```http
POST /api/v1/agent/conversations
```

```json
{
  "directoryId": 2,
  "title": "采购订单查询"
}
```

- `directoryId` 为空时使用默认目录。
- `title` 为空或仅包含空格时保存为“新会话”。

### 4.2 查询会话详情

```http
GET /api/v1/agent/conversations/{conversationId}
```

会话详情不内嵌消息，字段与目录树中的会话摘要一致。

### 4.3 修改标题或移动目录

```http
PATCH /api/v1/agent/conversations/{conversationId}
```

```json
{
  "title": "八月采购订单",
  "directoryId": 3,
  "version": 5
}
```

`title` 和 `directoryId` 至少提供一个；未提供字段保持原值。

### 4.4 删除会话

```http
DELETE /api/v1/agent/conversations/{conversationId}?version=6
```

会话及其全部消息在同一事务内硬删除，不能恢复。

## 5. 消息接口

只支持以下枚举：

```text
role: USER | ASSISTANT
messageType: TEXT | MULTIMODAL
```

### 5.1 查询全部消息

```http
GET /api/v1/agent/conversations/{conversationId}/messages
```

一次返回完整历史，并按 `sequenceNo ASC` 排列。

### 5.2 新增 USER 文本消息

```http
POST /api/v1/agent/conversations/{conversationId}/messages
```

```json
{
  "clientMessageId": "550e8400-e29b-41d4-a716-446655440000",
  "role": "USER",
  "messageType": "TEXT",
  "content": "帮我分析这份合同",
  "contentJson": null,
  "replyToMessageId": null,
  "consumedScore": 0
}
```

### 5.3 新增 ASSISTANT 消息并记录整轮积分

```json
{
  "clientMessageId": "550e8400-e29b-41d4-a716-446655440001",
  "role": "ASSISTANT",
  "messageType": "TEXT",
  "content": "这份合同主要包含以下条款……",
  "contentJson": null,
  "replyToMessageId": 101,
  "consumedScore": 12
}
```

`consumedScore` 是本轮输入与输出合计积分：

- USER 必须为 `0`。
- ASSISTANT 记录整轮合计值，不拆分输入和输出。
- 该值只用于历史记录，不在 Tenant 服务重复扣费。
- 同一 USER 可对应多条 ASSISTANT，用于重新生成；每次生成分别记录积分。

追加响应：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "message": {
      "id": 102,
      "clientMessageId": "550e8400-e29b-41d4-a716-446655440001",
      "replyToMessageId": 101,
      "sequenceNo": 2,
      "role": "ASSISTANT",
      "messageType": "TEXT",
      "content": "这份合同主要包含以下条款……",
      "contentJson": null,
      "consumedScore": 12,
      "createdAt": "2026-08-20T03:05:00Z"
    },
    "conversationVersion": 3
  }
}
```

### 5.4 MULTIMODAL 消息

`TEXT` 必须只提供 `content`；`MULTIMODAL` 必须只提供 `contentJson`。

```json
{
  "clientMessageId": "550e8400-e29b-41d4-a716-446655440002",
  "role": "USER",
  "messageType": "MULTIMODAL",
  "content": null,
  "contentJson": {
    "parts": [
      {"type": "text", "text": "分析这个采购单"},
      {
        "type": "file",
        "fileId": "file_123",
        "fileName": "采购单.pdf",
        "mimeType": "application/pdf",
        "size": 102400
      }
    ]
  },
  "replyToMessageId": null,
  "consumedScore": 0
}
```

`parts[].type` 只支持 `text`、`file`、`image`。文件和图片必须提供 `fileId`，禁止 Base64、Data URI 或文件二进制内容。

### 5.5 消息幂等

`clientMessageId` 必须是 UUID，并在同一员工、同一会话内唯一：

- 相同 UUID 和完全相同内容重试：返回第一次创建的消息，不增加消息数量。
- 相同 UUID 但内容、角色、积分或关联消息不同：返回 `code=20309`。
- `messageId` 和 `sequenceNo` 始终由服务端生成。

## 6. 定时任务接口

SaaS 只保存任务定义，不计算下次执行时间、不执行 Cron、不记录执行结果。

### 6.1 查询全部任务

```http
GET /api/v1/agent/schedules
```

按 `updatedAt DESC, id DESC` 返回全部任务。

### 6.2 创建任务

```http
POST /api/v1/agent/schedules
```

```json
{
  "name": "每日销售汇总",
  "description": "每天生成昨日销售汇总",
  "prompt": "查询昨天销售订单并生成销售汇总",
  "cronExpression": "0 9 * * *",
  "timezone": "Asia/Shanghai",
  "enabled": true
}
```

- Cron 固定为 Unix 五段式：`分 时 日 月 周`。
- `cronExpression` 和 `timezone` 必填。
- `timezone` 必须是 IANA 标识，不能使用固定 `+08:00` 替代地区时区。
- `enabled` 为空时默认为 `true`。

### 6.3 查询任务

```http
GET /api/v1/agent/schedules/{scheduleId}
```

### 6.4 局部修改和启停

```http
PATCH /api/v1/agent/schedules/{scheduleId}
```

```json
{
  "enabled": false,
  "version": 3
}
```

只更新明确提供的字段。清空 `description` 时传空字符串。`version` 必填。

### 6.5 删除任务

```http
DELETE /api/v1/agent/schedules/{scheduleId}?version=4
```

任务定义立即硬删除，本地 Scheduler 的清理由 Agent Client 负责。

### 6.6 时区字典

```http
GET /api/v1/agent/timezones
```

返回当前 Tenant 服务 JDK 支持的全部 IANA 时区标识，并按字典序排列。客户端应使用该接口构建下拉选择，服务端在保存任务时仍会再次校验。

## 7. 内容容量配置

默认限制：

```yaml
agent:
  data:
    message-content-max: 256KB
    message-content-json-max: 64KB
    schedule-prompt-max: 64KB
```

可以通过环境变量覆盖：

```text
AGENT_MESSAGE_CONTENT_MAX
AGENT_MESSAGE_CONTENT_JSON_MAX
AGENT_SCHEDULE_PROMPT_MAX
```

配置修改后重启 Tenant 服务生效。

## 8. 业务错误码

| code | 含义 | 客户端处理建议 |
|---:|---|---|
| 0 | 成功 | 正常处理 `data` |
| 40001 | 参数校验失败 | 检查请求字段 |
| 20301 | 员工上下文请求头错误 | 检查两个身份请求头 |
| 20302 | 员工不存在或已停用 | 退出当前员工工作区 |
| 20303 | 目录不存在 | 刷新目录树 |
| 20304 | 目录名称重复 | 提示更换名称 |
| 20305 | 默认目录不可修改或删除 | 禁用对应客户端操作 |
| 20306 | 会话不存在 | 从本地列表移除或刷新目录树 |
| 20307 | 定时任务不存在 | 从本地列表移除 |
| 20308 | 版本冲突 | 重新拉取后再操作 |
| 20309 | 消息幂等冲突 | 生成新的 `clientMessageId` 或修复客户端复用错误 |
| 20310 | 消息结构不合法 | 检查角色、类型、关联和积分 |
| 20311 | 内容超过容量限制 | 缩短文本或只上传文件引用 |
| 20312 | Cron格式错误 | 使用Unix五段式Cron |
| 20313 | 时区格式错误 | 从时区字典重新选择 |
| 50000 | 系统异常 | 保留请求上下文并稍后重试 |

## 9. 推荐调用流程

```text
1. POST /api/v1/agent/inner/auth
2. 员工选择租户，客户端本地记录 lastSelectedTenantId
3. 后续数据请求携带 X-Tenant-Id 与 X-Employee-Id
4. GET /api/v1/agent/directories/tree
5. GET /api/v1/agent/schedules
6. 打开会话时 GET /conversations/{id}/messages
7. 先保存 USER 消息
8. AgentCore 本地调用 LLM / MCP / Skill
9. 保存 ASSISTANT 消息，并写入整轮 consumedScore
10. 使用追加响应中的 conversationVersion 更新本地会话版本
```

AgentCore 的 LLM、MCP、Skill、Native Tool、浏览器、本地文件和 Scheduler 执行均不属于 Tenant 服务职责。
