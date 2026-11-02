# 租户数字员工与老板分身管理 API

本页面向 Tenant 平台（租户后台）开发者，说明如何通过 HTTP 管理某个租户的数字员工和老板分身。接口前缀为 `/api/tenant`，与租户知识库管理 API 使用同一套服务端鉴权。

员工侧的数字员工、老板分身页面不在本页范围内；这里只提供租户后台的生命周期管理接口。

## 鉴权与租户隔离

每个请求必须携带：

| 请求头 | 必填 | 说明 |
| --- | --- | --- |
| `X-Tenant-Admin-Key` | 是 | 与 Yuxi 环境变量 `TENANT_ADMIN_API_KEY` 比对；密钥由双方线下约定 |
| `X-Tenant-Id` | 是 | 目标租户 ID，整数 |

鉴权行为：

- Yuxi 未启用 SaaS 或未配置 `TENANT_ADMIN_API_KEY` 时，所有接口返回 `404 Not Found`，不暴露路由存在性。
- Key 缺失或不一致返回 `401`。
- 缺少 `X-Tenant-Id` 或类型不是整数返回 `422`。
- 所有资源按 `agents.tenant_id` 精确过滤；跨租户访问统一 `404`，不返回资源是否存在。
- 客户端传入的 `tenant_id` 会被忽略，归属始终由 `X-Tenant-Id` 决定。

安全边界：本组 API 与租户知识库 API 共用实例级静态 Key，`X-Tenant-Id` 由调用方声明。Key 泄露会影响该 Yuxi 实例上的所有租户，当前不提供按租户独立的 Key。Tenant 平台必须只在可信后端保存 Key，并通过网络策略限制来源；不要把 Key 下发到浏览器或普通员工客户端。

已有内置 Agent 和本地 Agent 的 `tenant_id` 为 `NULL`，不属于任何租户；租户管理 API 不会列出它们。

## 权限表达

租户平台通过创建/更新接口的 `share_config` 表达员工可见和可管理范围，没有独立的权限接口。`share_config` 使用 version 2：

```json
{
  "version": 2,
  "read_scope": {
    "access_level": "user",
    "department_ids": [],
    "user_uids": ["employee-code-1"]
  },
  "manage_scope": null
}
```

- `access_level` 支持 `global`、`department`、`user`。在租户资源里，`global` 表示租户内全局，不会跨租户。
- `user_uids` 使用员工登录 Yuxi 时的 `uid`（SaaS 场景通常是 `employeeCode`）。
- `manage_scope` 必须包含在 `read_scope` 内；可以为 `null`，表示没有员工管理权。
- 不传 `share_config` 时默认租户内全局可读，`manage_scope=null`。

## 字段说明

接入方最容易混淆的是：`description`、`digital_employee_profile`、`config_json.context.system_prompt` 和 `share_config` 各自负责不同事情。它们不是同一份说明的不同写法。

### 数字员工字段

| 字段 | 是否可写 | 用途 | 注意事项 |
| --- | --- | --- | --- |
| `name` | 是 | 员工展示名称 | 必填 |
| `slug` | 创建时可写 | URL 和后续更新使用的稳定标识 | 全实例唯一；冲突返回 `409`，保存响应值 |
| `backend_id` | 创建时可写 | Agent 执行后端 | 数字员工当前只接受 `ChatbotAgent`；不能传 `SubAgentBackend` |
| `description` | 是 | 卡片/列表的简短介绍 | **不进入模型 Prompt**，只用于展示 |
| `icon` | 是 | 图标 URL 或资源路径 | 可选 |
| `profile.position` | 是 | 岗位名称 | 发布前必填，最长 80 字符 |
| `profile.department` | 是 | 展示部门 | 发布前必填，最长 80 字符；不是权限部门 |
| `profile.responsibilities` | 是 | 岗位职责说明 | 发布前必填，最长 2000 字符；普通聊天不会自动注入 Prompt |
| `profile.capabilities` | 是 | 能力标签 | 自由文本，最多 12 项、单项 40 字符；发布前至少 1 项；不是工具或 Skill |
| `profile.boundaries` | 是 | 工作边界 | 发布前必填，最长 2000 字符；会议参与者上下文会使用 |
| `profile.acceptance` | 是 | 验收标准 | 发布前必填，最长 2000 字符；会议参与者上下文会使用 |
| `profile.status` | 否 | `draft` / `active` / `disabled` | 由创建、发布、停用接口维护 |
| `system_prompt` | 是 | 数字员工行为 Prompt 的便捷字段 | 服务端写入 `config_json.context.system_prompt`；与老板分身字段一致，显式传入时优先于 `config_json` 内的同名值。响应不返回顶层字段，回读看 `config_json.context.system_prompt` |
| `config_json` | 是 | 运行时配置 | 见下方“运行配置字段” |
| `share_config` | 是 | 员工可见/可管理权限 | 只控制权限，不控制员工知识或 Prompt |
| `tenant_id` | 否 | 租户归属 | 由 `X-Tenant-Id` 注入；请求体传入会被忽略 |
| `kind` | 否 | `digital_employee` 或 `founder_twin` | 由创建入口固定，不能修改 |
| `can_manage` | 否 | 当前调用方是否可管理 | 响应字段 |
| `summary` | 否 | 列表状态统计 | 响应字段，按 `active/draft/disabled` 计数 |

### 运行配置字段 `config_json.context`

这些字段决定员工实际怎么运行，不是展示信息。

| 字段 | 示例 | 用途 |
| --- | --- | --- |
| `system_prompt` | `"你是经营分析官，回答必须引用数据口径。"` | **实际行为/能力 Prompt**，追加到内置系统提示词 |
| `model` | `"provider:model"` | 员工使用的模型；留空使用系统默认模型 |
| `knowledges` | `"all"` / `["销售资料"]` / `[]` | 知识库选择；请求和响应使用名称，响应额外返回 `knowledge_ids`；`"all"` 是当前用户可见全部，`[]` 是明确不使用 |
| `tools` | `"all"` / `["tool-a"]` / `[]` | 可用工具选择 |
| `skills` | `"all"` / `["skill-a"]` / `[]` | 可用 Skill 选择 |
| `mcps` | `[]` / `["mcp-a"]` | MCP 服务选择 |
| `subagents` | `["general-purpose"]` / `[]` | 子智能体选择 |
| `temperature` | `0.3` | 模型温度，按后端 Context 声明生效 |

创建时可以直接提交：

```json
{
  "config_json": {
    "context": {
      "system_prompt": "你是经营分析官。先确认数据口径，再输出结论。",
      "knowledges": ["销售资料"],
      "tools": "all",
      "skills": []
    }
  }
}
```

知识库字段是展示层适配：租户 API 的请求和响应使用知识库名称，响应在 `context.knowledge_ids` 中冗余返回 ID；Yuxi 运行配置仍保存 `kb_id`。外部系统如果直接调用租户 API，优先使用名称，回读时同时保存 `knowledge_ids` 以便需要时定位资源。

也可以把 `system_prompt` 放在顶层；服务端会写入 `config_json.context.system_prompt`。两者同时提交时，顶层 `system_prompt` 优先：

```json
{
  "system_prompt": "你是经营分析官。先确认数据口径，再输出结论。",
  "config_json": {
    "context": {
      "knowledges": ["kb_xxx"],
      "tools": "all"
    }
  }
}
```

如果只写 `description`，模型行为不会改变；需要模型遵循的角色、规则、输出格式，必须写 `system_prompt` 或 `config_json.context.system_prompt`。

### 老板分身字段

| 字段 | 是否可写 | 用途 | 注意事项 |
| --- | --- | --- | --- |
| `name` | 是 | 分身展示名称 | 必填，最长 100 字符 |
| `slug` | 创建时可写 | 稳定标识 | 全实例唯一；冲突返回 `409` |
| `description` | 是 | 卡片/列表简介 | 不进入 Prompt |
| `icon` | 是 | 图标 | 可选 |
| `system_prompt` | 是 | 分身行为 Prompt | 服务端写入 `config_json.context.system_prompt`；不传使用默认分身提示词 |
| `share_config` | 是 | 员工可见/可管理权限 | 只控制权限 |
| `tenant_id` | 否 | 租户归属 | 由 `X-Tenant-Id` 注入 |
| `kind` | 否 | 固定为 `founder_twin` | 不能修改 |
| `stats.confirmed_entries` | 否 | 已确认经验数 | 响应字段 |
| `stats.draft_entries` | 否 | 草稿经验数 | 响应字段 |
| `stats.open_handoffs` | 否 | 打开中的转交数 | 响应字段 |

老板分身的经验、咨询和转交不在本组 API 内管理；`stats` 只是只读计数。

### 常见误区

- `description` 不是 Prompt，只是短介绍。
- `profile.capabilities` 是标签，不是可执行工具/Skill；实际能力在 `config_json.context.tools/skills/mcps`。
- `profile.responsibilities` / `boundaries` / `acceptance` 是档案和会议上下文，不会自动拼进普通聊天 Prompt。
- `config_json.context.knowledges` 默认是 `"all"`；不设置时员工可能使用当前用户可见的全部知识库，需要限制时显式传名称列表或 `[]`。响应中的 `knowledge_ids` 是冗余的 kb_id，用于定位资源。
- `share_config` 是权限，不是内容配置；`global` 在租户内生效，不跨租户。
- `tenant_id`、`kind`、`status`、`can_manage` 是服务端字段，不要作为更新目标。

## 数字员工

### 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/tenant/digital-employees` | 本租户数字员工列表，支持 `status`、`q` |
| `POST` | `/api/tenant/digital-employees` | 创建草稿，返回 `201` |
| `GET` | `/api/tenant/digital-employees/{slug}` | 详情 |
| `PUT` | `/api/tenant/digital-employees/{slug}` | 更新档案、配置和权限 |
| `POST` | `/api/tenant/digital-employees/{slug}/publish` | 发布；发布后员工可见 |
| `POST` | `/api/tenant/digital-employees/{slug}/disable` | 停用；保留历史对话 |
| `DELETE` | `/api/tenant/digital-employees/{slug}` | 删除无引用草稿 |

`status` 只支持 `active`、`draft`、`disabled`。列表响应包含 `summary`，分别统计三种状态的数量。

### 创建请求

```http
POST /api/tenant/digital-employees
X-Tenant-Admin-Key: <key>
X-Tenant-Id: 100
Content-Type: application/json

{
  "name": "经营分析官",
  "slug": "operations-analyst",
  "backend_id": "ChatbotAgent",
  "description": "输出经营分析",
  "system_prompt": "你是经营分析官。先确认数据口径，再输出结论。",
  "profile": {
    "position": "经营分析官",
    "department": "经营管理",
    "responsibilities": "输出经营分析",
    "capabilities": ["数据查询", "报告生成"],
    "boundaries": "不代替管理层决策",
    "acceptance": "结论包含数据口径"
  },
  "share_config": {
    "version": 2,
    "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
    "manage_scope": null
  }
}
```

`slug` 在实例内全局唯一。租户创建接口发现请求的 slug 已被占用时返回 `409`，不会静默追加后缀；Tenant 平台应保存响应中的 `slug`，并在冲突时换一个名称重试。

创建成功返回 `201`：

```json
{
  "employee": {
    "slug": "operations-analyst",
    "name": "经营分析官",
    "kind": "digital_employee",
    "tenant_id": 100,
    "digital_employee_profile": {
      "position": "经营分析官",
      "status": "draft"
    },
    "share_config": {
      "version": 2,
      "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
      "manage_scope": null
    },
    "can_manage": true
  }
}
```

发布前必须填写 `position`、`department`、`responsibilities`、`boundaries`、`acceptance`，并且 `capabilities` 至少一项，否则返回 `422`。

### 更新请求

`PUT` 只更新提交的字段。支持 `name`、`description`、`icon`、`profile`、`config_json`、`system_prompt`、`share_config`；不支持修改 `slug`、`kind`、`tenant_id`。发布后的员工更新档案时仍必须满足发布条件。只提交 `system_prompt` 时，其他 `config_json.context` 字段保持不变。

### 删除限制

只有 `draft` 状态、且没有被对话、定时任务、IM Bot 或会议引用的数字员工可以删除。其他情况返回 `409`。

## 老板分身

### 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/tenant/founder-twins` | 本租户老板分身列表 |
| `POST` | `/api/tenant/founder-twins` | 创建，返回 `201` |
| `GET` | `/api/tenant/founder-twins/{slug}` | 详情 |
| `PUT` | `/api/tenant/founder-twins/{slug}` | 更新展示信息和系统提示词 |
| `DELETE` | `/api/tenant/founder-twins/{slug}` | 删除无历史引用的分身 |

### 创建请求

```http
POST /api/tenant/founder-twins
X-Tenant-Admin-Key: <key>
X-Tenant-Id: 100
Content-Type: application/json

{
  "name": "老板分身",
  "description": "按老板已确认经验回答",
  "system_prompt": "你是老板分身。先检索已确认经验，再给出结论。",
  "share_config": {
    "version": 2,
    "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
    "manage_scope": null
  }
}
```

成功返回 `201`：

```json
{
  "twin": {
    "slug": "lao-ban-fen-shen",
    "name": "老板分身",
    "kind": "founder_twin",
    "tenant_id": 100,
    "backend_id": "ChatbotAgent",
    "share_config": {
      "version": 2,
      "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
      "manage_scope": null
    },
    "system_prompt": "你是老板分身。先检索已确认经验，再给出结论。",
    "can_manage": true,
    "stats": {
      "confirmed_entries": 0,
      "draft_entries": 0,
      "open_handoffs": 0
    }
  }
}
```

不传 `system_prompt` 时使用默认的分身提示词。`PUT` 支持 `name`、`description`、`icon`、`system_prompt`、`share_config`；不支持修改 `slug`、`kind`、`tenant_id`。列表和详情都会返回当前 `share_config` 与 `system_prompt`，平台可以直接回读并检测漂移。

### 经验、咨询与转交

本组 API 只管理老板分身本身。经验条目、咨询记录和转交单继续在 Yuxi 的老板分身页面维护，租户平台不直接读写这些数据。

### 删除限制

老板分身存在以下任一引用时拒绝删除并返回 `409`：

- 经验条目、咨询记录、转交单；
- 分身会议或决议；
- 对话、定时任务、IM Bot。

需要保留历史时，应保留分身记录，不要为了清理列表强行删除。

## 错误码

| 场景 | 状态码 | `detail` 示例 |
| --- | --- | --- |
| SaaS 未启用或管理 Key 未配置 | `404` | `Not Found` |
| Key 缺失或不一致 | `401` | `无效的管理 API Key` |
| 缺少 `X-Tenant-Id` | `422` | `缺少 X-Tenant-Id 请求头` |
| 资源不存在或跨租户访问 | `404` | `数字员工不存在` / `老板分身不存在` |
| 发布条件不满足 | `422` | `发布前请填写：岗位、职责...` |
| 删除仍有引用 | `409` | `数字员工仍被对话、定时任务、IM Bot 或会议引用` |
| 创建时 slug 已被占用 | `409` | `slug 已被占用` |
| 删除非草稿数字员工 | `409` | `只有草稿数字员工可以删除` |
| `share_config` 越界或格式错误 | `422` | 具体校验错误 |

## 对接流程

1. Yuxi 侧确认 `SAAS_ENABLED=true` 并配置 `TENANT_ADMIN_API_KEY`，与 Tenant 侧保存的密钥一致。
2. Tenant 后台使用 `X-Tenant-Admin-Key` + `X-Tenant-Id` 调用本组接口。
3. 创建数字员工/老板分身时，把租户后台的角色勾选展开成 `share_config.user_uids` 或部门 ID。
4. 数字员工填写完整岗位档案后调用 `publish`；停用时调用 `disable`。
5. 删除前先确认没有历史引用；有引用时保留资源，避免破坏会议、经验和待办。

## 代码位置

| 模块 | 路径 |
| --- | --- |
| 租户 Agent 路由 | `backend/server/routers/tenant_agent_router.py` |
| 租户鉴权依赖 | `backend/server/utils/tenant_admin.py` |
| 数字员工服务 | `backend/package/yuxi/services/digital_employee_service.py` |
| 老板分身服务 | `backend/package/yuxi/services/founder_twin_service.py` |
| Agent 租户归属与查询 | `backend/package/yuxi/repositories/agent_repository.py` |
| 权限隔离 | `backend/package/yuxi/permissions/resource_permission.py` |
| Schema 迁移 | `backend/package/yuxi/storage/postgres/manager.py` |
