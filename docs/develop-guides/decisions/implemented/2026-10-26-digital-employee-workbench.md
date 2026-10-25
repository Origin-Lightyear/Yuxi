# 数字员工接入企业智能工作台

状态：implemented
类型：feature
Owner：backend/package/yuxi/repositories/agent_repository.py

## 问题

原型 `大力水手Agent-企业智能工作台.html` 定义了「数字员工」的产品形态：按岗位组织可复用的企业 AI 能力，每个员工有职责、能力、边界、验收和发布状态。当前 Yuxi 只有通用 `Agent`，没有岗位档案、发布状态和员工列表；管理员无法把某个 Agent 作为「在岗数字员工」发布给员工，普通员工也没有一个只包含在岗员工的产品入口。

直接复用预设 Agent 不成立：`ensure_preset` 只在首次启动落库，之后不覆盖用户修改；非 builtin 预设只有超级管理员可管理，无法让企业管理员在运行时创建、发布和停用。另起一张员工表则会复制 Agent 的身份、权限和对话入口，形成双写。

本提案只批准 P1「数字员工」，部署形态为**单企业自建**。P2「待办」和 P3「老板分身」在同一设计文档中只定义边界，实施前各自另立提案；SaaS 多租户共享实例不在 P1。

设计文档：`docs/superpowers/specs/2026-10-26-enterprise-workbench-design.md`

## 决策

### 实现方案

在 `agents` 表增加两列：`kind`（`agent|digital_employee`，默认 `agent`）和 `digital_employee_profile`（JSON，默认 `{}`）。内部统一使用 `digital_employee`，避免和仓库已有的 SaaS 人类员工语义冲突；用户界面文案仍是「数字员工」。

数字员工仍是 `Agent`，复用现有 `share_config`、`config_json`、对话/Run 链路、知识库/Skills/MCP 和前端聊天入口；不新增执行后端，也不新增独立员工表。

新增 `backend/package/yuxi/services/digital_employee_service.py`，拥有档案归一化、发布校验、active 更新校验、状态迁移、可见性和运行守卫。状态机为 `draft -> active -> disabled`，`disabled` 可重新发布；草稿可删除，已发布/停用只能停用。新增 `backend/server/routers/digital_employee_router.py`（`/api/digital-employees`）提供列表、详情、创建、更新、发布、停用、删除草稿。创建仅限 `admin`/`superadmin`，更新/发布/停用/删除要求现有 `user_can_manage_agent`。

通用 Agent 接口保持普通智能体语义：`POST/PUT/DELETE /api/agent` 不能创建或修改员工；`GET /api/agent` 是运行时目录，排除非 active 员工；`GET /api/agent?kind=agent` 供智能体管理页使用。`GET /api/agent/{slug}` 对 active/disabled 员工可读，保证停用后会话历史还能加载；draft 对非管理者返回 404，管理者可读以便编辑加载。运行时在 `submit_agent_request`、`create_resume_run_view`、`create_thread_view`、定时任务校验和 IM Bot 配置处调用 `digital_employee_is_runnable`：非 active 员工拒绝新请求/新 resume/新线程/新任务/新绑定，但不影响历史读取和已在运行的 Run。

前端新增 `/digital-employees` 页面与导航项，复用 `InfoCard`、`PageHeader`、`AgentView` 对话入口；扩展现有 `AgentEditModal` 增加「岗位信息」分组和员工 API 分支，普通 Agent 路径不变。页面只展示后端返回的真实数据，不做静态演示。非管理者详情不展示管理员配置的私有 KB/Skills 名称；数字员工详情和通用 Agent 详情在 `can_manage=false` 时都移除 `config_json` 与 `configurable_items`。创建/更新/发布/停用响应使用 `include_resources=False`，详情仍返回资源摘要。

安全边界：员工的 `share_config` 决定谁可见；档案里的「部门」只是展示字段。员工可以出现在现有所有 Agent 调用面，数据权限按**调用身份**（网页用户、API Key owner、IM Bot owner、定时任务 owner）解析，不冒充最终提问人；P1 不引入服务账号、租户重构或越权数据访问。

## 替代方案

- **新建 `digital_employees` 表与 Agent 一对一**：领域边界更干净，但员工身份、权限、对话入口、Run 关联都要双写或 join；P1 的收益不足以抵消第二套身份。
- **只扩展 preset**：不能运行时创建/发布，非超级管理员无法管理，无法形成产品闭环。
- **把员工档案放进 `config_json.context`**：会被 `filter_declared_config` 按 `BaseContext` 字段丢弃；放进顶层又缺少校验和查询语义。独立列更明确。
- **新增 `digital_employee_status` 独立列**：查询更直接，但 P1 员工规模小，状态在服务层过滤足够；等规模或分页需要时再提案加列，避免过早增加状态列和同步点。
- **让数字员工拥有独立服务账号/更宽数据权限**：更接近「财务核查官能看财务数据」的产品直觉，但会改变现有「权限按调用身份执行」的安全不变量，需要服务身份、授权、审计和隔离设计。P1 明确不做；如果产品要求这一点，本提案必须重开。
- **把员工限制在网页聊天**：会排除 IM、API Key 和定时任务，但这些面已经按调用身份授权，限制它们没有安全收益，反而削弱数字员工的自动化价值。P1 不限制调用面。

## 后果

- 数字员工成为 `Agent` 的受管类型，普通智能体列表、权限、对话和 Run 链路不需要第二套实现。
- active 员工会出现在聊天选择器、API Key、IM Bot 和定时任务的 Agent 列表；这些入口的数据权限跟随调用身份，不提供员工独立服务账号。
- 员工状态存在 JSON 档案中，列表在服务层过滤；P1 接受几十到几百的规模，超过后需要重新提案分页或独立状态列。
- 停用只阻止新请求、新 resume、新线程、新定时任务和新 IM Bot 绑定，不中断已在运行的 Run。
- 非管理者读取员工详情时看不到私有资源标识；管理者仍可通过详情加载编辑器所需资源。
- 通用 `/api/agent` 详情首次加载工具元数据时曾因 `ToolRuntime` 参数不可生成 JSON Schema 而失败；本实现跳过不可序列化的运行时参数，避免首个详情请求 500。

## 验证

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| 既有 Agent 迁移后仍为普通智能体，迁移可重复执行 | 既有行被改成员工，或迁移重复执行失败 | `models_business.py`、`manager.py` | `docker compose exec -T api uv run --no-sync --group test pytest test/integration/api/test_digital_employee_migration.py -q -p no:cacheprovider`：1 passed；回读既有行 `kind='agent'` | 迁移后既有行 `kind != 'agent'` 时测试变红 | Passed |
| 管理员创建草稿后，普通员工不可见，管理者可见 | 草稿泄露或管理者看不到 | `digital_employee_service.list_digital_employees`、`digital_employee_is_visible` | `test_digital_employee_api.py`：普通用户列表/详情/线程 404，管理者详情 200、管理者建线程 409；7 passed | 移除状态过滤后普通用户可见则变红 | Passed |
| 发布后共享范围内员工可见 | 发布后仍 404，或越权用户可见 | `publish_digital_employee`、`share_config` | 同一 integration：发布后普通用户列表/详情 200，`can_manage=false`；`GET /api/agent?kind=agent` 不含员工，`kind=digital_employee` 含员工 | 把共享范围改成 user 精确列表后越权用户仍可见则变红 | Passed |
| 发布后的员工能通过真实 worker 完成 Run | 员工只能建线程，Run 不返回员工配置的输出 | `digital_employee_is_runnable`、`Agent` 配置解析、worker | E2E `test_digital_employee_e2e.py` 已写，但 replay 网关 `http://localhost:8765/health` 不可用，pytest 明确 skip | 网关可用时跑同一 E2E；若 Run 未 completed 或 output 不等于 `DETERMINISTIC_AGENT_E2E_OK` 则失败 | Not run |
| 停用后新 Run/新线程被拒绝，历史仍可读 | 停用员工仍能执行，或历史丢失 | `digital_employee_is_runnable`、`agent_request_service`、`agent_run_service` | integration：停用→新 Run 409、新线程 409；`GET /api/agent/{slug}` 200、历史 GET 200 | 移除 guard 后新请求成功则变红 | Passed |
| 非管理员不能创建/发布/停用；无 manage 权限的管理员也不能 | 前端隐藏被绕过，后端放行 | `digital_employee_router`、`user_can_manage_agent` | integration：普通用户创建 403；只有 read 权限的 admin 对更新/发布/停用/删除全部 403 | 去掉角色/权限检查后测试变红 | Passed |
| 通用 Agent 接口不能修改/删除/复制员工 | `PUT/DELETE/duplicate /api/agent/{employee}` 成功 | `agent_router` 隔离规则 | integration 三项均 400 | 移除隔离后员工被通用接口改名/删除则变红 | Passed |
| 发布校验和 active 更新校验拒绝缺字段和能力为空的档案 | 空岗位/空能力员工被发布或编辑成非法 | `validate_for_publish` | unit 参数化测试；integration：active 员工 `position=""`、`capabilities=[]` 均 422，回读原档案未变 | 删除校验后空档案发布/active 更新成功则变红 | Passed |
| 草稿不能建线程/定时任务/IM Bot；删除有引用的草稿被拒绝 | 草稿产生引用后被硬删除 | `create_thread_view`、`scheduled_agent_service`、IM Bot 配置 | integration：普通用户草稿线程 404、管理者草稿线程 409；草稿定时任务 409、草稿 IM Bot 409；直接插入 Bot 引用后删除草稿 409，移除引用后 200 | 移除 guard 后草稿线程/任务/Bot 创建成功则变红 | Passed |
| 非管理者详情不泄露私有资源标识 | `config_json`/`configurable_items` 出现在普通用户响应 | `digital_employee_router`、`agent_router._serialize_agent` | integration：普通用户在数字员工详情、通用列表、通用详情都没有两个字段；管理者详情仍有 | 移除序列化剥离后测试变红 | Passed |
| 前端页面只展示真实数据，管理员操作后状态同步 | 页面仍显示静态卡片或状态不刷新 | `DigitalEmployeeView.vue`、`AgentEditModal.vue` | `eslint . --max-warnings=0` 通过；`node --test` 470 passed；`vite build` 通过 | 把接口返回替换成静态数组时页面检查失败 | Partially passed：构建与单测通过，真实浏览器交互未执行 |
| 工程契约与格式 | 文档/路由/契约清单失配或空白错误 | `scripts/verify_engineering_contracts.py`、`.gitignore` | 工程契约脚本通过（55 decisions / 5 workflows / 4 agents files / 207 docs / 30 routers / 268 web sources）；`unittest` 63 passed；`git diff --check` 通过 | 新增未登记路由或不可解析文档链接时脚本变红 | Passed |

最低证据：unit 覆盖档案/状态/可见性/运行守卫纯逻辑（`test/unit` 2677 passed, 58 skipped）；integration 覆盖真实 PostgreSQL、真实 HTTP、权限、迁移、草稿引用与序列化剥离（8 passed）；E2E 已写但当前环境未运行；前端 lint/unit/build 通过，真实页面手工交互未执行；工程契约脚本与 `git diff --check` 通过。

## 风险

- **数据权限语义**：P1 假设数字员工跟随调用身份的数据权限。若产品要求员工拥有独立数据权限，本方案的安全边界和实现范围都不成立，必须重开提案。
- **单企业假设**：已确认为单企业自建，默认 `global` 共享成立；如果未来改为 SaaS 共享实例，必须新增租户范围和租户管理身份，不能直接复用 P1。
- **通用 Agent 目录变化**：active 员工会出现在聊天选择器和 IM Bot/定时任务的 Agent 列表；这是有意的，权限按调用身份。只想要普通智能体的调用方需要 `kind=agent`。实现时审计所有 `getAgents()` 调用方。
- **`digital_employee_profile.status` 在 JSON 中**：列表过滤在服务层完成，规模大时退化为全量加载；P1 接受几十到几百的规模。
- **扩展 `AgentEditModal`**：高频组件增加员工分支可能影响普通 Agent 创建/编辑；必须回归普通 Agent 路径。
- **停用不等于停止**：运行中的 Run 继续执行，产品文案不能承诺即时停止。
