# 老板分身（P3）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 用 `Agent.kind='founder_twin'` 提供可管理的老板分身：经验条目确认后向量检索，咨询答案必须通过引用校验工具，没依据只能创建转交单，并把转交单接入 P2 待办。

**Architecture:** 分身、经验、咨询、转交分别由 PostgreSQL 持久化；检索在写入/确认时生成 embedding，查询时按 `twin_slug + confirmed` 过滤并在进程内做余弦 top-k。Chatbot 通过条件 middleware 注入三个工具和答案 guard；API 与前端只展示已被后端校验的事实。

**Tech Stack:** PostgreSQL、SQLAlchemy async、FastAPI、Pydantic、LangChain middleware、Vue 3、Ant Design Vue、node:test。

**Spec:** `docs/superpowers/specs/2026-10-26-enterprise-workbench-design.md`

## Global Constraints

- `founder_twin` 复用 `Agent.kind`，不新建员工/分身表；后端仍使用 `ChatbotAgent`。
- 经验状态固定 `draft -> confirmed -> invalid`；已确认条目被编辑后回到 `draft` 并清空 embedding。
- 检索只加载 `status='confirmed'` 且 `embedding IS NOT NULL` 的条目，按分身 2,000 条上限；返回 top 5。
- `submit_consultation_answer` 必须校验引用条目存在、属于该分身、状态 confirmed、embeddable；无引用不得提交答案，只能 `create_handoff_request`。
- 未调用校验工具就结束必须有负向测试；第一次违规注入纠正消息，第二次违规让 Run 明确失败，不能静默给结论。
- 普通员工只能看自己的咨询和转交；分身管理者（`user_can_manage_agent`）可管理条目并查看全部。
- 转交单以 `source_type='handoff'` 接入 P2 DTO；只在 P3 实现，不复刻第二套待办页面。
- 新增三张表 bump `BUSINESS_SCHEMA_VERSION` 到 12，同步 ORM、幂等 DDL、`storage_migration.upgrade_from`、schema 测试和迁移集成测试。
- 不引入 pgvector/Milvus；规模超过 2,000 条时在文档记录迁移边界。
- 测试命令统一使用 `docker compose exec -T api uv run --no-sync --group test pytest ...`。

## Review Focus

- 引用校验收到 `cited_entry_ids=[]` 时不能返回成功答案；必须引导创建转交单。
- 引用其他分身或已失效条目的 ID：必须失败，不能因为 ID 存在就通过。
- 模型在工具校验后改写答案：guard 必须拒绝与已提交答案不一致的最终文本。
- 同一 Run 重复调用提交工具：幂等或明确失败，不能写出两条咨询记录。
- 管理者与普通员工看到不同咨询/转交范围；普通员工不能通过 URL 猜别人的记录。

---

### Task 1: 分身 kind、context 归属与三张表

**Files:**
- Modify: `backend/package/yuxi/agents/digital_employee.py`
- Modify: `backend/package/yuxi/agents/context.py`
- Modify: `backend/package/yuxi/services/agent_run_service.py`
- Modify: `backend/package/yuxi/storage/postgres/models_business.py`
- Modify: `backend/package/yuxi/storage/postgres/manager.py`
- Modify: `backend/package/yuxi/storage_migration.py`
- Test: `backend/test/integration/services/test_schema_migration_version.py`
- Create: `backend/test/integration/storage/test_founder_twin_migration.py`
- Test: `backend/test/unit/services/test_agent_run_context.py`

**Interfaces:**
- Produces: `FOUNDER_TWIN_KIND = "founder_twin"`, `MANAGED_AGENT_KINDS = {DIGITAL_EMPLOYEE_KIND, FOUNDER_TWIN_KIND}`.
- Produces: `BaseContext.agent_kind: str`（默认 `"agent"`，`configurable=False`），`load_agent_run_context` 写入 `agent_item.kind`。
- Produces ORM:
```python
ExperienceEntry(twin_slug, code, title, situation, judgment, boundary, source_type, source_ref, source_excerpt, status, embedding, embedding_model, confirmed_by, confirmed_at, citation_count, created_by, updated_by, created_at, updated_at)
ExperienceConsultation(twin_slug, uid, thread_id, run_id, question, answer, answer_message_id, cited_entry_ids, confidence, handoff_id, created_at)
HandoffRequest(twin_slug, uid, question, assigned_to_uid, status, answer, created_at, answered_at)
```
- Produces indexes: `(twin_slug,status)`, `(twin_slug,uid,created_at)`, unique `(twin_slug,code)`, unique `experience_consultations.run_id`.

- [x] **Step 1: 写失败测试**

迁移测试断言三张表存在、`uq_experience_entries_twin_code` 存在、重复升级幂等。context 测试构造 `Agent(kind='founder_twin')` 和 backend schema，断言 `context.agent_kind == 'founder_twin'`。

- [x] **Step 2: 运行测试确认失败**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/integration/services/test_schema_migration_version.py backend/test/integration/storage/test_founder_twin_migration.py backend/test/unit/services/test_agent_run_context.py -p no:cacheprovider -q`
Expected: FAIL，表不存在且 `agent_kind` 缺失。

- [x] **Step 3: 实现常量、context 和表**

`BaseContext` 加隐藏字段：
```python
agent_kind: str = field(default="agent", metadata={"name": "Agent 类型", "configurable": False, "hide": True})
```
`load_agent_run_context` 在构造后设置 `context.agent_kind = getattr(agent_item, "kind", "agent") or "agent"`。三张表按 Interfaces 定义，embedding 用 `JSON_VALUE`，`cited_entry_ids` 用 `JSON`，金额无关；`run_id` 唯一，`handoff_id` 为 `Integer` 外键到 `handoff_requests.id`。版本改为 12，`upgrade_from` 追加 11。

- [x] **Step 4: 跑迁移和 context 测试**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/integration/services/test_schema_migration_version.py backend/test/integration/storage/test_founder_twin_migration.py backend/test/unit/services/test_agent_run_context.py -p no:cacheprovider -q`
Expected: PASS。

- [x] **Step 5: 提交**

```bash
git add backend/package/yuxi/agents/digital_employee.py backend/package/yuxi/agents/context.py backend/package/yuxi/services/agent_run_service.py backend/package/yuxi/storage/postgres/models_business.py backend/package/yuxi/storage/postgres/manager.py backend/package/yuxi/storage_migration.py backend/test/integration/services/test_schema_migration_version.py backend/test/integration/storage/test_founder_twin_migration.py backend/test/unit/services/test_agent_run_context.py
git commit -m "feat(分身): 增加老板分身类型与经验数据模型"
```

---

### Task 2: 经验确认、embedding 与余弦检索 service

**Files:**
- Create: `backend/package/yuxi/repositories/founder_twin_repository.py`
- Create: `backend/package/yuxi/services/founder_twin_service.py`
- Test: `backend/test/unit/services/test_founder_twin_service.py`

**Interfaces:**
- Consumes: `ExperienceEntry`, `ExperienceConsultation`, `HandoffRequest`, `select_embedding_model`, `app_config.embed_model`.
- Produces:
```python
EXPERIENCE_STATUS_DRAFT = "draft"
EXPERIENCE_STATUS_CONFIRMED = "confirmed"
EXPERIENCE_STATUS_INVALID = "invalid"
MAX_EXPERIENCES_PER_TWIN = 2000
def experience_embedding_text(entry: ExperienceEntry) -> str
def cosine_similarity(left: list[float], right: list[float]) -> float
async def embed_text(text: str, *, model_spec: str | None = None) -> tuple[list[float], str]
async def confirm_experience(db, entry: ExperienceEntry, *, actor_uid: str) -> ExperienceEntry
async def retrieve_experiences(db, *, twin_slug: str, query: str, limit: int = 5) -> list[tuple[ExperienceEntry, float]]
async def validate_citations(db, *, twin_slug: str, cited_entry_ids: list[int]) -> list[ExperienceEntry]
```
- Produces manager checks: `get_manageable_twin`, `get_visible_twin` using `user_can_access_agent` / `user_can_manage_agent`.

- [x] **Step 1: 写失败测试**

固定 embedding 向量，断言：
```python
assert cosine_similarity([1, 0], [1, 0]) == 1.0
assert cosine_similarity([1, 0], [0, 1]) == 0.0
await confirm_experience(db, entry, actor_uid="u1")
assert entry.status == "confirmed"
assert entry.embedding == [1.0, 0.0]
assert (await validate_citations(db, twin_slug="boss", cited_entry_ids=[])) == []
with pytest.raises(ValueError):
    await validate_citations(db, twin_slug="boss", cited_entry_ids=[other_twin_entry.id])
```
检索测试插入 3 条同分身 confirmed、1 条 draft、1 条其他分身，断言只返回同分身 confirmed 且按相似度降序，top 2。

- [x] **Step 2: 运行测试确认失败**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/unit/services/test_founder_twin_service.py -p no:cacheprovider -q`
Expected: FAIL，模块不存在。

- [x] **Step 3: 实现 repository 与 service**

`experience_embedding_text` 拼接 title、situation、judgment、boundary、source_excerpt，空行过滤。`embed_text` 使用 `select_embedding_model(model_spec or app_config.embed_model)` 和 `abatch_encode([text])`，返回向量与实际 model spec。`confirm_experience` 先生成 embedding，再写 `status/confirmed_by/confirmed_at/embedding/embedding_model`；失败不改变条目状态。`retrieve_experiences` 按 `twin_slug`、`confirmed`、embedding 非空取最多 2000 条，对查询向量做余弦排序，返回 top limit；空查询或 embedding 失败抛显式 `ValueError`。`validate_citations` 去重后一次性查询，要求数量一致且全部 `twin_slug` 匹配、状态 confirmed。

- [x] **Step 4: 跑 service 测试**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/unit/services/test_founder_twin_service.py -p no:cacheprovider -q`
Expected: PASS。

- [x] **Step 5: 提交**

```bash
git add backend/package/yuxi/repositories/founder_twin_repository.py backend/package/yuxi/services/founder_twin_service.py backend/test/unit/services/test_founder_twin_service.py
git commit -m "feat(分身): 实现经验确认、向量检索与引用校验"
```

---

### Task 3: 分身、经验、咨询与转交 API

**Files:**
- Create: `backend/server/routers/founder_twin_router.py`
- Modify: `backend/server/routers/__init__.py`
- Modify: `backend/server/routers/agent_router.py`
- Modify: `backend/package/yuxi/services/agent_config_service.py`
- Test: `backend/test/integration/api/test_founder_twin_api.py`

**Interfaces:**
- Consumes: Task 2 service、`AgentRepository`、`prepare_agent_config_write`。
- Produces endpoints:
```text
GET    /api/founder-twins
POST   /api/founder-twins
GET    /api/founder-twins/{slug}
PUT    /api/founder-twins/{slug}
GET    /api/founder-twins/{slug}/entries?status=
POST   /api/founder-twins/{slug}/entries
PUT    /api/founder-twins/{slug}/entries/{entry_id}
POST   /api/founder-twins/{slug}/entries/{entry_id}/confirm
POST   /api/founder-twins/{slug}/entries/{entry_id}/invalidate
GET    /api/founder-twins/{slug}/consultations
GET    /api/founder-twins/{slug}/handoffs
PUT    /api/founder-twins/{slug}/handoffs/{handoff_id}
```
- Produces serialized twin with `can_manage: bool` and `stats: {confirmed_entries, draft_entries, open_handoffs}`.

- [x] **Step 1: 写失败测试**

真实 HTTP 测试：
- 管理员创建分身，普通员工列表可见但不能 `PUT`。
- 普通员工创建经验返回 403。
- 管理员创建 draft，确认后列表出现 confirmed，编辑后回到 draft。
- 普通员工只看到自己的 consultation/handoff；管理员看到全部。
- 普通员工直接 `PUT` 别人的 handoff 返回 403/404。
- `GET /api/agent?kind=founder_twin` 只返回分身。

- [x] **Step 2: 运行测试确认失败**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/integration/api/test_founder_twin_api.py -p no:cacheprovider -q`
Expected: FAIL，路由不存在。

- [x] **Step 3: 实现 API 与 Agent 隔离**

创建分身必须管理员；`backend_id` 固定 `ChatbotAgent`，`kind='founder_twin'`，默认 `share_config` 全局可读、创建者可管理。更新只允许改名/描述/system_prompt/share_config；不改变 kind。经验写接口要求 `user_can_manage_agent`；确认失败返回 503 和明确「embedding 未就绪」，不写 confirmed。`agent_router` 的 kind 白名单加入 founder_twin，序列化对非管理者隐藏 config；通用 `PUT/DELETE` 对 founder_twin 返回 400，指向 `/api/founder-twins`。`agent_config_service` 的受管 kind 过滤同步。

- [x] **Step 4: 跑 API 测试**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/integration/api/test_founder_twin_api.py -p no:cacheprovider -q`
Expected: PASS。

- [x] **Step 5: 提交**

```bash
git add backend/server/routers/founder_twin_router.py backend/server/routers/__init__.py backend/server/routers/agent_router.py backend/package/yuxi/services/agent_config_service.py backend/test/integration/api/test_founder_twin_api.py
git commit -m "feat(分身): 提供分身、经验和转交 API"
```

---

### Task 4: 咨询工具、答案 guard 与中间件

**Files:**
- Create: `backend/package/yuxi/agents/founder_twin.py`
- Create: `backend/package/yuxi/agents/middlewares/founder_twin.py`
- Modify: `backend/package/yuxi/agents/middlewares/__init__.py`
- Modify: `backend/package/yuxi/agents/buildin/chatbot/graph.py`
- Modify: `backend/package/yuxi/agents/buildin/chatbot/prompt.py`
- Test: `backend/test/unit/agents/test_founder_twin_guard.py`
- Test: `backend/test/unit/agents/test_founder_twin_tools.py`

**Interfaces:**
- Produces tools: `retrieve_experience(query: str)`, `submit_consultation_answer(answer: str, cited_entry_ids: list[int], confidence: float)`, `create_handoff_request(question: str, reason: str)`.
- Produces `FounderTwinMiddleware` (system prompt + tool registration) and `FounderTwinAnswerGuardMiddleware`.
- Tool success payloads: `{"ok": true, "answer": "..."}` and `{"ok": true, "handoff_id": 1, "handoff_key": "handoff:1"}`.

- [x] **Step 1: 写 failing guard 测试**

构造 `AIMessage(content="直接结论")`，无工具消息：
```python
first = await guard.aafter_model({"messages": [HumanMessage("问题"), ai]}, runtime)
assert first["jump_to"] == "model"
assert "submit_consultation_answer" in first["messages"][0].content
with pytest.raises(RuntimeError):
    await guard.aafter_model({"messages": [HumanMessage("问题"), ai, correction, ai]}, runtime)
```
构造成功 `ToolMessage(name="submit_consultation_answer", content='{"ok":true,"answer":"有依据的结论"}')` + 相同 AI 内容，断言 `None`；AI 内容不同断言纠正；handoff 工具消息断言 `None`。再断言非 founder_twin context 直接 `None`。

- [x] **Step 2: 运行测试确认失败**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/unit/agents/test_founder_twin_guard.py backend/test/unit/agents/test_founder_twin_tools.py -p no:cacheprovider -q`
Expected: FAIL，模块不存在。

- [x] **Step 3: 实现工具、middleware 和 graph 条件注入**

工具通过 `ToolRuntime` 取 `context.agent_slug` 和 `context.uid`，在 `pg_manager.get_async_session_context()` 中调用 Task 2 service。`submit_consultation_answer` 先 `validate_citations`，空引用失败；成功写/更新 `ExperienceConsultation(run_id 唯一)` 并累加引用计数。`create_handoff_request` 写 `HandoffRequest(status='open')`，同时写 consultation（`handoff_id`、无引用）。guard 扫描最近一条 `HumanMessage` 之后的工具消息：成功 submit 要求最终 AI 文本与 `answer` 归一化后一致；成功 handoff 允许结束；否则第一次注入 `HumanMessage(name='founder_twin_guard')` 并 `jump_to='model'`，第二次抛 `RuntimeError("老板分身未提交有依据的答案")`。

`ChatbotAgent` 的 `_build_middlewares` 在 `context.agent_kind == FOUNDER_TWIN_KIND` 时追加 `FounderTwinMiddleware` 和 guard；系统提示明确「先检索、必须提交、无引用转交、最终答案原样复述提交答案」。

- [x] **Step 4: 跑工具与 guard 测试**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/unit/agents/test_founder_twin_guard.py backend/test/unit/agents/test_founder_twin_tools.py -p no:cacheprovider -q`
Expected: PASS。

- [x] **Step 5: 提交**

```bash
git add backend/package/yuxi/agents/founder_twin.py backend/package/yuxi/agents/middlewares/founder_twin.py backend/package/yuxi/agents/middlewares/__init__.py backend/package/yuxi/agents/buildin/chatbot/graph.py backend/package/yuxi/agents/buildin/chatbot/prompt.py backend/test/unit/agents/test_founder_twin_guard.py backend/test/unit/agents/test_founder_twin_tools.py
git commit -m "feat(分身): 增加咨询校验工具与答案守卫"
```

---

### Task 5: 转交单接入 P2 待办

**Files:**
- Modify: `backend/package/yuxi/repositories/work_item_repository.py`
- Modify: `backend/package/yuxi/services/workbench_service.py`
- Modify: `backend/test/unit/repositories/test_work_item_repository.py`
- Modify: `backend/test/integration/api/test_workbench_api.py`

**Interfaces:**
- Consumes: `HandoffRequest`.
- Produces `WorkItemRow.source_type: Literal["agent_run","handoff"]`; handoff key `handoff:<id>`; DTO `source_type='handoff'`, `source_id=str(id)`, `thread_id=None`, `run_id=None`.
- Handoff status mapping: `open -> running`（等待老板回答）, `answered -> completed`, `closed -> cancelled`; `approval_kind=None`; `initiator_type='human'`。

- [x] **Step 1: 写失败测试**

插入 open handoff，断言：
```python
items = (await client.get("/api/workbench/todos")).json()["items"]
handoff_item = next(item for item in items if item["source_type"] == "handoff")
assert handoff_item["key"] == f"handoff:{handoff.id}"
assert handoff_item["status"] == "running"
assert handoff_item["unread"] is True
```
另一个用户看不到；`GET /api/workbench/todos/handoff:{id}` 只能本人或分身管理者读取。

- [x] **Step 2: 运行测试确认失败**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/unit/repositories/test_work_item_repository.py backend/test/integration/api/test_workbench_api.py -p no:cacheprovider -q`
Expected: FAIL，handoff 不出现。

- [x] **Step 3: 实现双源投影**

把 Run 最新子查询和 handoff 投影做 `UNION ALL`：两者输出统一列 `source_type/source_id/title/agent_slug/agent_name/initiator_uid/status/created_at/updated_at/thread_id/run_id/approval_kind`。handoff 的 `agent_slug=twin_slug`、`agent_name=twin.name`、`title=question`。排序仍在 union 后执行；summary 按统一状态列 group by。`get_latest_run` 改为 `get_item` 按 key 分派。P2 前端已有的 `source_type`/`handoff` 分支开始生效。

- [x] **Step 4: 跑待办测试**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/unit/repositories/test_work_item_repository.py backend/test/integration/api/test_workbench_api.py -p no:cacheprovider -q`
Expected: PASS。

- [x] **Step 5: 提交**

```bash
git add backend/package/yuxi/repositories/work_item_repository.py backend/package/yuxi/services/workbench_service.py backend/test/unit/repositories/test_work_item_repository.py backend/test/integration/api/test_workbench_api.py
git commit -m "feat(分身): 将转交单接入统一待办"
```

---

### Task 6: 前端老板分身页

**Files:**
- Create: `web/src/apis/founderTwinApi.js`
- Create: `web/src/utils/founderTwin.js`
- Create: `web/src/utils/founderTwin.test.js`
- Create: `web/src/views/FounderTwinView.vue`
- Modify: `web/src/apis/index.js`
- Modify: `web/src/router/index.js`
- Modify: `web/src/layouts/AppLayout.vue`

**Interfaces:**
- Consumes: Task 3 API、P2 `workItemTarget`。
- Produces: 分身列表、详情 tabs（咨询/经验库/转交）、经验新建/编辑/确认/失效、转交回答；普通员工隐藏管理按钮。
- Produces `FOUNDER_TWIN_STATUS_META` and `founderTwinCanManage`。

- [x] **Step 1: 写 failing util 测试**

断言 `draft/confirmed/invalid` 标签、`open/answered/closed` 标签、`handoff` 待办目标为 `FounderTwinComp`。

- [x] **Step 2: 运行测试确认失败**

Run: `cd web && node --test src/utils/founderTwin.test.js`
Expected: FAIL，模块不存在。

- [x] **Step 3: 实现 API、页面和导航**

页面 `/founder-twin`：左侧分身卡片列表，右侧详情 tabs。`开始咨询` 跳 `AgentComp?agent_id=<slug>`；管理者看到「新建经验」、编辑、确认、失效按钮，普通员工只看到 confirmed 条目和本人咨询。转交 tab 管理者可分配给自己并填写回答，普通员工只读。所有空态/错误态显式展示；不把 embedding 或内部 prompt 展示给普通用户。

`AppLayout` 增加「老板分身」导航，不显示未读徽标（转交未读走待办页）。

- [x] **Step 4: 跑前端验证**

Run: `cd web && node --test src/utils/founderTwin.test.js && ./node_modules/.bin/eslint src/views/FounderTwinView.vue src/layouts/AppLayout.vue src/apis/founderTwinApi.js src/utils/founderTwin.js && ./node_modules/.bin/vite build`
Expected: 测试 PASS，lint 0 error/warning，build 成功。

- [x] **Step 5: 提交**

```bash
git add web/src/apis/founderTwinApi.js web/src/apis/index.js web/src/utils/founderTwin.js web/src/utils/founderTwin.test.js web/src/views/FounderTwinView.vue web/src/router/index.js web/src/layouts/AppLayout.vue
git commit -m "feat(分身): 增加老板分身工作台页面"
```

---

### Task 7: 集成验收、决策记录与完整验证

**Files:**
- Create: `docs/develop-guides/decisions/implemented/2026-10-26-founder-twin-p3.md`
- Modify: `docs/develop-guides/decisions/README.md`
- Modify: `docs/superpowers/specs/2026-10-26-enterprise-workbench-design.md`

- [x] **Step 1: 补负向集成测试**

在 `test_founder_twin_api.py` 增加：引用空列表提交失败；引用其他分身失败；引用 invalid 失败；同一 run 重复提交只保留一条；普通员工不可读别人 handoff；确认 embedding 失败时条目仍 draft（mock model 抛错）。

- [x] **Step 2: 跑后端相关全量**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/unit/repositories/test_work_item_repository.py backend/test/unit/services/test_workbench_service.py backend/test/unit/services/test_founder_twin_service.py backend/test/unit/agents/test_founder_twin_guard.py backend/test/unit/agents/test_founder_twin_tools.py backend/test/integration/api/test_workbench_api.py backend/test/integration/api/test_founder_twin_api.py backend/test/integration/storage/test_founder_twin_migration.py backend/test/integration/services/test_schema_migration_version.py -p no:cacheprovider -q`
Expected: PASS。

- [x] **Step 3: 跑完整后端 unit 和前端**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/unit -m "not slow" -p no:cacheprovider -q`
Run: `cd web && node --test && ./node_modules/.bin/eslint . && ./node_modules/.bin/vite build`
Expected: 全部 PASS；若有既有失败，记录基线并在 PR 中区分。

- [x] **Step 4: 写决策记录**

记录：
- `founder_twin` 复用 Agent，不新建分身表。
- 经验状态机、确认时 embedding、2,000 条上限和进程内余弦。
- 引用校验和 guard 的两次语义；无引用只转交。
- 转交单接入 P2 的状态映射和可见范围。
- 隐私：普通员工只看自己，管理者看全部。
- 验证命令、结果、未运行项（真实浏览器/确定性 E2E 若不可用必须写 Not run）。

- [x] **Step 5: 工程契约与提交**

Run: `python3 scripts/verify_engineering_contracts.py && python3 -m unittest scripts.test_verify_engineering_contracts && git diff --check`
Expected: PASS。

```bash
git add docs/develop-guides/decisions/implemented/2026-10-26-founder-twin-p3.md docs/develop-guides/decisions/README.md docs/superpowers/specs/2026-10-26-enterprise-workbench-design.md
git commit -m "docs(分身): 记录 P3 决策与验证"
```
