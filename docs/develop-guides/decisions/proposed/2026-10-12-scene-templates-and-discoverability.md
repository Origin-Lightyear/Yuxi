# 场景模板与发现性（内置模板 + 开箱体验）

状态：proposed
类型：feature
Owner：backend/package/yuxi/agents/presets/__init__.py

跨 Owner 分工：`backend/package/yuxi/agents/presets/` 拥有模板元数据定义（scene/icon/example_questions/tips 随版本发布）；`backend/package/yuxi/services/agent_config_service.py` 拥有 scene-templates 聚合与 duplicate 用例；`backend/server/routers/agent_router.py` 拥有两个新端点的薄路由；`web/src/components/AgentChatComponent.vue` 与新 `SceneCatalog` 组件拥有开始屏目录、示例问题与 `/help` 前端拦截；`web/src/components/model-management/AgentManagePanel.vue` 拥有"另存为副本"入口。

## 问题

公司内部采用率受两个主因制约：入口不在工作流（钉钉接入未立项，本期不解决）、不知道能干什么。现状开始屏只有随机欢迎语与输入框（`AgentChatComponent.vue` start-screen），无模板/示例引导；预置 Agent（`agents/presets/`，6 个 shipping 预置）以普通智能体形态混在选择器里，缺少场景分组、图标与示例问题；无复制智能体能力（全仓无 duplicate/clone 端点），用户想在模板基础上改配置无低风险路径；web 聊天不解析 slash 命令（仅外部 channel 支持 `/state`、`/approve`），无 `/help` 引导。

## 提案

### 实现方案

**模板即内置 Agent（沿用既有 preset 机制，元数据不入库）**

- `AgentPreset`（`backend/package/yuxi/agents/presets/__init__.py`）扩展字段：`scene: str = ""`、`icon: str = ""`、`example_questions: tuple[str, ...] = ()`、`tips: tuple[str, ...] = ()`；**`scene` 非空即入场景目录**，内置子 Agent 不设 scene 自动排除；场景顺序与中文名由代码内有序列表 `SCENES` 定义。元数据随版本发布（已接受"运营改文案需发版"的取舍）。
- **`GET /api/agent/scene-templates`**（新只读端点，登录即可）：服务层按 slug join 已种子化的 preset agent 行（lifespan `default_agents` required=True 保证存在）与代码元数据，按 scene 分组返回 `[{scene, label, templates: [{agent_id, slug, name, description, icon, example_questions}]}]`，并顺带返回 `daily_tip`（见下）。
- **`POST /api/agent/{agent_id}/duplicate`**（用例在 `agent_config_service`）：源 agent 须当前用户可见（否则 404，沿用 `user_can_access_agent`）；拒绝 `is_subagent` 源（400）；复制完整 `config_json` 为**快照式复制**，新建当前用户私有 agent（`share_config` 用创建默认，不继承 global），名称为 `"{原名} 副本"`，剔除 `is_default`。副本即普通用户 agent，可自由编辑——模板保护原配置，副本放开定制。

**开始屏场景目录与示例问题**

- 新组件 `SceneCatalog.vue` 挂入开始屏（欢迎语下方），仅新会话渲染，随既有 start-screen 生命周期消失；渲染 `scene-templates` 分组卡片（icon/名称/描述）。
- 点卡 = 选中而非开跑：走既有 `selectAgent(agent_id)` 链路并聚焦输入框。
- 当前选中 agent 为模板时，输入框上方展示 `example_questions` chips；点击**填入输入框不自动发送**（沿用 KB `QuerySection` 交互惯例）。用户自建 Agent 无此能力（非目标）。
- 管理列表（`AgentManagePanel`）对 global 可见非自有 agent 显示"模板"徽标与"另存为我的副本"操作；副本成功后 `router.push('/agent?agent_id=<新id>')`。

**`/help` 与每日一技（零定时基建，预留钉钉）**

- `/help` 由前端拦截：`trim` 后恰为 `/help`（无参数）→ 不发 run，本地渲染不持久化的系统卡片（能力简述 + 模板推荐，点击即选中）；其他 `/xxx` 原样发送，零行为变化；语义与后端 `channel_command_service` 对齐，为 CLI/钉钉通道复用同一数据源留口。
- 每日一技：tips 池即各 preset `tips[]`；服务层只读函数按"当日与固定日历锚点（2026-01-01）相距天数 % tips 总池长度"确定性产出 `daily_tip`（含来源模板），由 scene-templates 端点顺带返回，开始屏目录顶部展示（点击即选中来源 agent）。将来钉钉推送（方向 A 独立立项后）读同一函数；本期无外发、无调度改动。

## 替代方案

- 独立 `scene_template` 数据模块（DB 表 + 管理端点）：为"管理员可配置"预留，但已选内置只读，属提前建设，否决。
- 纯前端实现（硬编码目录、前端 GET+POST 拼装副本）：零后端改动，但元数据与 preset 脱节会漂移、副本无后端校验与授权 Owner（违反权限在后端边界执行的系统事实），否决。
- 定时任务生成每日一技内容：tips 是只读代码内容，无需生成，引入调度与状态纯属多余，否决。

## 验收标准

| 验收主张 | 失败面 | 语义 Owner | 直接证据 / 命令 | 负向案例 | 当前结果 |
|---|---|---|---|---|---|
| preset 元数据扩展后发现机制仍幂等，scene 非空入目录、空 scene 排除 | 预置失联或子 Agent 误入目录 | `agents/presets/__init__.py` | unit：发现带新字段；scene 空/非空断言 | 重复 slug 仍被拒绝 | Not run |
| scene-templates 端点按 scene 分组排序且 agent_id 与种子行一致 | 分组错乱或 agent_id 悬空 | `agent_config_service.py` | HTTP+PostgreSQL integration | 不存在 slug 不产出条目 | Not run |
| duplicate 正向：副本私有归本人、config 快照完整、名称后缀正确 | 副本继承 global 或丢配置 | `agent_config_service.py` | HTTP+PostgreSQL integration 回读 | 副本不继承 `is_default` | Not run |
| duplicate 负向：无权源 404、subagent 400 | 越权复制或子 Agent 泄露 | `agent_router.py` + service | integration 负向用例 | 跨用户私有源 404 | Not run |
| 场景目录渲染分组与点选切 agent | 选不中或误开跑 | `SceneCatalog.vue` | web unit（store 交互纯逻辑） | 无模板数据时目录不渲染报错 | Not run |
| 示例问题 chips 点击填入不自动发送 | 误触发提问 | `AgentChatComponent.vue` | web unit | 无 example_questions 不渲染 chips | Not run |
| `/help` 拦截渲染、其他 slash 原样发送 | 误吞正常消息或漏拦截 | `AgentChatComponent.vue` | web unit | `/help extra` 原样发送 | Not run |
| 每日一技确定性（固定日期→固定 tips）且展示可点击选中 | 多实例不一致或不可交互 | 服务层 daily_tip 函数 + 前端 | unit：锚点日期固定值；web unit 展示逻辑 | 空 tips 池不产 daily_tip | Not run |
| 既有行为零回归：preset 种子幂等、原会话发起链路不变 | 升级破坏既有智能体 | 既有 lifespan/`ensure_preset`/会话链路 | 既有 unit + 全量套件 | 升级重复种子不产生重复行 | Not run |
| 全量命令通过（后端 unit、web unit/lint/build、contracts、docs build） | 回归破坏 | 各命令 Owner | 测试规范命令全量执行 | 正向命令 | Not run |

## 风险

- **钉钉推送延后**：每日一技的外发消费依赖方向 A（钉钉接入）独立立项；本期站内闭环，数据源已收敛为服务层只读函数，接口不因延后而返工。
- **元数据随版本发布**：场景文案/示例问题运营迭代需发版（已接受）；后续若诉求变强，可在不改端点契约的前提下演进为管理员配置（方案 B 的延后形态）。
- **副本的引用悬空**：副本快照复制 skills/knowledges/mcps 引用，源或目标后续被删除产生的悬空引用与既有 agent 编辑行为一致，非新增风险面；副本与源此后互不影响。
- **daily_tip 锚点**：使用固定日历锚点（2026-01-01）而非部署时间，保证多实例与跨天一致；池长度变化会改变当日 tip 内容，属内容更新的预期行为。
- **开始屏布局回归**：开始屏是高频首见界面，模板卡引入改变视觉首屏；以最小改动挂载（欢迎语与输入框之间既有 dock 区域），web unit + 手工验证覆盖新旧两种状态（有/无模板数据）。
