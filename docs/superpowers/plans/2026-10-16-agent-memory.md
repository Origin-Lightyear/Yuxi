# 跨会话用户×Agent 记忆功能 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为每个 (用户×Agent) 建立云端持久的记忆条目：Run 完成后后台 LLM 抽取、主 Agent 注入提示词、用户在聊天页查看并删除。

**Architecture:** 新表 `agent_memories`（uid+agent_slug，`source_run_id` 唯一幂等，硬删）四层落地；抽取走 `task_registry` durable task（`run_worker` finally 仅 completed 终态发布）；注入用独立 middleware `agent_memory.py` 挂在 chatbot graph 既有 memory middleware 旁（`prepare_run_execution` 身份块补 `agent_slug` 进 context）；API 两个薄端点 + 聊天页 agent 下拉入口面板。

**Tech Stack:** SQLAlchemy + asyncpg（幂等 DDL 进 `manager.py`，无 Alembic）、`task_registry`/`task_queue_service` durable task、LangGraph middleware（`append_to_system_message`）、`select_model` 单次 LLM 抽取、Vue 3 `<script setup>` + lucide 图标。

**Spec:** `docs/develop-guides/decisions/implemented/2026-10-16-agent-memory-feature.md`（本计划实现该 spec；实现层裁定见 Global Constraints 第 1 条；记录已随实现移入 `implemented/`）。

## Global Constraints

1. **实现裁定（spec 微调，行为验收不变）**：spec 写"扩展既有 memory middleware"，实现改为**独立** `agents/middlewares/agent_memory.py`。原因：`load_memory_prompt`（`memory_service.py:20`）在 `enable_memory` 开但 MEMORY.md 缺失/为空时返回 None → 既有 middleware 不创建；agent 记忆注入不得依赖 MEMORY.md 存在。graph 挂载点紧邻既有 memory middleware；矩阵 Owner 行随 Task 3 更新为 `agents/middlewares/agent_memory.py`。
2. `enable_memory` 统一管两套：关闭时不注入（两套都不注入）、不发布抽取任务；已有条目保留可见可删。不开新开关。
3. 所有条目查询/删除强制 `uid` 过滤；非本人/不可见 agent 一律 404，不泄露存在性。
4. 容量硬上限 50 条/ (uid, agent_slug)，超限按 `created_at` 升序删最旧；抽取条目每条 ≤100 字、中文、0~N 条；注入取最新 20 条（倒序）。
5. 抽取仅 `final_run.status == "completed"` 触发；发布失败 best-effort 记日志，不改 Run 结果；`source_run_id` 唯一约束兜底任务重试幂等。
6. 抽取模型决议：run `input_payload.model_spec` → 系统 option `default_model`（`config/options.py:95`）→ 皆无则跳过抽取记 warning。
7. `agent_slug` 进 context 只取自 Run 行（`run.agent_slug`），`configurable=False`，不信任外部输入。
8. 证据底线：后端 unit `cd backend && uv run --group test pytest test/unit -m "not slow" -q` 全绿；web `cd web && npm run test:unit && npm run lint:check` 全绿；`python3 scripts/verify_engineering_contracts.py` 通过；integration/浏览器验证无环境则矩阵诚实记 `Not run`。
9. 每任务 TDD：先 RED 再 GREEN，中文 Conventional Commit（带 why body）；只改任务范围。
10. 门禁函数复用既有事实：任务发布模式抄 `run_worker.py:685-697`（create task record + `publish_task(task_id)`）；handler 签名抄 `knowledge_task_service.py:35`（`async def run_*(context: TaskContext) -> dict`，`TaskContext` 来自 `yuxi.services.task_service`）。

## Review Focus

- **开关关闭时的注入/发布**：`enable_memory=False` 时 middleware 不创建、finally 不发布任务——两者都必须独立测试（最容易漏掉发布侧）。
- **空 MEMORY.md 用户**：`enable_memory=True` 且 MEMORY.md 为空但有 agent 条目时，`<agent_memory>` 必须注入（这正是独立 middleware 的理由，须有单测钉住）。
- **幂等**：同 `source_run_id` 任务重跑不产生重复条目（唯一约束 + 负向测试）。
- **越权**：他人 `memory_id` DELETE、不可见 agent GET——必须 404 而非 403/200 空列表（存在性不泄露）。
- **容量边界**：恰好 50 条时不删、第 51 条写入时最旧被删（边界单测）。

---

### Task 1: `agent_memories` 表、模型与 Repository

**Files:**
- Modify: `backend/package/yuxi/storage/postgres/models_business.py`（`scheduled_agent_jobs` 后追加）
- Modify: `backend/package/yuxi/storage/postgres/manager.py`（`ensure_business_schema` DDL 块，仿 `:1068-1097`）
- Create: `backend/package/yuxi/repositories/agent_memory_repository.py`
- Test: `backend/test/unit/storage/test_agent_memory_model.py`、`backend/test/unit/repositories/test_agent_memory_repository.py`

**Interfaces:**
- Consumes: `pg_manager` 基类约定、`select`/`and_`、现有 `Base` 模型基类（抄 `scheduled_agent_jobs` 声明处 import）。
- Produces:
  - `AgentMemory` SQLAlchemy 模型：列 `id`（pk）、`uid`（str，非空，索引）、`agent_slug`（str(255)，非空，索引）、`content`（text，非空）、`source_run_id`（str，非空，**唯一约束**）、`created_at`/`updated_at`；`to_dict()` 输出 `id/uid/agent_slug/content/source_run_id/created_at/updated_at`。
  - `AgentMemoryRepository(db)`：
    - `async def list_by_agent(self, *, uid: str, agent_slug: str) -> list[dict]`（`created_at` DESC）
    - `async def add(self, *, uid: str, agent_slug: str, content: str, source_run_id: str) -> dict`（唯一约束冲突时上抛，由 handler 捕获跳过该条）
    - `async def count_by_agent(self, *, uid: str, agent_slug: str) -> int`
    - `async def delete_oldest_over_limit(self, *, uid: str, agent_slug: str, limit: int = 50) -> int`（删除至剩 `limit` 条，返回删除数）
    - `async def delete(self, *, uid: str, agent_slug: str, memory_id: int) -> bool`（三重过滤硬删，返回是否命中）
  - DDL（幂等 `CREATE TABLE IF NOT EXISTS agent_memories` + 唯一索引 `ux_agent_memories_source_run_id` + `uid/agent_slug` 组合普通索引 `ix_agent_memories_uid_agent`）。

- [ ] **Step 1: 写失败模型测试**

`test/unit/storage/test_agent_memory_model.py`：构造 `AgentMemory(uid="u1", agent_slug="a1", content="条目", source_run_id="r1")`，断言 `to_dict()` 含全部 7 键且 `content == "条目"`。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run --group test pytest test/unit/storage/test_agent_memory_model.py -q`
Expected: FAIL（`ImportError: cannot import name 'AgentMemory'`）

- [ ] **Step 3: 实现 `AgentMemory` 模型与 DDL**

在 `models_business.py` 按 `scheduled_agent_jobs` 声明风格追加 `AgentMemory`（表名 `agent_memories`；`source_run_id = Column(String(64), nullable=False, unique=True, index=True)` 之类——唯一性以 DDL/约束测试为准）；`to_dict()` 手写 7 键。`manager.py` `ensure_business_schema` 的业务 DDL 段追加幂等建表 + 两个索引（`execute(text(...))` 模式抄既有块）。

- [ ] **Step 4: 写失败 repository 测试（RED）**

`test/unit/repositories/test_agent_memory_repository.py`（仿现有 repository unit 用内存/fixture session，若仓内 repository unit 依赖真实 PG session fixture 则按该 fixture 写；断言业务结果而非 SQL 文本）：
- 正向：`add` 三条不同 `source_run_id` → `list_by_agent` 返回 3 条按 `created_at` 倒序，`count_by_agent == 3`。
- 负向-幂等：同 `source_run_id` 二次 `add` 上抛唯一约束异常。
- 负向-所有权：`delete(uid="other", ...)` 返回 False 且行仍在。
- 边界：造 50 条后 `delete_oldest_over_limit(limit=50)` 返回 0；再加 1 条调用后返回 1 且最旧消失。

- [ ] **Step 5: 跑测试确认失败**

Run: `cd backend && uv run --group test pytest test/unit/repositories/test_agent_memory_repository.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 6: 实现 `AgentMemoryRepository`**

按 Interfaces 块实现五个 async 方法；`delete_oldest_over_limit` 用子查询取超限最旧行 id 后 delete；所有方法内部拼 `uid ==` 与 `agent_slug ==` 条件，绝不接受无 uid 调用。

- [ ] **Step 7: 跑两个测试文件确认通过**

Run: `cd backend && uv run --group test pytest test/unit/storage/test_agent_memory_model.py test/unit/repositories/test_agent_memory_repository.py -q`
Expected: 全 PASS

- [ ] **Step 8: Commit**

```bash
git add backend/package/yuxi/storage/postgres/models_business.py backend/package/yuxi/storage/postgres/manager.py backend/package/yuxi/repositories/agent_memory_repository.py backend/test/unit/storage/test_agent_memory_model.py backend/test/unit/repositories/test_agent_memory_repository.py
git commit -m "feat(memory): 新增 agent_memories 表模型与 uid 强制过滤的 repository" -m "source_run_id 唯一约束兜底抽取幂等；容量 50 截断与硬删除为后续任务的地基。"
```

---

### Task 2: `agent_memory_service` 用例与薄路由

**Files:**
- Create: `backend/package/yuxi/services/agent_memory_service.py`
- Create: `backend/server/routers/agent_memory_router.py`
- Modify: `backend/server/routers/__init__.py`（注册路由，抄 `scheduled_agent_router` 注册行）
- Test: `backend/test/unit/services/test_agent_memory_service.py`、`backend/test/integration/routers/test_agent_memory_api.py`

**Interfaces:**
- Consumes: `AgentMemoryRepository`（Task 1 五方法）、`UserConfig.load(db, uid).schema.enable_memory`（`config/user.py:19`）、`AgentRepository(db).get_visible_by_slug(slug=..., user=..., kind=...)`（`prepare_run_execution` 用法样例）、既有 router 依赖注入（`db: AsyncSession`、`current_user` 依赖，抄 `scheduled_agent_router.py` 顶部）。
- Produces（供 Task 3/4/5/UI）：
  - `agent_memory_service.py`：
    - `async def list_memories(*, uid: str, agent_slug: str) -> list[dict]`
    - `async def delete_memory(*, uid: str, agent_slug: str, memory_id: int) -> None`（未命中抛 `ValueError("记忆不存在")`，路由转 404）
    - `async def is_memory_enabled(uid: str) -> bool`（读 `UserConfig`，Task 5 发布侧与 Task 3 注入侧共用）
    - `async def load_injection_entries(*, uid: str, agent_slug: str, limit: int = 20) -> list[str]`：`enable_memory` 关或 uid 空 → `[]`；否则 `list_by_agent` 取前 `limit` 条 content（倒序已由 repository 保证）
  - Router（prefix `/api/agent`，路由声明在 `GET /{agent_id}` 之前无要求——本路由是不同 path 模板 `/{slug}/memories`，FastAPI 按注册顺序匹配，注册进 `__init__.py` 时放在 agent 主 router 之前或确认无参数遮蔽）：
    - `GET /api/agent/{slug}/memories` → `{"items": [...]}`；`get_visible_by_slug(slug, user=current_user, kind="main")` 为 None → 404；否则 `list_memories(uid=str(current_user.uid), agent_slug=slug)`
    - `DELETE /api/agent/{slug}/memories/{memory_id}` → `{"ok": True}`；可见性同上 404；`delete_memory` 未命中 → 404

- [ ] **Step 1: 写失败 service 测试（RED）**

`test/unit/services/test_agent_memory_service.py`（mock/monkeypatch `AgentMemoryRepository` 与 `UserConfig`，断言业务结果）：
- `load_injection_entries`：开关关闭 → `[]`（不查库）；开关开 + 3 条 → 返回 3 个 content 字符串。
- `delete_memory`：repository `delete` 返回 False → 抛 `ValueError`。
- `is_memory_enabled`：`enable_memory` True/False 映射正确。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run --group test pytest test/unit/services/test_agent_memory_service.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `agent_memory_service.py`**

按 Interfaces 实现四函数；`delete_memory` 未命中统一抛 `ValueError("记忆不存在")`。

- [ ] **Step 4: 写失败 integration 路由测试（RED）**

`test/integration/routers/test_agent_memory_api.py`（抄 `test_scene_templates_api.py`/`test_agent_duplicate_api.py` 的 client+登录 fixture 形状）：
- 正向 GET：建一个可见智能体 + 2 条 `agent_memories` 行 → GET 返回 2 items 且倒序。
- 负向 GET：不存在/不可见 slug → 404。
- 正向 DELETE：本人条目删除 → 200 且行消失。
- 负向 DELETE：他人 uid 的条目（同 slug）→ 404 且行仍在。

- [ ] **Step 5: 跑 integration 确认失败**

Run: `cd backend && uv run --group test pytest test/integration/routers/test_agent_memory_api.py -q`（无 PG 环境则记录失败原因为环境、以 Step 8 unit 绿 + 集成代码就位交付，矩阵该行记 `Not run`）
Expected: FAIL（404 router not found / import error）

- [ ] **Step 6: 实现 router 并注册**

`agent_memory_router.py` 按 Interfaces；`routers/__init__.py` 注册；路由函数体只做依赖注入与异常→HTTP 映射，逻辑全在 service。

- [ ] **Step 7: 复跑 unit + integration**

Run: `cd backend && uv run --group test pytest test/unit/services/test_agent_memory_service.py -q && uv run --group test pytest test/integration/routers/test_agent_memory_api.py -q`
Expected: unit 全 PASS；integration PASS 或环境性阻塞如实记录

- [ ] **Step 8: Commit**

```bash
git add backend/package/yuxi/services/agent_memory_service.py backend/server/routers/agent_memory_router.py backend/server/routers/__init__.py backend/test/unit/services/test_agent_memory_service.py backend/test/integration/routers/test_agent_memory_api.py
git commit -m "feat(memory): 记忆条目查看/删除用例与两个薄端点" -m "404 不泄露他人条目存在性；enable_memory 判定收敛为 service 共用函数供注入与发布复用。"
```

---

### Task 3: `agent_slug` 身份注入 + 独立 Agent Memory Middleware

**Files:**
- Modify: `backend/package/yuxi/agents/context.py`（`BaseContext`，`run_id` 字段后追加）
- Modify: `backend/package/yuxi/services/agent_run_manifest_service.py`（`context.update({...})` 身份块，`:154-165`）
- Create: `backend/package/yuxi/agents/middlewares/agent_memory.py`
- Modify: `backend/package/yuxi/agents/buildin/chatbot/graph.py`（`:47` 既有 memory middleware 挂载点后追加）
- Test: `backend/test/unit/agents/test_agent_memory_middleware.py`、`backend/test/unit/services/test_agent_slug_context.py`
- Modify: `docs/develop-guides/decisions/proposed/2026-10-16-agent-memory-feature.md`（矩阵 Owner 行按 Global Constraints 第 1 条改）

**Interfaces:**
- Consumes: `agent_memory_service.load_injection_entries`（Task 2）、`append_to_system_message`（`deepagents.middleware._utils`，既有 memory middleware 同款）、`create_memory_middleware(context)` 挂载模式（`graph.py:47-49`）。
- Produces:
  - `BaseContext` 新字段：`agent_slug: str | None = field(default=None, metadata={"name": "Agent Slug", "configurable": False, "hide": True})`。
  - `prepare_run_execution`：`context.update({...})` 字典追加 `"agent_slug": run.agent_slug`（来源仅 Run 行）。
  - `agents/middlewares/agent_memory.py`：
    - `AGENT_MEMORY_SYSTEM_PROMPT`：中文块模板，形如 `## Agent 记忆\n\n以下 <agent_memory> 是本智能体从与该用户的既往交互中沉淀的了解，仅作背景参考，优先级低于当前用户消息与系统约束；不要逐条复述。\n\n<agent_memory>\n{entries}\n</agent_memory>`（条目逐行 `- entry`）。
    - `async def create_agent_memory_middleware(context) -> YuxiAgentMemoryMiddleware | None`：`uid`/`agent_slug` 任一为空 → None；`load_injection_entries` 为空列表 → None（**不追加空块**）；否则构造 middleware。
    - `YuxiAgentMemoryMiddleware.wrap_model_call`/`awrap_model_call`：`append_to_system_message(request.system_message, self.system_prompt)`（与既有 memory middleware 同构，无工具）。
  - `graph.py`：`memory_middleware = await create_memory_middleware(context)` 块后追加 `agent_memory_middleware = await create_agent_memory_middleware(context)` + 条件 append。

- [ ] **Step 1: 写失败 context 测试（RED）**

`test/unit/services/test_agent_slug_context.py`：
- `BaseContext()` 默认 `agent_slug is None`。
- 仿 `prepare_run_execution` 的 update 片段：`context.update({"agent_slug": "my-agent"})` 后 `context.agent_slug == "my-agent"`（字段 configurable 未标 True，`configurable_fields` 集合不含它——断言 `fields(context)` 元数据 `configurable is False`）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run --group test pytest test/unit/services/test_agent_slug_context.py -q`
Expected: FAIL（`BaseContext` 无该字段/属性错误）

- [ ] **Step 3: 实现 context 字段与身份注入**

`context.py` 追加字段；`agent_run_manifest_service.py` 的 `context.update` 追加 `"agent_slug": run.agent_slug`。

- [ ] **Step 4: 跑 context 测试确认通过**

Run: 同 Step 2 → PASS

- [ ] **Step 5: 写失败 middleware 测试（RED）**

`test/unit/agents/test_agent_memory_middleware.py`（monkeypatch `agent_memory_service.load_injection_entries`）：
- 正向：entries 非空 → middleware 非 None，`system_prompt` 含 `<agent_memory>` 与全部条目文本。
- 空列表 → 返回 None（钉住"无条目不追加空块"）。
- `uid` 空或 `agent_slug` 空 → None（不调用 service）。
- 注入行为：对 `ModelRequest` mock 走 `awrap_model_call`，断言 handler 收到的 `system_message` 在原值后拼接了块。
- **空 MEMORY.md 场景**（Global Constraints 第 1 条裁定的理由）：monkeypatch 使 `create_memory_middleware` 返回 None 时，`create_agent_memory_middleware` 仍独立返回非 None——直接断言两个工厂互不依赖。

- [ ] **Step 6: 跑测试确认失败**

Run: `cd backend && uv run --group test pytest test/unit/agents/test_agent_memory_middleware.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 7: 实现 middleware 与 graph 挂载**

按 Interfaces 实现 `agent_memory.py`；`graph.py` 在 memory middleware append 后追加 agent memory middleware 两行。

- [ ] **Step 8: 跑 middleware + context 测试确认通过**

Run: `cd backend && uv run --group test pytest test/unit/agents/test_agent_memory_middleware.py test/unit/services/test_agent_slug_context.py -q`
Expected: 全 PASS

- [ ] **Step 9: 更新 spec 矩阵 Owner 行**

把 proposed spec 矩阵"注入"行的 Owner 从 `agents/middlewares/memory.py` 改为 `agents/middlewares/agent_memory.py`（仅此一处）。

- [ ] **Step 10: Commit**

```bash
git add backend/package/yuxi/agents/context.py backend/package/yuxi/services/agent_run_manifest_service.py backend/package/yuxi/agents/middlewares/agent_memory.py backend/package/yuxi/agents/buildin/chatbot/graph.py backend/test/unit/agents/test_agent_memory_middleware.py backend/test/unit/services/test_agent_slug_context.py docs/develop-guides/decisions/proposed/2026-10-16-agent-memory-feature.md
git commit -m "feat(memory): agent_slug 进运行上下文并注入 <agent_memory> 提示块" -m "独立 middleware 使注入不依赖 MEMORY.md 存在；空条目不追加空块；来源身份仅 Run 行。"
```

---

### Task 4: `memory_extract` Durable Task（注册、抽取、容量、幂等）

**Files:**
- Create: `backend/package/yuxi/services/memory_extract_task.py`
- Modify: `backend/package/yuxi/services/task_registry.py`（`_TASK_DEFINITIONS` 元组追加）
- Test: `backend/test/unit/services/test_memory_extract_task.py`

**Interfaces:**
- Consumes: `TaskContext`（`yuxi.services.task_service:62`，用 `context.payload`/`context.set_progress`/`context.raise_if_cancelled`，样例 `knowledge_task_service.py:35`）、`ConversationRepository(db).read_memory_messages(uid=..., thread_id=..., limit=..., include_tools=False)`（`conversation_repository.py:666`）、`select_model(model_spec)`（`models/chat.py:242`，返回 adapter，调用方式参考仓内 `ainvoke` 用法——LangChain adapter `.ainvoke([...])` 或既有调用点抄一行）、`AgentMemoryRepository`（Task 1 `add`/`delete_oldest_over_limit`，唯一冲突捕获 `IntegrityError`/`sqlalchemy.exc` 后跳过该条）、`agent_memory_service.is_memory_enabled`（Task 2）。
- Produces:
  - `memory_extract_task.py`：
    - `EXTRACT_MAX_ITEMS = 10`、`EXTRACT_MAX_CHARS = 100`、`AGENT_MEMORY_LIMIT = 50`（常量集中，供容量测试引用）。
    - `EXTRACT_SYSTEM_PROMPT`：中文抽取指令——从对话中抽取值得该智能体长期记住的用户事实/偏好，每条 ≤100 字、中文、不要凭空推测；无可抽取则输出空 JSON 数组；输出格式 `["条目1", "条目2"]`。
    - `async def extract_memory_items(*, messages: list, model_spec: str) -> list[str]`：构造 user 消息（对话文本序列化，截断至有界长度如 8000 字符）+ system prompt，`select_model(model_spec).ainvoke` 一次，解析 JSON 数组，过滤非 str/超长（截断到 100 字），返回 list（解析失败 → `[]` 并记 warning，不抛）。
    - `async def run_memory_extract(context: TaskContext) -> dict`：payload 读 `uid/agent_slug/thread_id/run_id/model_spec`；`is_memory_enabled` 关 → 返回 `{"skipped": True}`；`read_memory_messages` 取对话（空 → `{"skipped": True}`）；`extract_memory_items`；逐条 `add(..., source_run_id=run_id)`，唯一冲突捕获跳过；写入后 `delete_oldest_over_limit(limit=AGENT_MEMORY_LIMIT)`；返回 `{"extracted": n}`。
  - `task_registry.py`：`TaskDefinition("memory_extract", "yuxi.services.memory_extract_task", "run_memory_extract")`。

- [ ] **Step 1: 写失败抽取测试（RED）**

`test/unit/services/test_memory_extract_task.py`（mock `select_model` 与 repository/UserConfig）：
- `extract_memory_items`：mock 模型返回 `'["喜欢简洁回答", "负责财务报表"]'` → 2 条；返回 `'[]'` → `[]`；返回非 JSON（`"ok"`）→ `[]`；超长条目被截到 100 字。
- `run_memory_extract`：开关关闭 → `{"skipped": True}` 且不调用模型；空对话 → `{"skipped": True}`；正常 2 条 → `add` 被调 2 次且 `source_run_id == run_id`、随后调用 `delete_oldest_over_limit(limit=50)`；`add` 第二条抛唯一冲突 → 第一条仍写入、任务不抛。
- 容量边界：`delete_oldest_over_limit` 收到的 `limit` 恒为常量 `AGENT_MEMORY_LIMIT == 50`。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run --group test pytest test/unit/services/test_memory_extract_task.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 `memory_extract_task.py` 并注册**

按 Interfaces 实现；`task_registry._TASK_DEFINITIONS` 追加一行。

- [ ] **Step 4: 跑测试确认通过**

Run: 同 Step 2 → PASS

- [ ] **Step 5: Commit**

```bash
git add backend/package/yuxi/services/memory_extract_task.py backend/package/yuxi/services/task_registry.py backend/test/unit/services/test_memory_extract_task.py
git commit -m "feat(memory): memory_extract durable task（LLM 抽取+幂等写入+容量截断）" -m "source_run_id 唯一约束与开关短路保证重试幂等与零成本关闭；解析失败降级为空集。"
```

---

### Task 5: `run_worker` completed 终态发布抽取任务

**Files:**
- Modify: `backend/package/yuxi/services/run_worker.py`（finally completed 分支，`:1481-1486` `dispatch_next_request` 块附近追加）
- Test: `backend/test/unit/services/test_run_worker_memory_publish.py`

**Interfaces:**
- Consumes: `agent_memory_service.is_memory_enabled`（Task 2）、`TaskRepository.create(task_id, data)`（`task_repository.py:125`，返回 `(record, created)`）、`publish_task(task_id)`（`task_queue_service.py:17`）、`final_run.input_payload`（`run_worker.py:894` 已有 dict 校验先例）、系统 option `default_model`（`config/options.py:95`，读取方式参考仓内加载 system options 的既有调用——grep `default_model`/`system_options` 的读取点抄一行）、`uuid4`。
- Produces:
  - `run_worker` finally 中新增私有 helper：`async def _publish_memory_extract(*, uid: str, agent_slug: str, thread_id: str, final_run) -> None`：
    - `is_memory_enabled(uid)` 为 False → 直接 return。
    - `model_spec = final_run.input_payload.get("model_spec") or 系统 default_model`；皆空 → logger.warning 后 return。
    - `task_id = str(uuid4())`；`TaskRepository().create(task_id, {"type": "memory_extract", "payload": {"uid": uid, "agent_slug": agent_slug, "thread_id": thread_id, "run_id": final_run.id, "model_spec": model_spec}, "handler_version": 1})`——data 键形状抄知识任务创建处（实现者 grep `TaskRepository` 的 `create(` 调用点对齐 `type/payload/handler_version` 等实际键名）。
    - `publish_task(task_id)`；整个 helper 调用处包 `try/except`，失败 `logger.error`，不改 Run 结果。
  - 调用点：finally 中 `if final_run and final_run.status == "completed"` 既有分支内（紧邻 `dispatch_next_request`）追加 `await _publish_memory_extract(...)`（best-effort）。

- [ ] **Step 1: 写失败发布测试（RED）**

`test/unit/services/test_run_worker_memory_publish.py`（monkeypatch `_publish_memory_extract` 依赖的 `is_memory_enabled`/`TaskRepository`/`publish_task`；直接测 helper 函数，不跑整个 worker）：
- 开关关 → 不创建任务不发布。
- 开关开 + payload 有 `model_spec` → task data `type=="memory_extract"` 且 payload 五键齐、`model_spec` 为该值。
- payload 无 `model_spec` → 使用系统 `default_model`（monkeypatch 读取函数返回固定值）。
- `publish_task` 抛异常 → helper/调用不向上抛（断言不 raise）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && uv run --group test pytest test/unit/services/test_run_worker_memory_publish.py -q`
Expected: FAIL（函数不存在）

- [ ] **Step 3: 实现 `_publish_memory_extract` 并接线 finally**

按 Produces；调用点在 completed 分支内、异常安全。

- [ ] **Step 4: 跑测试 + 相关既有 worker unit 确认通过**

Run: `cd backend && uv run --group test pytest test/unit/services/test_run_worker_memory_publish.py -q && uv run --group test pytest test/unit -m "not slow" -q`
Expected: 新测试 PASS；全量 unit 无回归

- [ ] **Step 5: Commit**

```bash
git add backend/package/yuxi/services/run_worker.py backend/test/unit/services/test_run_worker_memory_publish.py
git commit -m "feat(memory): completed Run 终态 best-effort 发布抽取任务" -m "失败/中断 Run 不发布；发布失败只记日志不改 Run 结果；模型决议 payload 优先、系统 default_model 兜底。"
```

---

### Task 6: 聊天页「记忆」入口与面板

**Files:**
- Create: `web/src/apis/agent_memory_api.js`
- Create: `web/src/components/AgentMemoryPanel.vue`
- Modify: `web/src/views/AgentView.vue`（agent 配置下拉 `config-dropdown-actions`，`:86-104` "编辑智能体/新建智能体" 按钮组内追加「记忆」项；组件区挂载 `<AgentMemoryPanel>`）
- Test: `web/test/AgentMemoryPanel.test.mjs`（node --test 源码断言模式，仿现有 `*.test.mjs`）

**Interfaces:**
- Consumes: `agent_memory_api` 请求封装模式（抄 `web/src/apis/scheduled_agent_api.js` 的 request/baseURL 写法）、`userConfigApi.get().enable_memory`（`user_config_api.js`，样例 `UserConfigSettingsCard.vue:36`）、lucide `Brain`/`Trash2`/`X` 图标（项目已用 lucide-vue-next）、既有 Modal/Drawer 组件约定（看 `AgentEditModal.vue` 的 Teleport/overlay 结构，面板用同款轻量 Modal）。
- Produces:
  - `agent_memory_api.js`：`list(slug)` → GET `/api/agent/${slug}/memories`；`remove(slug, memoryId)` → DELETE `/api/agent/${slug}/memories/${memoryId}`。
  - `AgentMemoryPanel.vue` props：`agentSlug`（String，必填）、`open`（Boolean）； emits：`close`。行为：`open` 变 true 且 `enable_memory` 为真时拉取列表（loading/空态「暂无记忆」/列表：每条 content + `created_at` 格式化时间 + 删除按钮）；删除经确认（`window.confirm` 或既有确认组件，抄仓内删除确认先例）后调 `remove` 并就地移除；失败提示沿用既有消息组件。`enable_memory` 为假时不渲染内容也不拉取。
  - `AgentView.vue`：下拉 actions 区在「新建智能体」后加「记忆」按钮（lucide `Brain` 图标），点击 `openMemoryPanel = true` 并记录当前 `currentAgentOption.slug`；`v-if` 门控：当前 agent 存在且 `enableMemory` 为真（挂载时 `userConfigApi.get()` 一次，与 `UserConfigSettingsCard` 同法）。面板组件挂载在 `AgentEditModal` 附近。

- [ ] **Step 1: 写失败面板测试（RED）**

`web/test/AgentMemoryPanel.test.mjs`（node --test，读组件源码做结构断言，仿仓内现有 vue 测试形态——先看 `web/test/` 下任一 `.test.mjs` 对 `.vue` 的处理方式，同构仿写）：
- 源码含 `agent_memory_api`/`list(`/`remove(` 调用。
- 源码含空态文案「暂无记忆」、删除确认与 `enable_memory`/`enableMemory` 门控。
- `AgentView.vue` 源码含「记忆」按钮文案、`AgentMemoryPanel` 挂载与 `Brain` 图标 import。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd web && npm run test:unit -- --test-name-pattern=AgentMemoryPanel`（或全量 `npm run test:unit` 看新文件 FAIL）
Expected: FAIL（组件/文件不存在）

- [ ] **Step 3: 实现 api 封装、面板组件与 AgentView 接线**

按 Produces；视觉/交互对齐仓内既有 Modal 与下拉样式，不引入新 UI 库。

- [ ] **Step 4: 跑 web 全量 unit + lint 确认通过**

Run: `cd web && npm run test:unit && npm run lint:check`
Expected: 全 PASS（既有 438+ 不回归，新测试计入）

- [ ] **Step 5: Commit**

```bash
git add web/src/apis/agent_memory_api.js web/src/components/AgentMemoryPanel.vue web/src/views/AgentView.vue web/test/AgentMemoryPanel.test.mjs
git commit -m "feat(web): 聊天页 agent 下拉「记忆」入口与查看/删除面板" -m "enable_memory 关闭时入口不出现；仅展示当前用户自己的条目。"
```

---

### 收尾（不单列 Task，实现完成后执行并记录）

- 矩阵回填：`docs/develop-guides/decisions/proposed/2026-10-16-agent-memory-feature.md` 各行"当前结果"列按实际命令输出改 `Passed`（unit/web/门禁）或保持 `Not run`（integration/浏览器，无环境时）；文档行 Owner 更新为本计划实际文件。
- 全量门禁：`python3 scripts/verify_engineering_contracts.py` + `python3 -m unittest scripts.test_verify_engineering_contracts` + 后端 unit 全量 + web unit/lint/build + `git diff --check`。
- 未验证范围（PR/记录用）：真实浏览器页面视觉验证（web/AGENTS.md 要求）、`test_agent_memory_api.py` integration（无 PG 环境时）。
