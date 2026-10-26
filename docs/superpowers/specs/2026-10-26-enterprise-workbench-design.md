# 企业智能工作台：数字员工、待办与老板分身

状态：P1/P2/P3 implemented（2026-10-26）

日期：2026-10-26

范围：P1「数字员工」、P2「待办事项」、P3「老板分身」均已实现；实现决策和验证见对应决策记录。

部署形态：单企业自建（一个 Yuxi 部署服务一个企业）。SaaS 多租户共享实例不在 P1。

来源原型：`大力水手Agent-企业智能工作台.html`（未跟踪的静态演示）

## 1. 意图与为什么

原型已经把产品形态讲清楚：员工先看数字员工能做什么，把事交给它；所有执行进入统一待办；老板的判断方式沉淀成分身，员工先问分身，分身没把握再转本人。

当前仓库里这三块都没有真实事实：原型是静态 JS 模板，数字员工没有岗位元数据，待办没有跨线程查询，老板分身没有经验条目和引用约束。本设计把原型收敛成 Yuxi 的产品功能，而不是继续维护一份演示页面。

P1 先交付「数字员工」：管理员能配置、发布、停用；员工能看到在岗数字员工并直接对话；权限、状态和运行时入口都由后端拥有。P2 已交付统一执行收件箱，P3 已交付经验引用、答案守卫和转交闭环。

## 2. 目标、非目标与反目标

### 目标

- P1：数字员工是 `Agent` 的一种受管类型，具备岗位、职责、能力、边界、验收和发布状态。
- P1：管理员创建草稿、发布、停用；普通员工只看到在岗且在自己共享范围内的数字员工。
- P1：数字员工复用现有 Agent 对话、知识库、Skills、MCP、模型和 Run 链路，不新建执行后端。
- P1：停用后不能再发起新 Run，但历史对话仍可读；权限判断在后端完成。
- P2（边界）：待办是 `AgentRun` / `ScheduledAgentRun` / 转交单的运行时投影，不是第二套任务事实表。
- P3（边界）：老板分身的经验条目、确认状态、引用和转交必须可持久化、可校验；不能只靠提示词。

### 非目标

- 不做员工之间的任务创建、指派、截止时间和通知（待办只做 AI 执行收件箱）。
- 不做数字员工的独立服务账号或越权数据访问；运行时数据权限仍跟随调用身份。
- 不做会议录音转写、审批意见自动抽取；P3 MVP 只支持手工/文本导入经验。
- 不做多租户共享实例、移动端、KPI 或绩效。
- 不把老板分身放进 P1；P1 只预留 `kind` 的扩展空间。
- 不改造现有 Agent 管理页为工作台，也不重排全局导航。

### 反目标

- 页面继续显示静态示例数据或写死状态。
- 在前端隐藏按钮来代替后端授权。
- 为待办新建一张与 `AgentRun` 并行、需要双向同步的状态表。
- 用提示词宣称「只引用已确认经验」，但没有可验证的引用记录。

## 3. 约束与假设

### 已核实的系统事实

- `Agent` 只有 `name/description/icon/config_json/share_config/is_subagent/...`，没有岗位、部门、状态或 tenant 字段（`backend/package/yuxi/storage/postgres/models_business.py:300`）。
- `share_config` v2 的 `read_scope` / `manage_scope` 是现有授权边界；`admin` 不会自动获得所有部门或用户的资源（`backend/package/yuxi/permissions/resource_permission.py`）。
- 预置 Agent 由 `ensure_preset` 首次落库，之后不覆盖用户修改；非 builtin 预置只有超级管理员可管理（`backend/package/yuxi/repositories/agent_repository.py:125`）。
- `AgentRun.status` 的 `interrupted` 是终态，审批载荷只在 LangGraph checkpoint；恢复会新建 Run（`backend/package/yuxi/services/agent_run_service.py`、`tool_approval.py`）。
- 定时任务通过 `source=scheduled_agent` 进入统一 Request/Run 链路（`backend/package/yuxi/services/scheduled_agent_service.py:425`）。
- 知识库检索没有条目状态或自定义 metadata 过滤；Milvus 的 `metadata.source` 固定为「未知来源」（`backend/package/yuxi/knowledge/implementations/milvus.py`）。
- 仓库没有 pgvector；PostgreSQL 没有向量列。

### 假设（已确认）

- **部署形态为 A：单企业自建。** 一个部署服务一个企业，管理员在 Yuxi 内配置数字员工。P1 不新增 `Agent.tenant_id`，默认 `global` 共享只在单企业部署成立。
- 数字员工由管理员配置和发布；普通员工只消费。部门经理自助创建不在 P1。
- 「部门」在员工档案里是展示字段；真正的可见范围仍由 `share_config` 拥有。两者可以不一致，UI 必须避免让用户误以为档案部门就是授权部门。
- 数字员工运行时的知识库/Skills/工具可见性按调用身份解析；P1 不引入服务账号或权限提升。
- P2/P3 的实现决策和验证分别记录在 `implemented/2026-10-26-workbench-todos-p2.md` 和 `implemented/2026-10-26-founder-twin-p3.md`。

## 4. 方案与取舍

### 4.1 数字员工的数据归属

**选择：扩展 `Agent`。**

- 增加 `kind`（`agent|digital_employee`）和 `digital_employee_profile`（JSON）。
- 复用 `AgentRepository`、`share_config`、`AgentEditModal`、`AgentView`、Run 链路。
- 代价：`Agent` 承担两种产品语义；必须用接口和校验阻止两条写路径互相污染。

**未选：**

- 新建 `digital_employees` 表与 Agent 一对一：领域更干净，但身份、权限、对话入口、列表都要双写或 join；P1 没有足够收益。
- 只做 preset：`ensure_preset` 不覆盖、不可由管理员运行时创建/发布，无法形成产品闭环。

### 4.2 待办的事实来源（P2 边界）

**选择：运行时投影。**

- 以「每线程最新顶层 Run」为待办事实，叠加 `scheduled_agent` 来源和 P3 转交单。
- 只新增已读表；不新增与 Run 并行的任务状态。
- 代价：跨线程查询需要窗口函数和索引；审批详情仍要回到会话处理。

**未选：**

- 新建 `todo_items` 表由 worker 同步：查询方便，但引入第二事实源和一致性风险。
- 复用管理员任务中心：那是 Durable Task，和数字员工执行不是同一领域。

### 4.3 老板分身的经验检索（P3）

**选择：PostgreSQL 经验条目 + 写入时 embedding，查询时进程内余弦 top-k。**

- 单分身经验条目上限按 2,000 条设计；按 `twin_slug + status` 过滤，引用 ID 稳定。
- 代价：不适合十万级条目；规模上来后需要迁移到 Milvus 或独立向量索引。

**未选：**

- 每个分身建独立知识库走 Milvus：复用检索链路，但没有状态过滤、来源元数据不可靠、入库异步，条目级引用难以验收。
- 把所有条目塞进 prompt：只适合很小的库，无法控制规模和引用。

### 4.4 分身答案的约束（P3）

**选择：Skill + 校验工具。**

- `retrieve_experience` 检索；`submit_consultation_answer` 校验引用的条目确实已确认且属于该分身；前端从工具结果渲染依据。
- 代价：需要 P3 单独设计「未调用校验工具就结束」的 guard。

**未选：**

- 单独咨询 API + 结构化 JSON：控制力最强，但复制会话、流式、历史一套。
- 只靠提示词：无法证明不编造。

## 5. P1 详细设计：数字员工

### 5.1 领域模型与命名

内部统一使用 `digital_employee`，避免和仓库里已有的 SaaS「员工」（人类用户，`UserConfig.employee_id`、`is_employee()`）冲突。用户界面文案仍是「数字员工」。

`Agent` 增加两列：

| 字段 | 类型 | 默认 | 语义 |
|---|---|---|---|
| `kind` | `VARCHAR(32)` | `agent` | `agent` 普通智能体；`digital_employee` 数字员工。P3 可增加 `founder_twin`，不需要改列类型。 |
| `digital_employee_profile` | JSON（PG 为 JSONB） | `{}` | 数字员工的岗位档案；`kind=agent` 时为空对象。 |

`digital_employee_profile` 结构（服务端归一化后）：

```json
{
  "position": "经营分析官",
  "department": "经营管理",
  "responsibilities": "整合授权经营数据，输出结构化分析报告",
  "capabilities": ["数据查询", "指标分析", "报告生成"],
  "boundaries": "不自动确认账务、不代替负责人做决策",
  "acceptance": "报告需给出数据口径、异常项和待确认事项",
  "status": "active",
  "published_at": "2026-10-26T10:00:00",
  "published_by": "uid-xxx",
  "disabled_at": null,
  "disabled_by": null
}
```

字段规则：

- `position`、`department`：1–80 字符；发布时必填，active 状态下更新后仍必须有效。
- `responsibilities`、`boundaries`、`acceptance`：0–2000 字符；发布时三项都必填。
- `capabilities`：去空白、去重、最多 12 个，每个 1–40 字符；发布时至少 1 个。
- `status`：只由服务端状态迁移写入；客户端提交时忽略。
- `published_*` / `disabled_*`：只由服务端写入。
- 草稿允许字段不完整；发布时统一校验。active 员工每次更新后重新校验，避免「发布时合法、编辑后非法」。
- `PUT` 对 `profile` 做补丁合并：未提交的字段保持原值；提交的字段归一化后合并。

状态机：

```text
create ──> draft ──publish──> active ──disable──> disabled
                │                                  │
                └──────────disable─────────────────┘
                                                   │
                              publish <────────────┘
```

- 发布：`draft` / `disabled` → `active`，校验档案；重复发布幂等。
- 停用：只允许 `active` → `disabled`，幂等；草稿只能删除，避免草稿停用后既不能删也不能发布。
- 删除：只允许 `draft`，且必须没有线程、定时任务或 IM Bot 引用；`active` / `disabled` 只能停用，避免孤儿 Run。
- 编辑：不改变 `status`；`active` 员工改档案后必须仍通过发布校验。
- `kind` 创建后不可变。

### 5.2 持久化与迁移

- 修改 `backend/package/yuxi/storage/postgres/models_business.py` 的 `Agent`：
  - `kind = Column(String(32), nullable=False, default="agent", index=True)`
  - `digital_employee_profile = Column(JSON_VALUE, nullable=False, default=dict)`
  - `to_dict()` 输出 `kind`、`digital_employee_profile`。
- 修改 `backend/package/yuxi/storage/postgres/manager.py` 的建表与幂等迁移：
  - `ALTER TABLE IF EXISTS agents ADD COLUMN IF NOT EXISTS kind VARCHAR(32) NOT NULL DEFAULT 'agent'`
  - `ALTER TABLE IF EXISTS agents ADD COLUMN IF NOT EXISTS digital_employee_profile JSONB NOT NULL DEFAULT '{}'::jsonb`
  - `CREATE INDEX IF NOT EXISTS ix_agents_kind ON agents (kind)`
  - 新建表 DDL 同步加两列。
- 既有行由默认值回填为 `kind='agent'`、`digital_employee_profile='{}'`；不需要数据迁移脚本。
- SQLite 测试库由 SQLAlchemy metadata 建表，列类型使用 `JSON_VALUE` 的 JSON 变体。
- 不在 P1 增加独立的 `digital_employee_status` 列。员工列表规模按几十到几百设计，状态在服务层过滤；超过规模再提案加列。

### 5.3 服务与仓储边界

新增 `backend/package/yuxi/services/digital_employee_service.py`，拥有数字员工生命周期：

- `list_digital_employees(user, db, *, status=None, q=None)`
- `get_digital_employee(user, db, slug)`
- `create_digital_employee(user, db, payload)`
- `update_digital_employee(user, db, slug, payload)`
- `publish_digital_employee(user, db, slug)`
- `disable_digital_employee(user, db, slug)`
- `delete_digital_employee(user, db, slug)`
- `normalize_profile(patch, current)`
- `validate_for_publish(profile)`
- `digital_employee_is_runnable(agent)`：`kind != 'digital_employee'` 或 `profile.status == 'active'`
- `digital_employee_is_visible(user, agent)`：`user_can_access_agent` 且（`user_can_manage_agent` 或 `status == 'active'`）

职责分工：

- `digital_employee_service` 拥有档案校验、状态迁移、发布/停用规则、可见性和运行守卫。
- `AgentRepository` 继续拥有 Agent 行写入和 `share_config` 归一化；`create/update` 增加可选 `kind` / `digital_employee_profile` 参数。
- `prepare_agent_config_write` 继续拥有 `config_json` 的字段/资源过滤；员工服务和现有 Agent 路由共用。
- 路由只做 Pydantic 入参、身份依赖和 HTTP 错误映射，不写业务规则。

运行时 guard：

- 在 `agent_request_service.submit_agent_request` 和 `agent_run_service.create_resume_run_view` 加载 Agent 后调用 `digital_employee_is_runnable`；不可运行时返回明确 409（员工未发布/已停用）。
- FIFO 队列的 `dispatch_next_request`/`dispatch_ready_head` 在真正创建 Run 前复查员工状态；不可运行时把**当前派发队头**置为 `rejected`、消息置为 `rejected`，不创建 Run。同线程其余请求仍保持 `queued`，由后续回收循环在轮到它们时逐个拒绝；paused 队列不会自动派发，保持 `queued`，用户 `continue_thread_queue` 返回 409 `agent_not_runnable` 或自行取消。派发检查锁定员工行，与停用状态迁移串行化，避免停用提交后仍创建 Run。
- `conversation_service.create_thread_view`、`scheduled_agent_service._validate_agent` 和 IM Bot 配置创建/更新使用同一 guard；草稿/停用员工不能建线程、不能建定时任务、不能绑定 Bot。
- 历史读取（`chat_service`）只使用现有权限可见性，不调用 runtime guard；停用后历史仍可读。
- 正在运行的 Run 不因停用而中断；停用只影响新请求、新 resume、新线程/任务/Bot 绑定。

通用 Agent 接口隔离：

- `POST /api/agent` 不暴露 `kind`/`digital_employee_profile`；始终创建 `kind=agent`。
- `PUT /api/agent/{slug}` 和 `DELETE /api/agent/{slug}` 遇到 `kind=digital_employee` 返回 400，提示使用数字员工接口。
- `GET /api/agent` 是运行时目录：排除 `kind=digital_employee` 且 `status != active` 的员工；active 员工保留，供聊天选择器使用。
- `GET /api/agent/{slug}` 对 active/disabled 员工可读（只要权限可见），对非管理者读取 draft 返回 404、对管理者返回 200。这样停用员工的会话历史还能加载，管理者编辑器也能加载草稿；普通员工读取草稿改用 `/api/digital-employees/{slug}` 仍是 404。
- `GET /api/agent?kind=agent` 供只想要普通智能体的调用方使用；`AgentManagePanel` 改为传 `kind=agent`，避免员工出现在智能体管理页。

### 5.4 HTTP 契约

新增 `backend/server/routers/digital_employee_router.py`，前缀 `/api/digital-employees`，注册到 `backend/server/routers/__init__.py`。

| 方法 | 路径 | 身份 | 行为 |
|---|---|---|---|
| GET | `/api/digital-employees` | 已登录 | 列出当前用户可见的员工；普通用户只得到 active；管理员可得到 draft/disabled；支持 `status`、`q`。 |
| GET | `/api/digital-employees/{slug}` | 已登录 | 读取可见员工详情；普通用户对 draft/disabled 404。 |
| POST | `/api/digital-employees` | `admin`/`superadmin` | 创建草稿；`backend_id` 默认 `ChatbotAgent`，拒绝 `SubAgentBackend`；`kind=digital_employee`。 |
| PUT | `/api/digital-employees/{slug}` | `can_manage` | 更新名称、描述、图标、档案、`config_json`、`share_config`；不改 `kind`/`status`；active 时重新校验档案。 |
| POST | `/api/digital-employees/{slug}/publish` | `can_manage` | 校验档案并置 `active`；幂等。 |
| POST | `/api/digital-employees/{slug}/disable` | `can_manage` | 置 `disabled`；幂等。 |
| DELETE | `/api/digital-employees/{slug}` | `can_manage` + draft | 删除草稿；有线程/定时任务/IM Bot 引用或非 draft 时 409。 |

创建请求示例：

```json
{
  "name": "经营分析官",
  "slug": "business-analyst",
  "description": "整合授权经营数据，输出结构化分析报告",
  "icon": "",
  "backend_id": "ChatbotAgent",
  "profile": {
    "position": "经营分析官",
    "department": "经营管理",
    "responsibilities": "整合授权经营数据，分析趋势并生成报告",
    "capabilities": ["数据查询", "指标分析", "报告生成"],
    "boundaries": "不代替负责人做决策",
    "acceptance": "报告包含数据口径、异常项和待确认事项"
  },
  "config_json": {"context": {"knowledges": ["kb-xxx"], "skills": ["knowledge-base"]}},
  "share_config": {
    "version": 2,
    "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
    "manage_scope": null
  }
}
```

响应：

```json
{
  "employee": {
    "id": 7,
    "slug": "business-analyst",
    "name": "经营分析官",
    "description": "...",
    "icon": "",
    "kind": "digital_employee",
    "digital_employee_profile": {"...": "..."},
    "share_config": {"...": "..."},
    "can_manage": true,
    "effective_permission": "manage",
    "is_template": false
  }
}
```

员工页面和 `/api/digital-employees` 以 `slug` 作为规范 ID；`id` 是通用 Agent 的数字主键，不参与员工路由。

错误语义：

- 403：非管理员创建；无 manage 权限编辑/发布/停用/删除。
- 404：员工不存在、不可见，或通用 Agent 入口碰到 draft 员工。
- 409：删除有引用或非草稿；运行时入口碰到非 active 员工；状态迁移冲突。
- 422：档案校验失败（包括 active 更新后非法）、`SubAgentBackend`、非法 `kind`。

### 5.5 权限、可见性与调用身份

- 创建：`admin`/`superadmin`。普通用户不能创建数字员工，也不能通过 `POST /api/agent` 绕过。
- 读取：`share_config.read_scope` 命中；草稿/停用只对 `can_manage` 可见（员工目录）。历史记录例外，见下。
- 编辑/发布/停用/删除：`can_manage`（owner、manage_scope、superadmin；builtin 规则不适用于员工，因为员工不是 builtin）。
- 运行时：active + 权限可见。停用后新请求/新 resume 409；历史读取仍按权限。
- 历史记录：停用员工的 `GET /api/agent/{slug}` 和会话历史读取保留，目录列表不展示；这样用户还能打开旧对话。
- 部门字段只用于展示；授权只看 `share_config`。UI 在编辑页明确标注「展示部门」与「共享范围」是两件事。
- 默认共享范围：管理员创建时默认 `global`（全企业可读），`manage_scope=null`（仅 owner/superadmin 可管理）。这是单企业部署假设下的产品默认；SaaS 多租户共享实例不在 P1。
- 调用身份：员工可以出现在现有所有 Agent 调用面（网页、API Key、IM Bot、定时任务）。数据权限按**调用身份**解析：网页用户、API Key owner、Bot owner、定时任务 owner；不是按「最终提问人」冒充。停用只阻止新请求/新 resume/新绑定，不会自动停用已有 Bot 或定时任务；其 owner 会收到明确 409。
- 运行时的 KB/Skills/工具可见性仍按调用身份解析；员工不会因为被发布而获得调用身份没有的数据权限。这是 P1 的安全不变量，也是产品文案必须承认的边界。

### 5.6 前端

路由与导航：

- 新增 `web/src/views/DigitalEmployeeView.vue`，路由 `/digital-employees`，`name: 'DigitalEmployeeComp'`，`AppLayout` 子路由，`requiresAuth: true`，`keepAlive: false`。
- `AppLayout.mainList` 增加 `{ name: '数字员工', path: '/digital-employees', icon: Users }`，放在「智能体」之后。
- 不新增管理端守卫；后端授权是边界。普通员工能进页面，只看到 active 员工。

页面：

- `PageHeader`：标题「数字员工」，信息区显示「N 个在岗 / M 个草稿」（草稿数仅管理者可见）。
- `PageShoulder`：搜索、状态筛选（管理者可见草稿/停用）、「配置数字员工」按钮（管理员可见）。
- 卡片网格：复用 `InfoCard` 或同风格卡片；字段为岗位、部门、描述、能力标签、状态徽标、边界摘要；操作「查看详情」「去对话」。
- 详情抽屉：岗位、职责、能力、边界、验收、共享范围、发布时间；管理者额外看到配置的资源摘要（模型/知识库/Skills）和发布/停用/编辑/删除草稿。
  - 非管理者不展示管理员配置的 KB/Skills 名称，只显示能力标签、边界和「运行时资源按你的权限解析」，避免泄露私有资源存在性。后端在 `can_manage=false` 时从数字员工详情、通用列表和通用详情移除 `config_json` 与 `configurable_items`。
- 空态、加载态、错误态；发布/停用/删除后重新拉取列表。

编辑：

- 扩展现有 `AgentEditModal`，增加 `kind` / `digitalEmployeeProfile` 入参与「岗位信息」分组；创建/更新员工时调用 `digitalEmployeeApi`，普通 Agent 路径不变。
- 运行时配置继续复用 `AgentRuntimeConfigForm`；共享范围继续复用 `ShareConfigForm`。
- 管理员创建员工时默认 `ChatbotAgent`；后端选择器不允许 `SubAgentBackend`。

对话入口：

- 复用 `router.push({ name: 'AgentComp', query: { agent_id: employee.slug } })`。
- 不新增聊天页；员工与普通 Agent 共用 `AgentView` / `AgentChatComponent`。
- 如果当前已有绑定线程，沿用现有提示「当前对话已绑定智能体，请新建对话后切换」。

API 模块：

- 新增 `web/src/apis/digital_employee_api.js`：`listDigitalEmployees`、`getDigitalEmployee`、`createDigitalEmployee`、`updateDigitalEmployee`、`publishDigitalEmployee`、`disableDigitalEmployee`、`deleteDigitalEmployee`。
- 页面本地状态即可，不新增 Pinia store；员工列表规模小，避免多一份缓存真相。

### 5.7 失败与边界

- 草稿不可对话：列表不展示给普通用户；`GET /api/agent/{slug}` 对普通用户 404、对管理者 200；`create_thread_view` 与 `submit_agent_request` 对管理者 409、对普通用户 404。
- 停用后：新 Run/新 resume/人工 continue 409；已排队请求在派发时被拒绝，不产生新 Run；历史线程仍可打开（`GET /api/agent/{slug}` 200、历史 200）；聊天选择器不再出现该员工（`GET /api/agent` 过滤）。
- 已绑定停用员工的定时任务/IM Bot：新派发/新消息 409；不自动解绑或停用，owner 需要重新选择员工。P1 不静默跳过。
- 删除草稿：先检查 `Conversation.agent_id`、`ScheduledAgentJob.agent_slug`、`IMBotConfig.agent_slug`；有引用 409；无引用才硬删除 Agent 行。草稿对普通用户的 `/api/agent/{slug}/memories` 也返回 404。
- 状态迁移：`disable` 只接受 active；草稿停用返回 409。锁定读取使用 `populate_existing=True`，避免 identity map 旧快照绕过 active 更新校验或误删已发布员工。
- 重名：`name` 可重复；`slug` 由 `_unique_slug` 保证唯一。
- 预置冲突：员工 slug 与 preset slug 冲突时自动加后缀；预置本身不改成员工。
- 并发发布/停用：服务在事务内对 Agent 行加锁后再写状态，避免两个管理员互相覆盖。
- 配置引用不可见资源：沿用 `prepare_agent_config_write` 的过滤；发布不会把创建者私有的 KB/Skills 权限转给其他人。
- 停用时已有运行中的 Run：不强制中断；Run 按现有 lease/取消链路完成。产品文案不承诺「停用即停止」。
- 通用 Agent 路由绕过：`PUT/DELETE /api/agent/{digital_employee}` 返回 400；测试必须覆盖。
- `GET /api/agent` 的调用方：聊天选择器需要 active 员工；Agent 管理页传 `kind=agent`；IM Bot 和定时任务允许 active 员工（按调用身份授权）。实现时审计所有 `getAgents()` 调用方。

### 5.8 验收与证据

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 既有 Agent 迁移后仍为普通智能体，迁移可重复执行 | 迁移把既有行改成员工或重复加列失败 | `models_business.py` / `manager.py` | `pytest test/integration/api/test_digital_employee_migration.py`：1 passed | 迁移后既有行 `kind != 'agent'` 时测试变红 | Passed |
| 管理员创建草稿后，普通员工列表不可见，管理者可见 | 草稿泄露到普通员工或管理者看不到 | `digital_employee_service.list_digital_employees` / `digital_employee_is_visible` | `test_digital_employee_api.py`：普通用户列表/详情/线程 404，管理者详情 200、管理者建线程 409 | 移除状态过滤后测试变红 | Passed |
| 发布后，共享范围内员工可见 | 发布后仍 404，或越权用户可见 | `digital_employee_service.publish_digital_employee` + `share_config` | 真实 HTTP：创建→发布→普通用户列表/详情 200；`kind=agent` 排除员工，`kind=digital_employee` 包含员工 | 把 `share_config` 改成 user 精确列表后越权用户仍可见则变红 | Passed |
| 发布后的员工能通过真实 worker 完成 Run | 只能建线程，Run 不返回员工配置的输出 | `digital_employee_is_runnable` + worker | E2E `test_digital_employee_e2e.py` 已写；当前 replay 网关不可用，测试明确 skip | 网关可用时 Run 未 completed 或 output 不等于 `DETERMINISTIC_AGENT_E2E_OK` 则失败 | Not run |
| 停用后新 Run/新线程/排队派发被拒绝，历史仍可读 | 停用员工仍能执行，或历史丢失 | `digital_employee_is_runnable` + request/run/queue guard | integration：停用→新 Run 409、新线程 409、人工 continue 409；直接造 queued 请求后 `dispatch_next_request` 返回 None，请求变 `rejected` 且无 Run；`GET /api/agent/{slug}` 200；历史 GET 200 | 移除 guard 后新请求或排队派发成功则变红 | Passed |
| 非管理员不能创建/发布/停用；无 manage 权限的管理员也不能 | 前端隐藏被绕过，后端放行 | `digital_employee_router` + `user_can_manage_agent` | integration：普通用户创建 403；只有 read 权限的 admin 更新/发布/停用/删除全部 403 | 去掉角色/权限检查后测试变红 | Passed |
| 通用 Agent 接口不能修改/删除/复制员工 | `PUT/DELETE/duplicate /api/agent/{employee}` 成功 | `agent_router` 隔离规则 | integration 三项均 400 | 移除隔离后员工被通用接口改名/删除则变红 | Passed |
| 并发状态迁移基于数据库最新行 | 预加载旧状态后跳过 active 校验或误删已发布员工 | `AgentRepository.get_by_slug_for_update` | integration：两个 session 中先预加载 draft，另一个 session 改为 active，再锁定读取得到 active；unit 断言 `populate_existing=True` | 去掉 `populate_existing` 后锁定读取仍返回旧 draft 则变红 | Passed |
| 发布校验和 active 更新校验拒绝缺字段和能力为空 | 空岗位/空能力员工被发布或编辑成非法 | `digital_employee_service.validate_for_publish` | unit 参数化测试；integration：active 员工空岗位/空能力 422，回读原档案未变 | 删除校验后空档案发布/active 更新成功则变红 | Passed |
| 草稿不能建线程/定时任务/IM Bot；删除有引用的草稿被拒绝 | 草稿产生引用后被硬删除 | `create_thread_view`、`scheduled_agent_service`、IM Bot 配置 | integration：普通用户草稿线程 404、管理者草稿线程 409；定时任务 409、IM Bot 409；分别直接造 conversation / scheduled job / IM Bot 引用后删除均 409，移除引用后 200；草稿停用 409；草稿 memories 普通用户 404、管理者 200 | 移除 guard 后草稿线程/任务/Bot 创建成功则变红 | Passed |
| 非管理者详情不泄露私有资源标识 | `config_json`/`configurable_items` 出现在普通用户响应 | `digital_employee_router`、`agent_router._serialize_agent` | integration：普通用户的数字员工详情、通用列表、通用详情都没有两个字段；管理者详情仍有 | 移除序列化剥离后测试变红 | Passed |
| 前端页面只展示真实数据，管理员操作后状态同步 | 页面仍显示静态卡片或状态不刷新 | `DigitalEmployeeView.vue`、`AgentEditModal.vue`、`AgentView.vue`、`utils/digitalEmployee.js` | `eslint . --max-warnings=0`、`node --test`（471 passed）、`vite build`；`normalizeDigitalEmployee` 用真实 `{id: 7, slug}` 和缺 slug payload 断言规范 ID | 非管理者点击编辑后仍打开通用弹窗、或员工保存仍请求数字 id、或页面不刷新则失败；浏览器级检查未执行 | Partially passed：构建/单测通过，真实浏览器交互未执行 |

最低证据：

- Unit：`test/unit` 2678 passed, 58 skipped；覆盖档案归一化、发布校验、active 更新校验、状态迁移、可见性/运行守卫、锁刷新和工具元数据降级。
- Integration：数字员工 API + 迁移共 10 passed；覆盖真实 PostgreSQL、真实 HTTP、权限、通用接口隔离、迁移、草稿引用、队列派发和序列化剥离。
- E2E：已写 `test/e2e/test_digital_employee_e2e.py`；当前 replay 网关不可用，未执行。
- Frontend：lint（0 warning）、unit（471 passed）、build 通过；真实浏览器交互未执行。
- 工程契约：`python3 scripts/verify_engineering_contracts.py` 通过（55 decisions / 5 workflows / 4 agents files / 207 docs / 30 routers / 268 web sources）；`python3 -m unittest scripts.test_verify_engineering_contracts` 63 passed；`git diff --check` 通过。

### 5.9 实施结果与未验证范围

- 员工详情响应：创建/更新/发布/停用使用 `include_resources=False`；详情使用 `include_resources=True`，避免变更接口因资源摘要解析失败而回滚。
- 状态可读性：员工目录只向普通用户展示 active；停用员工的通用详情和历史可读；草稿的通用详情仅管理者可读。
- 前端：`/digital-employees` 页面、导航、列表/详情抽屉、发布/停用/删除草稿、`AgentEditModal` 岗位信息分组和员工保存分支已实现；智能体管理页改为 `GET /api/agent?kind=agent`，避免员工混入通用管理。
- 停用后的队列：queued 请求在派发阶段被置为 `rejected` 并记录错误；人工 continue 返回 409。已存在且已投递的 pending Run 不强制中断。
- 未验证：deterministic E2E 真实 Run 未执行；真实浏览器中的创建/编辑/发布/停用/去对话路径未执行。这两项不写成通过。
- 修复的既有缺陷：工具元数据首次加载时，`ToolRuntime` 参数无法生成 JSON Schema 会让详情接口 500；现在只跳过不可序列化的参数列表，不阻断工具发现。

## 6. P2 / P3 接口边界

### 6.1 P2 待办事项

P2 已实现，不改变 P1 的员工模型。它新增一个运行时投影：

```text
WorkItem
  key: "run:<run_id>" | "handoff:<id>"
  source_type: "agent_run" | "handoff"
  source_id
  title
  agent_slug / agent_name
  initiator_type: "human" | "digital_employee"
  initiator_uid / initiator_name
  status: "queued" | "running" | "waiting_approval" | "completed" | "failed" | "cancelled"
  approval_kind: "tool" | "question" | null
  approval_summary: string | null
  thread_id / run_id
  created_at / updated_at
  unread: bool
```

- 事实来源：每线程最新顶层 Run；排除 subagent、agent_call、agent_evaluation；`source=scheduled_agent` 的发起方显示为数字员工。
- `waiting_approval`：最新 Run 为 `interrupted` 且 `error_type` 为 `human_approval_required` / `ask_user_question_required`。
- 审批动作不在待办页复制：详情抽屉提供「去对话处理」，跳转现有会话，由 `HumanApprovalModal` 完成 resume。
- 新增 `work_item_views(uid, item_key, viewed_at)` 已读表，`(uid,item_key)` 唯一；不新增待办状态表。
- P2 API：`GET /api/workbench/todos`、`GET /api/workbench/todos/{key}`、`POST /api/workbench/todos/{key}/viewed`、`GET /api/workbench/todos/summary`。
- P3 的转交单以 `source_type=handoff` 接入同一 DTO；P2 已预留 key 解析、DTO 和前端动作，但尚未产生 handoff 记录。
- P2 实现决策、状态映射、已读粒度和验证见 [待办收件箱接入企业智能工作台](../../develop-guides/decisions/implemented/2026-10-26-workbench-todos-p2.md)。

### 6.2 P3 老板分身

P3 已实现，复用 P1 的 `kind` 扩展点，增加 `founder_twin`：

- 经验条目：`experience_entries`（`twin_slug`、`code`、`title`、`situation`、`judgment`、`boundary`、`source_type`、`source_ref`、`source_excerpt`、`status`、`confirmed_by/at`、`embedding`、`citation_count`）。
- 咨询记录：`experience_consultations`（`twin_slug`、`uid`、`thread_id`、`run_id`、`question`、`answer_message_id`、`cited_entry_ids`、`confidence`、`handoff_id`）。
- 转交单：`handoff_requests`（`twin_slug`、`uid`、`question`、`assigned_to_uid`、`status`、`answer`、`created_at/answered_at`）。
- 检索：只加载 `status=confirmed` 的条目；embedding 在条目写入/确认时生成；查询用现有 embedding 模型 + 进程内余弦；2,000 条上限写入文档。
- 答案：`submit_consultation_answer` 校验引用条目已确认、属于该分身、未被标失效；没有引用时只能创建转交单，不能给结论。P3 必须设计「未调用校验工具就结束」的 guard，并给出负向测试。
- 页面：`/founder-twin`，包含咨询、经验库、待确认条目、来源和转交；普通员工只读咨询和自己的记录，老板/管理员管理条目。
- 实现决策、状态机、guard、转交可见范围和验证见 [老板分身与经验引用约束](../../develop-guides/decisions/implemented/2026-10-26-founder-twin-p3.md)。

## 7. 交给实现者的部分

- 前端视觉细节、响应式断点、卡片/抽屉的具体布局和文案。
- 测试 fixture 的具体构造；E2E 复用哪条 deterministic 模型路径。
- 错误提示的具体措辞（语义已在 5.4 固定）。
- 员工列表是否需要分页；P1 按不分页实现，超过数百条再提案。
- P2/P3 的详细设计、迁移和索引。
- 是否在 P1 提供「草稿预览对话」；当前设计为不提供，先发布再对话。

## 8. Builder 检查决议

- **内部命名**：使用 `digital_employee`，避免和 SaaS 人类员工冲突。
- **停用历史**：`GET /api/agent/{slug}` 对 active/disabled 可读；列表只出现 active；新 Run 409。
- **active 更新**：每次更新后重新校验档案，不能改空。
- **草稿引用**：线程、定时任务、IM Bot 绑定都加 runnable guard；删除草稿仍有引用检查。
- **调用面**：不限制在网页；员工是 Agent，权限跟随调用身份（网页/API Key/Bot/定时任务 owner）；停用导致显式失败，不自动解绑。
- **资源摘要**：非管理者不显示配置的 KB/Skills 名称，避免泄露私有资源。
- **部署形态**：已确认为 A 单企业自建；SaaS 共享实例另行提案。

## 9. 风险与待确认

1. **数据权限语义（产品文案必须承认）**：数字员工跟随调用身份的数据权限，不提供独立服务账号。如果产品要求「财务核查官」能看财务数据而调用者看不到，P1 的模型不成立，需要单独设计服务身份、授权和审计。
2. **单企业假设已确认，但仍是发布边界**：默认 `global` 共享只在单企业自建成立；如果未来改为 SaaS 共享实例，必须新增 `Agent.tenant_id`、租户管理身份和租户范围，不能直接复用 P1。
3. **通用 Agent 目录变化**：active 员工会出现在聊天选择器和 IM Bot/定时任务的 Agent 列表；这是有意的，调用身份授权。只想要普通智能体的调用方需要传 `kind=agent`。实现时审计所有 `getAgents()` 调用方。
4. **`digital_employee_profile.status` 在 JSON 中**：列表过滤在服务层完成，规模大时退化为全量加载；P1 接受几十到几百的规模。
5. **扩展 `AgentEditModal`**：这是现有高频组件，员工分组可能影响普通 Agent 创建；需要回归普通 Agent 的创建/编辑，并保持运行时配置不变。
6. **停用不等于停止**：运行中的 Run 会继续；产品文案不能承诺即时停止。若需要强制停止，走现有 cancel 链路，P1 不新增语义。
