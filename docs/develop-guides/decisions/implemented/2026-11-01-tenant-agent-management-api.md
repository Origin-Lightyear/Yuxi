# 租户平台数字员工与老板分身管理 API

状态：implemented
类型：feature
Owner：backend/server/routers/tenant_agent_router.py

## 问题

租户平台需要在租户后台创建和维护本租户的数字员工与老板分身。现有 `/api/digital-employees` 与 `/api/founder-twins` 依赖本地登录用户和 `share_config`，`agents` 表也没有租户归属字段。直接复用会让不同租户的数字员工、老板分身出现在同一列表，并允许跨租户按 slug 读取或修改。

租户知识库已经有可复用的服务端鉴权约定：`X-Tenant-Admin-Key` 与 `TENANT_ADMIN_API_KEY` 比对，`X-Tenant-Id` 指定目标租户，SaaS 未启用时返回 404。

## 决策

### 租户归属

- `agents` 增加可空 `tenant_id` 和索引。已有内置 Agent 与本地 Agent 保持 `NULL`。
- 租户管理 API 创建的数字员工和老板分身写入 `X-Tenant-Id` 对应的 `tenant_id`；更新接口不接受归属变更。
- 非 `superadmin` 用户解析 Agent 权限时，资源 `tenant_id` 非空且与用户租户不一致直接返回 `NONE`。SaaS 登录上下文同时把 `tenant_id` 挂到当前用户对象。
- `User.user_config` 使用 `selectin` 预加载；`User.tenant_id` 从登录身份读取租户归属，覆盖 worker、定时任务、API Key 和知识库工具等从数据库加载用户的路径，同时不破坏 `SELECT ... FOR UPDATE`。
- 本地内置 Agent 的 `tenant_id` 为 `NULL`，继续对所有租户可见。

### 接口边界

新增两个租户管理路由，沿用租户知识库的鉴权依赖和错误语义：

- `/api/tenant/digital-employees`：列表、详情、创建、更新、发布、停用、删除。
- `/api/tenant/founder-twins`：列表、详情、创建、更新、删除。

创建返回 `201`，其余成功返回现有资源结构。跨租户访问统一 `404`，不暴露资源存在性。删除数字员工沿用草稿和引用检查；删除老板分身在存在经验、咨询、转交、会议或 Agent 引用时返回 `409`。

数字员工与老板分身都接受顶层 `system_prompt`；服务端写入 `config_json.context.system_prompt`，显式字段优先于 `config_json` 内的同名值。数字员工只提交 `system_prompt` 更新时，其他 `config_json.context` 字段保持不变；响应不回顶层字段，回读看 `config_json.context.system_prompt`。

租户数字员工 API 的知识库字段使用名称做展示层契约：请求和响应中的 `config_json.context.knowledges` 是名称列表，响应同时冗余返回 `knowledge_ids`；Yuxi 运行配置仍保存 `kb_id`。这样租户平台不需要把 ID 直接展示给用户。

### 权限表达

租户平台通过创建/更新接口的 `share_config` 表达员工可见和可管理范围，不新增独立权限接口。`share_config` 继续使用 v2 的 `read_scope` / `manage_scope`；租户归属由服务端注入，客户端不能覆盖。

### 非目标

- 不通过租户 API 管理老板分身的经验、咨询或转交；这些继续在 Yuxi 内维护。
- 不新增前端页面，不改变员工侧数字员工、老板分身的现有交互。
- 不把租户 API 暴露给普通 JWT 用户。

## 替代方案

- 复用现有管理员接口：实现少，但无法表达租户归属，跨租户读取和 slug 冲突无法闭环，拒绝。
- 只依赖 `share_config`：不增加 schema，但 `global` 范围会跨租户泄漏，列表查询也没有租户过滤，拒绝。
- 独立租户 Agent 映射表：能表达归属，但每次列表和权限判断都要额外 join，且现有 Agent 已经适合承载可空归属，拒绝。

## 后果

- 业务 schema 从 13 升到 14；迁移只增加可空列和索引，现有行无需回填。
- 现有本地管理员默认看不到租户平台创建的 Agent；`superadmin` 仍可跨租户维护。
- 租户平台必须持有静态管理 Key，并保证 `X-Tenant-Id` 与自身租户一致。
- 静态管理 Key 是实例级共享密钥；持有者可声明任意 `X-Tenant-Id`。当前不提供按租户独立 Key，部署必须限制 Key 的分发范围和网络来源。
- 老板分身删除会因历史引用失败，平台需要先处理引用或在 Yuxi 内保留。

## 对接备注

对外字段契约由 [租户数字员工与老板分身管理 API](../../../advanced/tenant-agent-management-api.md) 的“字段说明”维护，本记录不复制完整字段表。接入方最容易误解的字段如下：

| 字段 | 不是 | 实际用途 |
| --- | --- | --- |
| `description` | 行为说明或 Prompt | 卡片/列表短介绍，仅展示，不进入模型上下文 |
| `digital_employee_profile.capabilities` | 可执行工具或 Skill | 自由能力标签；最多 12 项、单项 40 字符，发布前至少 1 项 |
| `digital_employee_profile.responsibilities/boundaries/acceptance` | 自动注入的聊天 Prompt | 档案展示、搜索和会议参与者上下文；普通聊天不会自动拼接 |
| `config_json.context.system_prompt` | 普通描述字段 | 实际行为/能力 Prompt，追加到内置系统提示词；数字员工和老板分身也可用顶层 `system_prompt` 写入 |
| `config_json.context.knowledges` | 权限配置 | 运行时知识库选择；租户 API 请求/响应用名称，响应冗余返回 `knowledge_ids`，运行配置保存 kb_id；默认 `"all"`，`[]` 表示不使用 |
| `config_json.context.tools/skills/mcps/subagents` | 展示标签 | 运行时工具、Skill、MCP 和子智能体选择 |
| `share_config` | 员工内容配置 | 员工可见/可管理权限；`global` 只在租户内生效 |
| `backend_id` | 员工类型 | Agent 执行后端；数字员工当前只接受 `ChatbotAgent` |
| `tenant_id`/`kind`/`status`/`can_manage` | 可写字段 | 服务端注入或生命周期维护的只读字段 |

接入方如果只填写 `description`，数字员工的模型行为不会改变；需要模型遵循的角色、规则和输出格式必须写入 `config_json.context.system_prompt`。如果不设置 `knowledges`，默认使用当前用户可见的全部知识库；需要限制时必须显式传知识库列表或 `[]`。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
| --- | --- | --- | --- | --- | --- |
| `agents.tenant_id` 可重复迁移并存在索引 | 旧库升级后列缺失或迁移不可重放 | `backend/package/yuxi/storage/postgres/manager.py` | `pytest test/integration/api/test_digital_employee_migration.py test/integration/storage/test_founder_twin_migration.py test/integration/storage/test_work_item_views_migration.py test/integration/services/test_schema_migration_version.py`：16 passed | 重复执行 `ensure_business_schema` 保持幂等；迁移测试断言列与索引存在 | Passed |
| 租户 API 只操作本租户资源 | 跨租户按 slug 读取、更新、发布或删除 | `backend/server/routers/tenant_agent_router.py` | `pytest test/integration/api/test_tenant_agent_router.py`：8 passed | `X-Tenant-Id: 999` 访问租户 100 资源返回 404；跨租户重复 slug 返回 409 | Passed |
| 租户归属不能被客户端覆盖 | 请求体 `tenant_id` 改变资源归属 | `backend/package/yuxi/services/digital_employee_service.py` | 同上；创建/更新请求携带 `tenant_id: 999`，响应仍为 100 | 断言响应 `tenant_id == 100` | Passed |
| 普通权限解析阻止跨租户 Agent | `global` 共享范围绕过租户边界 | `backend/package/yuxi/permissions/resource_permission.py` | `pytest test/unit/permissions/test_resource_permission.py`：19 passed | 租户 200 管理员读取租户 100 资源得到 `NONE` | Passed |
| 数据库加载的 SaaS 用户保留租户上下文 | worker、定时任务、API Key 等路径读取不到租户归属 | `backend/package/yuxi/storage/postgres/models_business.py` | `pytest test/integration/api/test_tenant_agent_router.py`：8 passed；`pytest test/unit/repositories/test_agent_repository.py`：按租户过滤用例通过 | DB 加载的同租户用户可读取 Agent，切换到 200 后返回 `None` | Passed |
| 租户上下文不破坏带锁用户查询 | `selectin`/joined 预加载与 `FOR UPDATE` 冲突 | `backend/package/yuxi/storage/postgres/models_business.py` | `pytest test/integration/services/test_api_key_user_lifecycle.py -k test_create_first`：1 passed；`pytest test/integration/services/test_scheduled_agent_repository.py::test_transient_dispatch_failure_is_recovered_exactly_once test/integration/services/test_scheduled_agent_repository.py::test_account_soft_deletion_removes_scheduled_job_history`：2 passed | 带锁用户读取、API Key 创建、定时派发恢复与账号删除均通过 | Passed |
| 数字员工删除守卫包含会议引用 | 删除已被会议使用的员工破坏历史 | `backend/package/yuxi/repositories/agent_repository.py` | 同上租户 API 测试中的 `test_tenant_digital_employee_delete_guard_includes_meetings` | 插入会议参与记录后删除返回 409 | Passed |
| 数字员工与老板分身都支持顶层 `system_prompt` | 接入方需要记两套 Prompt 字段 | `backend/package/yuxi/services/digital_employee_service.py` | `pytest test/integration/api/test_tenant_agent_router.py`：8 passed | 创建和更新写入 `config_json.context.system_prompt`，只更新 Prompt 时其他 context 字段保留 | Passed |
| 知识库名称展示且 ID 冗余 | 租户平台直接展示 kb_id，或发送名称被运行配置拒绝 | `backend/server/routers/tenant_agent_router.py` | 同上测试中的 `test_tenant_agent_knowledge_names_are_displayed_and_ids_stored` | 请求/响应返回名称，数据库配置断言保存 kb_id | Passed |
| 现有数字员工、老板分身和 Agent 路径无回归 | 租户字段改变现有管理或员工读取行为 | `backend/package/yuxi/services/founder_twin_service.py` | `pytest test/integration/api/test_tenant_agent_router.py`：8 passed；`pytest test/integration/api/test_founder_twin_api.py`：2 passed；`pytest test/integration/api/test_digital_employee_api.py`：9 passed | 原有用例覆盖草稿隐藏、发布、经验管理与转交 | Passed |
| 知识库权限无回归 | 通用租户检查误伤知识库读取 | `backend/package/yuxi/permissions/resource_permission.py` | `pytest test/integration/api/test_tenant_knowledge_router.py test/integration/api/test_knowledge_router_employee.py test/integration/api/test_agent_config_resource_authorization.py`：40 passed, 5 skipped | 租户知识库与员工知识库用例 | Passed |
| 全量后端 unit 无回归 | 新增租户列或权限判断破坏既有行为 | 全仓库 | `pytest test/unit -m "not slow"`：2725 passed, 58 skipped | 无 | Passed |
| 文档可构建 | 新页面或导航链接失效 | `docs/advanced/tenant-agent-management-api.md` | `docs/node_modules/.bin/vitepress build` | 构建包含新页面 | Passed |
| 工程契约 | 决策记录或 Owner 引用失效 | `docs/develop-guides/decisions/implemented/2026-11-01-tenant-agent-management-api.md` | `python3 scripts/verify_engineering_contracts.py` | 非本变更的未跟踪提案文件不参与最终提交 | Passed（排除用户未跟踪提案后） |

未执行：`ruff check` / `ruff format --check`，当前容器未提供 `ruff` 可执行文件。未执行真实浏览器验证：本次只新增后端 API 和 Markdown 文档。未执行完整 worker 端到端聊天：已用 DB 加载用户、API Key 生命周期和定时派发恢复覆盖租户上下文与带锁查询路径。租户管理路由集成测试依赖 `TENANT_ADMIN_API_KEY`；未配置时会跳过，repository 层租户过滤单测仍覆盖跨租户查询。
