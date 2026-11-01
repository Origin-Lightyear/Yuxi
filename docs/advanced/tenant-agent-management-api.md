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

`PUT` 只更新提交的字段。支持 `name`、`description`、`icon`、`profile`、`config_json`、`share_config`；不支持修改 `slug`、`kind`、`tenant_id`。发布后的员工更新档案时仍必须满足发布条件。

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
