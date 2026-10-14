# 场景模板与发现性（内置模板 + 开箱体验）

状态：implemented
类型：feature
Owner：backend/package/yuxi/agents/presets/__init__.py

跨 Owner 分工：`backend/package/yuxi/agents/presets/` 拥有模板元数据定义（scene/icon/example_questions/tips 随版本发布）；`backend/package/yuxi/services/agent_config_service.py` 拥有 scene-templates 聚合与 duplicate 用例；`backend/server/routers/agent_router.py` 拥有两个端点的薄路由；`backend/package/yuxi/repositories/agent_repository.py` 拥有数字主键读取与 `is_template` 派生；`web/src/components/AgentChatComponent.vue` 与 `web/src/components/SceneCatalog.vue` 拥有开始屏目录、示例问题与 `/help` 前端拦截；`web/src/components/model-management/AgentManagePanel.vue` 拥有"另存为副本"入口。

## 问题

公司内部采用率受两个主因制约：入口不在工作流（钉钉接入未立项，本期不解决）、不知道能干什么。开始屏只有随机欢迎语与输入框（`AgentChatComponent.vue` start-screen），无模板/示例引导；预置 Agent（`agents/presets/`）以普通智能体形态混在选择器里，缺少场景分组、图标与示例问题；全仓无 duplicate/clone 端点，用户想在模板基础上改配置无低风险路径；web 聊天不解析 slash 命令（仅外部 channel 支持 `/state`、`/approve`），无 `/help` 引导。

## 决策

### 实现方案

**模板即内置 Agent（沿用既有 preset 机制，元数据不入库）**

- `AgentPreset` 扩展字段 `scene`/`icon`/`example_questions`/`tips`；`SCENES` 有序元组定义 3 个场景（`general`、`writing`、`research`）与中文名，`scene` 不在 `SCENES` 中时发现阶段直接抛错，不静默跳过。`scene` 非空即入场景目录，不设 `scene` 的子 Agent 预置自动排除。5 个场景模板为 `default-chatbot`（general）、`writing-assistant` 与 `weekly-report`（writing）、`deep-research` 与 `data-interpreter`（research）。序列化时 `is_template` 由 preset slug 集合与 `is_subagent` 派生，不新增 DB 字段。元数据随版本发布（已接受"运营改文案需发版"的取舍）。
- **`GET /api/agent/scene-templates`**（登录即可，声明在 `GET /api/agent/{agent_id}` 之前避免被当 slug 吞掉）：服务层 `get_scene_templates` 按 slug join 已种子化的 preset agent 行与代码元数据，按 `SCENES` 顺序分组返回 `scenes`（`agent_id` 取真实行主键），未种子化与空场景预置不产出条目，并顺带返回 `daily_tip`。
- **`daily_tip` 按 UTC 日历日轮转**：tip 池按 `SCENES × 预置 × tips` 展平，索引为 `(当日 - 2026-01-01).days % 池长`，当日取 `datetime.now(UTC).date()`——用本地日界会让不同时区实例在同一时刻产出不同 tip。池为空返回 `None`，前端不渲染。
- **`POST /api/agent/{agent_id}/duplicate`**（用例在 `agent_config_service`）：路径参数为数字主键（`get_by_id` 解析 `int`，无法解析按不存在处理）；源不可见返回 404（`user_can_access_agent`，不泄露存在性），`is_subagent` 源返回 400；`config_json` 深拷贝为快照式复制，资源字段经 `prepare_agent_config_write` 以当前用户重新解析授权；`share_config` 为 v2 用户级读写范围、剔除 `is_default`、名称 `"{原名} 副本"`，响应与创建端点同形。副本即普通用户 agent 可自由编辑——模板保护原配置，副本放开定制。

**开始屏场景目录与示例问题**

- `SceneCatalog.vue` 挂在开始屏欢迎语与输入框之间，仅新会话渲染，随既有 start-screen 生命周期消失；渲染 `scenes` 分组卡片（icon/名称/描述）与 `daily_tip` 横幅，无模板数据时不渲染。
- 点卡与每日一技横幅均为选中而非开跑：`emit('select', agent_id)` 走既有 `selectAgent` 链路并聚焦输入框。
- 当前选中 agent 为模板时，输入框上方展示 `example_questions` chips；点击填入输入框不自动发送（沿用 KB `QuerySection` 交互惯例）。用户自建 Agent 无此能力（非目标）。
- 管理列表对 global 可见非自有 agent 显示"模板"徽标与"另存为我的副本"操作，调 `duplicateAgent(agent.id)`（数字主键），副本成功后跳转新 agent 会话。

**`/help` 与每日一技（零定时基建，预留钉钉）**

- `/help` 由前端 `isHelpCommand` 拦截：`trim` 后恰为 `/help`（无参数）→ 不发 run，本地渲染不持久化的系统卡片（能力简述 + 模板推荐，点击即选中）；其他 `/xxx`（含 `/help extra`、`/HELP`）原样发送，零行为变化；语义与后端 `channel_command_service` 对齐，为 CLI/钉钉通道复用同一数据源留口。
- 每日一技由服务层只读函数 `pick_daily_tip` 确定性产出（含来源模板），scene-templates 端点顺带返回，开始屏目录顶部展示。将来钉钉推送（方向 A 独立立项后）读同一函数；本期无外发、无调度改动。

## 替代方案

- 独立 `scene_template` 数据模块（DB 表 + 管理端点）：为"管理员可配置"预留，但已选内置只读，属提前建设，否决。
- 纯前端实现（硬编码目录、前端 GET+POST 拼装副本）：零后端改动，但元数据与 preset 脱节会漂移、副本无后端校验与授权 Owner（违反权限在后端边界执行的系统事实），否决。
- 定时任务生成每日一技内容：tips 是只读代码内容，无需生成，引入调度与状态纯属多余，否决。

## 后果

- 运营改场景文案、示例问题与 tips 需发版（元数据在代码）；后续若诉求变强，可在不改端点契约的前提下演进为管理员配置（本记录替代方案 B 的延后形态）。
- 每日一技外发依赖方向 A（钉钉接入）独立立项；本期站内闭环，数据源已收敛为服务层只读函数，接口不因延后而返工。池长度变化会改变当日 tip 内容，属内容更新的预期行为。
- 副本快照复制 skills/knowledges/mcps 引用，源或目标后续被删除产生的悬空引用与既有 agent 编辑行为一致；副本与源此后互不影响。
- integration（真实 HTTP + PostgreSQL：scene-templates 分组与种子行一致、duplicate 正/负向）在本机未执行：worktree 无 `.env`，`docker compose` 插值即报 `API_KEY_DERIVATION_SECRET`/`SANDBOX_PROVISIONER_TOKEN` 缺失，且 docker daemon 未启动；本地 fallback 在 conftest setup 即 `RuntimeError: SANDBOX_PROVISIONER_TOKEN is required for integration sandbox cleanup`（本机 5050 无 API、`test/.env.test` 不存在）。服务层与路由逻辑由 unit 断言，真实链路语义未验证，结果记 `Not run`/`Inspected` 而非通过。
- 真实浏览器手工验证（开始屏目录与 chips/help 卡片视觉、点选切 agent）未执行：web unit 只证明组件纯逻辑与源结构，build 只证明编译通过。
- 开始屏是高频首见界面，模板卡改变视觉首屏，仅以最小改动挂载（欢迎语与输入框之间的既有 dock 区域）；布局回归未经真实页面验收。

## 验证

本机（host uv，无 docker 服务栈）实际执行的命令与结果：

- `cd backend && uv run --group test pytest test/unit -m "not slow" -q`：2653 passed, 6 warnings in 37.85s。
- `uv run --group test pytest test/unit/agents/test_builtin_discovery.py test/unit/repositories/test_agent_repository_deep_research.py test/unit/services/test_agent_config_service.py test/unit/services/test_scene_templates.py -q`：31 passed in 3.46s（scene 元数据/未知 scene 拒绝、种子幂等、分组与 `daily_tip` 确定性及 UTC、duplicate 正反向服务层）。
- `cd web && npm run test:unit`：437 pass / 0 fail；`npm run lint:check` 退出 0；`npm run build` 成功。
- `cd web && node --test --test-concurrency=1 test/unit/sceneCatalog.test.js test/unit/helpCommand.test.js test/unit/duplicateAgent.test.js`：8 pass / 0 fail。
- integration docker 策略：`docker compose ps` → `error while interpolating services.api.environment.API_KEY_DERIVATION_SECRET: required variable API_KEY_DERIVATION_SECRET is missing a value`（无 `.env`）；`docker ps` → `failed to connect to the docker API at unix:///Users/wanghua/.docker/run/docker.sock ... daemon is running`（daemon 未启动）。
- integration 本地策略：`cd backend && uv run --group test pytest test/integration/api/test_scene_templates_api.py -v` → setup 即 `RuntimeError: SANDBOX_PROVISIONER_TOKEN is required for integration sandbox cleanup`（`test/integration/conftest.py`）；`curl http://localhost:5050/api/system/health` 无响应，`test/.env.test` 不存在。
- `python3 scripts/verify_engineering_contracts.py`：工程信任检查通过；`python3 -m unittest scripts.test_verify_engineering_contracts`：63 tests OK。
- `cd docs && npm run build`：build complete in 4.17s（先 `npm install` 安装本机缺失依赖）。
- `git diff --check`：无输出。

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| preset 元数据扩展后发现机制仍幂等，scene 非空入目录、空 scene 排除 | 预置失联或子 Agent 误入目录 | `agents/presets/__init__.py` | unit：`test_builtin_discovery.py` + `test_agent_repository_deep_research.py` 等 31 passed；全量 2653 passed | 重复 slug 仍被拒绝；未知 scene 报错不静默跳过 | Passed |
| scene-templates 端点按 scene 分组排序且 agent_id 与种子行一致 | 分组错乱或 agent_id 悬空 | `agent_config_service.py` | unit：`test_get_scene_templates_groups_by_scene` passed（monkeypatch repository）；HTTP+PostgreSQL integration 本机不可用，见「验证」 | 不存在 slug 不产出条目 | Inspected |
| duplicate 正向：副本私有归本人、config 快照完整、名称后缀正确 | 副本继承 global 或丢配置 | `agent_config_service.py` | unit：`test_duplicate_agent_creates_private_copy_for_current_user` passed（断言 share_config 用户级、深拷贝、` 副本` 后缀、`is_default=False`）；HTTP+PostgreSQL 回读本机不可用 | 副本不继承 `is_default` | Inspected |
| duplicate 负向：无权源 404、subagent 400 | 越权复制或子 Agent 泄露 | `agent_router.py` + service | unit：`test_duplicate_agent_missing_source_returns_404` / `inaccessible_source_returns_404` / `subagent_returns_400` 共 3 passed；真实 HTTP 负向用例本机不可用 | 跨用户私有源 404 | Inspected |
| 场景目录渲染分组与点选切 agent | 选不中或误开跑 | `SceneCatalog.vue` | web unit：`sceneCatalog.test.js` 渲染分组与点卡 `emit select` passed；真实页面点选手工验证 Not run | 无模板数据时目录不渲染报错 | Passed |
| 示例问题 chips 点击填入不自动发送 | 误触发提问 | `AgentChatComponent.vue` | web unit：`sceneCatalog.test.js`「仅填充示例问题，不触发发送」passed | 无 example_questions 不渲染 chips | Passed |
| `/help` 拦截渲染、其他 slash 原样发送 | 误吞正常消息或漏拦截 | `AgentChatComponent.vue` + `commandInput.js` | web unit：`helpCommand.test.js` 2 passed + `duplicateAgent.test.js` 发送派发前拦截 passed | `/help extra`、`/HELP` 原样发送 | Passed |
| 每日一技确定性（固定日期→固定 tips）且展示可点击选中 | 多实例不一致或不可交互 | 服务层 `pick_daily_tip` + 前端 | unit：`test_scene_templates.py` 锚点确定性、空池 None、UTC 日期 passed；web unit：横幅空/非空渲染与点选 passed | 空 tips 池不产 daily_tip | Passed |
| 既有行为零回归：preset 种子幂等、原会话发起链路不变 | 升级破坏既有智能体 | 既有 lifespan/`ensure_preset`/会话链路 | 后端全量 unit 2653 passed；web 全量 437 pass | 升级重复种子不产生重复行；重复 slug 拒绝 | Passed |
| 全量命令通过（后端 unit、web unit/lint/build、contracts、docs build） | 回归破坏 | 各命令 Owner | 后端 2653 passed；web 437 pass + lint 退出 0 + build 成功；contracts 通过 + unittest 63 OK；docs build complete 4.17s；`git diff --check` 无输出 | 正向命令 | Passed |
| 真实链路：scene-templates 与 duplicate 的真实 HTTP + PostgreSQL 语义 | 路由顺序、种子行 join、越权在真实链路上未兑现 | integration 测试（`test/integration/api/`） | 本机无 `.env` 且 docker daemon 未运行，compose 不可用；本地 fallback 缺 `SANDBOX_PROVISIONER_TOKEN`，见「验证」 | 路由被 `{agent_id}` 吞掉、跨用户 404 失效 | Not run |
| 真实浏览器：开始屏目录/chips/`/help` 卡片视觉与交互 | 视觉回归或交互在真实页面失效 | `AgentChatComponent.vue` 开始屏 | 本机未启动前后端联调环境，未执行手工验证 | 有/无模板数据两种首屏状态 | Not run |
