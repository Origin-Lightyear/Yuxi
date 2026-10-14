# 场景模板与发现性 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让内置预置 Agent 以场景模板形态呈现（目录/示例问题/每日一技/另存为副本/`/help`），解决"不知道能干什么"的采用率痛点。

**Architecture:** 沿用既有 `agents/presets` 种子机制，仅在 `AgentPreset` 上加代码内元数据字段（scene/icon/example_questions/tips，scene 非空即入目录）；后端新增 `GET /api/agent/scene-templates`（聚合 + 确定性 daily_tip）与 `POST /api/agent/{id}/duplicate`（快照式私有副本）两个薄端点；前端改造开始屏（SceneCatalog + chips + `/help` 本地拦截）与管理列表（模板徽标 + 副本入口）。

**Tech Stack:** FastAPI + SQLAlchemy async（既有）、pytest integration（真实 HTTP+PostgreSQL）、Vue 3 SFC、node --test（既有 SFC 源码切片 harness 模式）。

**Spec:** `docs/develop-guides/decisions/proposed/2026-10-12-scene-templates-and-discoverability.md`

**Plan 额外内容（spec 未枚举，已在设计对话确认）:** 模板数量 = 既有 2 个（default-chatbot、deep-research）+ 新增 3 个内容场景 preset（写作润色、周报生成、数据解读），共 5 模板、3 个场景分组。

## Global Constraints

- 所有新增/修改的验收矩阵结果列仅可为 `Passed/Inspected/Not run/Inferred`（contracts 门禁枚举）。
- HTTP 路由薄：用例流程进 `yuxi.services`，持久化查询进 `yuxi.repositories`；权限经 `user_can_access_agent` / 后端依赖执行，前端不做授权边界。
- `AgentPreset` 元数据只存代码不入库；`scene` 非空即入目录，`scene == ""` 的子 Agent 预置自动排除。
- duplicate 副本：私有 `share_config`（read_scope 与 manage_scope 均 `access_level="user"`、`user_uids=[str(user.uid)]`、`version: 2`）、`is_default` 不复制、`is_subagent` 恒 False、名称 `f"{原名} 副本"`、`config_json` 深拷贝并经 `prepare_agent_config_write` 重算 `config_resource_access`。
- `GET /api/agent/scene-templates` 必须声明在 `GET /api/agent/{agent_id}` **之前**（路由顺序，否则被当 slug 解析成 404）。
- `/help` 仅拦截 `trim()` 后恰为 `/help` 的文本；其他内容（含 `/help extra`、其他 `/xxx`）原样走既有发送链路，零行为变化。
- `daily_tip` 锚点 `DAILY_TIP_ANCHOR = date(2026, 1, 1)`，`idx = (today - anchor).days % len(pool)`，池 = 全部模板 tips 按 SCENES 顺序展平；空池返回 `None` 且前端不渲染。
- web 单测命令：`cd web && npm run test:unit`；lint：`npm run lint:check`；build：`npm run build`。后端 unit：`cd backend && uv run --group test pytest test/unit -m "not slow"`；integration（需 docker compose env）：`docker compose exec api uv run --group test pytest test/integration`；无 env 时矩阵按 S3 先例诚实记 `Not run` 并在 PR 说明。
- 提交信息中文 Conventional Commit；每任务一提交；不引用中央 claim ID。

## Review Focus

1. **路由顺序抢位**：scene-templates 若注册在 `/{agent_id}` 之后会被吞成 slug 查询 → 404。Task 2 integration 正向断言响应含 `scenes` 数组即覆盖。
2. **升级种子幂等**：新增 3 个 preset 进入 lifespan `default_agents` 种子链，重复启动不得产生重复行或已有行配置回写。Task 6 全量 unit（既有 `ensure_preset`/discovery 测试）覆盖，负向证据 = 既有重复 slug 拒绝测试仍在。
3. **副本越权引用**：`config_json` 里的技能/知识库引用必须以副本所有者重算 `config_resource_access`，否则副本持有源的资源授权。Task 3 integration 断言副本 `config_json` 与源一致且序列化 `is_template` 为 false、`share_config` 仅含副本所有者。
4. **`/help` 误吞**：`/help extra`、` /help `（带空格）等边界。`isHelpCommand` 纯函数 unit 覆盖（`trim()==="/help"` 为唯一真值），带参数变体必须为 false。
5. **daily_tip 跨日期与空池**：锚点当天、锚点前一天（负数取模）、空 tips 池。Task 2 unit 三例覆盖；前端 `daily_tip=null` 不渲染由 Task 4 unit 断言。

---

### Task 1: AgentPreset 元数据扩展与场景内容

**Files:**
- Modify: `backend/package/yuxi/agents/presets/__init__.py`
- Modify: `backend/package/yuxi/agents/presets/default_chatbot.py`、`backend/package/yuxi/agents/presets/deep_research.py`
- Create: `backend/package/yuxi/agents/presets/writing_assistant.py`、`backend/package/yuxi/agents/presets/weekly_report.py`、`backend/package/yuxi/agents/presets/data_interpreter.py`
- Test: `backend/test/unit/agents/test_builtin_discovery.py`（新增用例）

**Interfaces:**
- Consumes: 既有 `discover_agent_presets()`、`AgentPreset` dataclass。
- Produces:
  - `AgentPreset(slug, name, description, backend_id="ChatbotAgent", context={}, scene="", icon="", example_questions=(), tips=())`——四个新字段均带空默认，既有子 Agent 预置文件零改动即自动 `scene=""`。
  - `SCENES: tuple[tuple[str, str], ...] = (("general", "通用助手"), ("writing", "写作与办公"), ("research", "研究与分析"))`——有序 (key, 中文标签)。
  - `preset_slug_set() -> frozenset[str]`（`@lru_cache`）——供 Task 3 `is_template` 判定。
  - 场景归属：default-chatbot→general（icon `message-circle`）、writing-assistant→writing（`pen-line`）、weekly-report→writing（`file-text`）、deep-research→research（`telescope`）、data-interpreter→research（`bar-chart-3`）。
  - 新 preset slug：`writing-assistant`、`weekly-report`、`data-interpreter`；`backend_id="ChatbotAgent"`；`context={"system_prompt": ...}`（各自简短中文 system prompt）；每个模板 `example_questions` 3 条、`tips` 2 条（池共 10 条）；既有两个模板也补齐 3+2。
  - `discover_agent_presets()` 增加校验：`scene` 非空但不在 `SCENES` keys → `ValueError`。

- [ ] **Step 1: 写失败测试**

在 `backend/test/unit/agents/test_builtin_discovery.py` 新增（`pytestmark` 沿用该文件既有配置）：

```python
def test_scene_metadata_and_content_templates():
    presets = {p.slug: p for p in discover_agent_presets()}
    assert presets["default-chatbot"].scene == "general"
    assert presets["default-chatbot"].example_questions  # 非空
    assert presets["fact-verifier"].scene == ""  # 子 Agent 排除
    for slug in ("writing-assistant", "weekly-report", "data-interpreter"):
        assert presets[slug].backend_id == "ChatbotAgent"
        assert presets[slug].tips and presets[slug].example_questions
    assert [key for key, _ in SCENES] == ["general", "writing", "research"]
    assert preset_slug_set() >= {"default-chatbot", "deep-research", "writing-assistant"}

def test_discover_rejects_unknown_scene(monkeypatch):
    # 构造 scene 不在 SCENES 的 PRESET 模块并断言 ValueError
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run --group test pytest test/unit/agents/test_builtin_discovery.py -v`
Expected: FAIL（`scene` 字段不存在 / `SCENES` 未定义）

- [ ] **Step 3: 实现**

`presets/__init__.py`：`AgentPreset` 加四个字段与 `SCENES`、`preset_slug_set()`；`discover_agent_presets()` 内加 scene 校验。三个新 preset 文件按 Interfaces 的 slug/icon/3+2 内容各写一个 `PRESET`；两个既有文件补字段（不改 name/description/backend_id）。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run --group test pytest test/unit/agents/test_builtin_discovery.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/package/yuxi/agents/presets backend/test/unit/agents/test_builtin_discovery.py
git commit -m "feat(discover): 预置 Agent 扩展场景元数据并新增内容场景模板"
```

---

### Task 2: scene-templates 聚合服务、daily_tip 与只读端点

**Files:**
- Modify: `backend/package/yuxi/services/agent_config_service.py`
- Modify: `backend/server/routers/agent_router.py`（在 `get_agent` 声明**之前**插入）
- Test: `backend/test/unit/services/test_scene_templates.py`（新建）、`backend/test/integration/api/test_scene_templates_api.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `SCENES`、`preset_slug_set()`；既有 `AgentRepository.list_by_slugs(slugs)`（`agent_repository.py:185`）、`initialize_agent_presets` 种子结果。
- Produces:
  - `DAILY_TIP_ANCHOR: Final = date(2026, 1, 1)`
  - `def pick_daily_tip(candidates: list[tuple[AgentPreset, Agent]], today: date) -> dict | None`——纯函数；池按 `SCENES` 顺序 × 预置定义顺序展平 `(preset, tip_text, agent)`；返回 `{"tip", "slug", "name", "agent_id"}`；`candidates` 为空或全无 tips → `None`。
  - `async def get_scene_templates(db: AsyncSession) -> dict`——返回 `{"scenes": [{"scene", "label", "templates": [{"agent_id", "slug", "name", "description", "icon", "example_questions"}]}], "daily_tip": {...} | None}`；`scene==""` 的预置与未种子化（`list_by_slugs` 缺行）的 slug 均跳过；空场景不产出分组。
  - 路由：`@agent_router.get("/scene-templates")`，`Depends(get_required_user)` + `Depends(get_db)`，返回上述 dict。**声明位置在 `get_agent`（`/{agent_id}`）之前**。

- [ ] **Step 1: 写失败测试**

unit（`test_scene_templates.py`）：

```python
def test_get_scene_templates_groups_by_scene():  # 分组顺序=SCENES；agent_id 为种子行 id；example_questions 透传
def test_pick_daily_tip_deterministic():
    # 固定 candidates，date(2026,1,1) 与 date(2026,1,2) 结果按 %len(pool) 连续，date(2025,12,31) 与 date(2026,1,1) 同值（负向取模）
def test_pick_daily_tip_empty_pool_returns_none():
```

integration（`test_scene_templates_api.py`，`pytestmark = [pytest.mark.asyncio, pytest.mark.integration]`，沿用既有 `test_client`/`admin_headers` fixture）：

```python
async def test_scene_templates_endpoint_returns_catalog(test_client, admin_headers):
    response = await test_client.get("/api/agent/scene-templates", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [s["scene"] for s in body["scenes"]] == ["general", "writing", "research"]
    slugs = {t["slug"] for s in body["scenes"] for t in s["templates"]}
    assert {"default-chatbot", "deep-research", "writing-assistant"} <= slugs
    assert body["daily_tip"] is None or {"tip", "slug", "name", "agent_id"} <= set(body["daily_tip"])
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run --group test pytest test/unit/services/test_scene_templates.py -v`
Expected: FAIL（函数未定义）；integration 需 compose env，可在本步跳过、Step 4 一并跑。

- [ ] **Step 3: 实现**

`agent_config_service.py` 加 `DAILY_TIP_ANCHOR`、`pick_daily_tip`、`get_scene_templates`（读 `SCENES` → 预置定义 → `AgentRepository.list_by_slugs` 取 agent 行 → 组装）；`agent_router.py` 在 `get_agent` 之前加端点。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run --group test pytest test/unit/services/test_scene_templates.py -v`
Expected: PASS
Run: `docker compose exec api uv run --group test pytest test/integration/api/test_scene_templates_api.py -v`（env 不可用时记录待跑，矩阵暂 `Not run`）
Expected: PASS（可用时）

- [ ] **Step 5: 提交**

```bash
git add backend/package/yuxi/services/agent_config_service.py backend/server/routers/agent_router.py backend/test/unit/services backend/test/integration/api/test_scene_templates_api.py
git commit -m "feat(discover): 新增场景模板聚合端点与确定性每日一技"
```

---

### Task 3: duplicate 另存为副本端点

**Files:**
- Modify: `backend/package/yuxi/repositories/agent_repository.py`（加 `get_by_id`）
- Modify: `backend/package/yuxi/services/agent_config_service.py`（加 `duplicate_agent`）
- Modify: `backend/server/routers/agent_router.py`
- Test: `backend/test/integration/api/test_agent_duplicate_api.py`（新建）

**Interfaces:**
- Consumes: 既有 `user_can_access_agent`（`agent_repository.py:64`）、`prepare_agent_config_write`（POST /api/agent 创建路径同款）、`repo.create(...)`（`agent_repository.py:253` 一带的 is_subagent 解析）、`_serialize_agent`。
- Produces:
  - `async def AgentRepository.get_by_id(agent_id: str) -> Agent | None`——按主键读。
  - `async def duplicate_agent(agent_id: str, *, user: User, db: AsyncSession) -> dict`——顺序：`get_by_id` 空 → 404；`user_can_access_agent` False → 404；`is_subagent` → 400（`HTTPException`）。通过后：`backend = get_agent_backend(src.backend_id)`；`config_json, config_resource_access = await prepare_agent_config_write(copy.deepcopy(src.config_json), context_schema=backend.context_schema, db=db, user=user)`；`repo.create(name=f"{src.name} 副本", slug=None, backend_id=src.backend_id, description=src.description, icon=src.icon, pics=src.pics, config_json=config_json, config_resource_access=config_resource_access, share_config={"version": 2, "read_scope": {"access_level": "user", "department_ids": [], "user_uids": [str(user.uid)]}, "manage_scope": {"access_level": "user", "department_ids": [], "user_uids": [str(user.uid)]}}, is_default=False, is_subagent=False, created_by=str(user.uid), creator=user)`；返回 `{"agent": await _serialize_agent(repo, item, user, include_configurable_items=True)}`（与 POST /api/agent 同形）。
  - 路由：`@agent_router.post("/{agent_id}/duplicate")`，`Depends(get_required_user)`。
  - 序列化扩展（`AgentRepository.serialize`）：`data["is_template"] = agent.slug in preset_slug_set() and not agent.is_subagent`。

- [ ] **Step 1: 写失败测试**

`test_agent_duplicate_api.py`（真实 HTTP+PG，`pytestmark = [pytest.mark.asyncio, pytest.mark.integration]`；私有源用 `AgentRepository.create` 直接落库构造，`share_config` 指向他人 uid）：

```python
async def test_duplicate_template_creates_private_copy_for_current_user(test_client, admin_headers):
    r = await test_client.post("/api/agent/{default-chatbot主agent的GET返回id}/duplicate", headers=admin_headers)
    assert r.status_code == 200, r.text
    agent = r.json()["agent"]
    assert agent["name"].endswith(" 副本") and agent["is_default"] is False
    assert agent["share_config"]["read_scope"] == {"access_level": "user", "department_ids": [], "user_uids": ["<当前uid>"]}
    assert agent["is_template"] is False

async def test_duplicate_negative(...):
    # 不存在 id → 404；他人私有 agent → 404；subagent(fact-verifier 种子行) → 400
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run --group test pytest test/integration/api/test_agent_duplicate_api.py -v`
Expected: FAIL/ERROR（404 未实现路由）

- [ ] **Step 3: 实现**

按 Interfaces 的 `duplicate_agent` 与路由、`get_by_id`、`is_template` 注入实现（薄路由 + 服务用例 + repository 查询）。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && uv run --group test pytest test/integration/api/test_agent_duplicate_api.py -v`
Expected: PASS（env 不可用时矩阵记 `Not run`）

- [ ] **Step 5: 提交**

```bash
git add backend/package/yuxi backend/server/routers/agent_router.py backend/test/integration/api/test_agent_duplicate_api.py
git commit -m "feat(discover): 新增智能体另存为副本端点"
```

---

### Task 4: SceneCatalog 目录、示例问题 chips 与每日一技（前端）

**Files:**
- Create: `web/src/components/SceneCatalog.vue`
- Modify: `web/src/apis/agent_api.js`（加 `fetchSceneTemplates`）
- Modify: `web/src/stores/agent.js`（加 `sceneTemplates`/`dailyTip` state 与 `loadSceneTemplates` action）
- Modify: `web/src/components/AgentChatComponent.vue`（greeting 与 `AgentInputArea` 之间挂载目录与 chips）
- Test: `web/test/unit/sceneCatalog.test.js`（新建，沿用 `chatStartScreen.test.js` 的源码切片 + `Vue.compile` harness）

**Interfaces:**
- Consumes: Task 2 端点响应形状；`agentStore.selectAgent(agentId, {allowSubagent})`（`agent.js:198`）；`userInput`（AgentInputArea `v-model`，`AgentChatComponent.vue:257`）；`isNewConversation` prop。
- Produces:
  - `agent_api.js`: `export const fetchSceneTemplates = () => request.get('/api/agent/scene-templates')`（沿用该文件既有 request 封装风格）。
  - `agent.js`: `sceneTemplates = ref(null)`、`dailyTip = ref(null)`、`async function loadSceneTemplates()`（幂等：已加载则跳过；成功时分别赋值 `data.scenes` / `data.daily_tip`）。
  - `SceneCatalog.vue`：props `{ scenes: Array, dailyTip: Object|null }`；emits `select(agentId)`；模板含每日一技横幅（`dailyTip` 为空则整块不渲染，点击横幅 emit `select(dailyTip.agent_id)`）与按 `scene` 分组的模板卡（icon+name+description，点击 emit `select(template.agent_id)`）。
  - `AgentChatComponent.vue`：`v-if="isNewConversation"` 区域内（greeting 之后）挂 `<SceneCatalog :scenes="sceneTemplates" :daily-tip="dailyTip" @select="agentStore.selectAgent" />`（`sceneTemplates` 为 null 时不渲染）；chips：`selectedTemplate = computed(() => sceneTemplates 扁平列表中 agent_id === selectedAgentId 的项 || null)`，其 `example_questions` 非空且 `isNewConversation` 时渲染 chips，点击 `userInput = question`（**只填充不发送**）。首次进入新会话时调用 `loadSceneTemplates()`（watch `isNewConversation` 或 setup 时机，幂等保护）。

- [ ] **Step 1: 写失败测试**

`sceneCatalog.test.js`：

```js
// harness 仿 chatStartScreen.test.js：readFileSync SceneCatalog.vue → 截取 <template> → Vue.compile → renderToString
test('渲染场景分组与模板卡，点卡 emit select', ...)
test('dailyTip 为空不渲染横幅，非空渲染且点横幅 emit select', ...)
// 源码断言：AgentChatComponent 挂载 SceneCatalog、chips 点击只赋值 userInput 不调用发送（source.includes("userInput = ") 且 handler 内无发送调用）
```

- [ ] **Step 2: 运行确认失败**

Run: `cd web && node --test test/unit/sceneCatalog.test.js`
Expected: FAIL（组件/挂载不存在）

- [ ] **Step 3: 实现**

按 Interfaces 实现 api/store/组件/挂载/chips。

- [ ] **Step 4: 运行确认通过**

Run: `cd web && node --test test/unit/sceneCatalog.test.js && npm run test:unit && npm run lint:check`
Expected: 全 PASS，0 警告

- [ ] **Step 5: 提交**

```bash
git add web/src web/test/unit/sceneCatalog.test.js
git commit -m "feat(discover): 开始屏场景目录、每日一技与示例问题填充"
```

---

### Task 5: /help 本地拦截与管理列表副本入口

**Files:**
- Create: `web/src/utils/commandInput.js`
- Modify: `web/src/components/AgentChatComponent.vue`（发送前拦截 + 本地帮助卡片）
- Modify: `web/src/apis/agent_api.js`（加 `duplicateAgent`）
- Modify: `web/src/components/model-management/AgentManagePanel.vue`（模板徽标 + 副本操作）
- Test: `web/test/unit/helpCommand.test.js`（新建）、`web/test/unit/duplicateAgent.test.js`（新建）

**Interfaces:**
- Consumes: Task 3 序列化字段 `is_template`；Task 4 的 `loadSceneTemplates`/`sceneTemplates`（帮助卡片复用模板数据）；`AgentManagePanel` 既有 `canManageAgent`/`isBuiltinAgent` 与 `#card-more-action-corner` 插槽（`:223`）。
- Produces:
  - `web/src/utils/commandInput.js`: `export function isHelpCommand(text) { return String(text ?? '').trim() === '/help' }`——唯一拦截判定，其他任何输入（含 `/help extra`、`/HELP`）为 false。
  - `AgentChatComponent.vue`: 发送 handler 顶部 `if (isHelpCommand(userInput)) { userInput = ''; helpCardOpen.value = true; return }`；`helpCardOpen = ref(false)`；帮助卡片渲染在 greeting 区（含静态能力说明 + `sceneTemplates` 模板列表，点模板条目调 `agentStore.selectAgent` 并关卡片；关闭按钮 `helpCardOpen=false`；普通消息发送成功路径把 `helpCardOpen` 置 false）。
  - `agent_api.js`: `export const duplicateAgent = (agentId) => request.post(\`/api/agent/${agentId}/duplicate\`)`。
  - `AgentManagePanel.vue`: 卡片 `#card-more-action-corner` 插槽 `v-if` 放开为 `canManageAgent(agent) || agent.is_template`（菜单内编辑/删除项仍各自 `canManageAgent` 判断）；新增"模板"徽标（`v-if="agent.is_template"`）与"另存为我的副本"项（`v-if="agent.is_template"`）→ `await agentApi.duplicateAgent(agent.id)` → `refreshAgentLists()` → `router.push({ name: 'AgentComp', query: { agent_id: newAgent.id } })` → `message.success`。

- [ ] **Step 1: 写失败测试**

`helpCommand.test.js`：`isHelpCommand('/help')===true`；`isHelpCommand(' /help ')===true`；`'/help extra'`/`'/HELP'`/`'help'`/`''`/`null` 均 false。
`duplicateAgent.test.js`：源码断言 `AgentManagePanel.vue` 含 `is_template` 徽标与 `duplicateAgent` 调用、插槽 v-if 含 `|| agent.is_template`；`AgentChatComponent.vue` 含 `isHelpCommand(userInput)` 拦截先于发送调用。

- [ ] **Step 2: 运行确认失败**

Run: `cd web && node --test test/unit/helpCommand.test.js test/unit/duplicateAgent.test.js`
Expected: FAIL

- [ ] **Step 3: 实现**

按 Interfaces 实现纯函数、拦截、卡片、api、管理列表入口。

- [ ] **Step 4: 运行确认通过**

Run: `cd web && npm run test:unit && npm run lint:check`
Expected: 全 PASS

- [ ] **Step 5: 提交**

```bash
git add web/src web/test/unit
git commit -m "feat(discover): 本地 /help 指令与模板另存为副本入口"
```

---

### Task 6: 全量验证与决策记录回填

**Files:**
- Modify: `docs/develop-guides/decisions/proposed/2026-10-12-scene-templates-and-discoverability.md` → 移至 `implemented/`

**Interfaces:**
- Consumes: Task 1-5 全部产物与既有门禁。
- Produces: 实现终态决策记录（`状态：implemented`、`## 问题/决策/替代方案/后果/验证` 结构、无 `## 提案` 标题、Owner 指向真实存在的 `backend/package/yuxi/agents/presets/__init__.py`、验收矩阵结果列回填 `Passed/Inspected/Not run`）。

- [ ] **Step 1: 后端全量 unit**

Run: `cd backend && uv run --group test pytest test/unit -m "not slow" -q`
Expected: 全 PASS（种子幂等、既有 discovery/repository 测试不回归）

- [ ] **Step 2: web 全量 + lint + build**

Run: `cd web && npm run test:unit && npm run lint:check && npm run build`
Expected: 全 PASS

- [ ] **Step 3: integration（按环境）**

Run: `docker compose exec api uv run --group test pytest test/integration -q`
Expected: PASS；若 compose env 不可用（缺 `.env`），矩阵诚实记 `Not run` 并在最终 PR 说明，不伪造。

- [ ] **Step 4: 回填决策记录并过门禁**

移至 `implemented/`，按 Produces 结构改写，逐行回填矩阵实际结果；运行：

```bash
python3 scripts/verify_engineering_contracts.py
python3 -m unittest scripts.test_verify_engineering_contracts
cd docs && npm run build
git diff --check
```

Expected: contracts 通过、docs build 成功、diff 无空白错误

- [ ] **Step 5: 提交**

```bash
git add docs/develop-guides/decisions
git commit -m "docs(discover): 场景模板功能收敛为实现终态"
```
