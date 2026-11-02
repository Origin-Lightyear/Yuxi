# 待办收件箱（P2）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 把当前用户每条线程的最新顶层 Run 投影成统一待办收件箱，提供未读、筛选、详情和审批跳转，不新建第二套任务事实表。

**Architecture:** `AgentRun` 仍是唯一执行事实；`WorkItem` 由 repository 用窗口函数实时投影，`work_item_views` 只记录用户已读。FastAPI 只做薄路由，投影、状态映射和权限归 `workbench_service` 所有；前端 `/workbench` 消费同一 DTO，审批仍回会话由 `HumanApprovalModal` 处理。

**Tech Stack:** PostgreSQL、SQLAlchemy async、FastAPI、Pydantic、Vue 3、Pinia、Ant Design Vue、node:test。

**Spec:** `docs/superpowers/specs/2026-10-26-enterprise-workbench-design.md`

## Global Constraints

- 事实来源固定为「每线程最新顶层 Run」：`run_type IN ('chat','resume')`，排除 `source IN ('agent_call','agent_evaluation')`；不读取 subagent Run。
- `queued` 只映射 `AgentRun.status='pending'`；`AgentRunRequest.status='queued'` 不产生独立待办。
- `waiting_approval` 只由 `status='interrupted'` + `error_type IN ('human_approval_required','ask_user_question_required')` 产生；其余 `interrupted` 归 `failed`。
- 已读粒度是 `item_key`，不是线程；项目 key 固定 `run:<run_id>`。
- 可见范围只按 `AgentRun.uid=当前用户`；不因 Agent 后续停用或取消共享而隐藏本人历史 Run。
- `source='scheduled_agent'` 的发起方显示为对应 Agent（数字员工）；其余显示当前用户。
- P2 不实现 `handoff` source，但 DTO、key 解析和前端动作不能写死只有 `agent_run`。
- 分页默认 50，最大 200；排序按 Run `updated_at DESC, id DESC`。
- 新表必须 bump `BUSINESS_SCHEMA_VERSION`，同步幂等 DDL、`storage_migration.upgrade_from`、schema 版本测试和迁移集成测试。
- 不允许把审批 UI 复制进待办页；详情只提供「去对话处理」跳转。
- 测试命令统一使用 `docker compose exec -T api uv run --no-sync --group test pytest ...`。

## Review Focus

- 同一线程连续多个 Run：只有最新 Run 出现；旧 Run 的详情 key 必须 404，不能展示过期审批。
- `pending`、`interrupted` 普通错误、`cancel_requested` 的状态映射：用户不能看到「执行中」却实际等待审批的错位。
- 审批载荷不在 `AgentRun`：详情不能假装能展示审批参数，只能提供会话跳转。
- 筛选 + 分页 + summary 必须基于同一窗口结果；不能先分页再筛选导致总数和列表不一致。
- 已读幂等和跨用户隔离：重复标记不报错，别人的 key 不能标记。

---

### Task 1: 已读表、迁移版本与幂等 DDL

**Files:**
- Modify: `backend/package/yuxi/storage/postgres/models_business.py`
- Modify: `backend/package/yuxi/storage/postgres/manager.py`
- Modify: `backend/package/yuxi/storage_migration.py`
- Test: `backend/test/integration/services/test_schema_migration_version.py`
- Create: `backend/test/integration/storage/test_work_item_views_migration.py`

**Interfaces:**
- Consumes: existing `Base`, `utc_now_naive`, `BUSINESS_SCHEMA_VERSION`.
- Produces: ORM `WorkItemView(id, uid, item_key, viewed_at)`; `BUSINESS_SCHEMA_VERSION=11`; table `work_item_views` with unique `(uid,item_key)` and index on `uid`.

- [x] **Step 1: 写失败测试**

在 `test_schema_migration_version.py` 把业务版本断言改为 11，并在新迁移测试中断言：
```python
assert await fetchval("SELECT to_regclass('public.work_item_views')") == "work_item_views"
assert await fetchval(
    "SELECT count(*) FROM pg_indexes WHERE tablename='work_item_views' AND indexname='uq_work_item_views_uid_item_key'"
) == 1
```
再执行两次 `upgrade_from(..., target_version=11)`，断言无异常且 `work_item_views` 仍只有一张。

- [x] **Step 2: 运行测试确认失败**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/integration/services/test_schema_migration_version.py backend/test/integration/storage/test_work_item_views_migration.py -p no:cacheprovider -q`
Expected: FAIL，版本仍是 10 且 `to_regclass` 为空。

- [x] **Step 3: 实现模型和 DDL**

`models_business.py` 增加：
```python
class WorkItemView(Base):
    __tablename__ = "work_item_views"
    id = Column(Integer, primary_key=True, autoincrement=True)
    uid = Column(String(64), nullable=False, index=True)
    item_key = Column(String(96), nullable=False)
    viewed_at = Column(DateTime, nullable=False, default=utc_now_naive)
    __table_args__ = (UniqueConstraint("uid", "item_key", name="uq_work_item_views_uid_item_key"),)
```
`manager.py` 把 `BUSINESS_SCHEMA_VERSION` 改为 11，并在业务 DDL 末尾追加幂等 `CREATE TABLE IF NOT EXISTS` 与 `CREATE UNIQUE INDEX IF NOT EXISTS`。`storage_migration.upgrade_from` 接受 10 并迁移到 11。

- [x] **Step 4: 跑迁移测试**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/integration/services/test_schema_migration_version.py backend/test/integration/storage/test_work_item_views_migration.py -p no:cacheprovider -q`
Expected: PASS。

- [x] **Step 5: 提交**

```bash
git add backend/package/yuxi/storage/postgres/models_business.py backend/package/yuxi/storage/postgres/manager.py backend/package/yuxi/storage_migration.py backend/test/integration/services/test_schema_migration_version.py backend/test/integration/storage/test_work_item_views_migration.py
git commit -m "feat(待办): 增加待办已读表与迁移"
```

---

### Task 2: 窗口投影 repository

**Files:**
- Create: `backend/package/yuxi/repositories/work_item_repository.py`
- Test: `backend/test/unit/repositories/test_work_item_repository.py`

**Interfaces:**
- Consumes: `AgentRun`, `Conversation`, `Message`, `Agent`, `WorkItemView`.
- Produces:
```python
TOP_LEVEL_RUN_TYPES = ("chat", "resume")
EXCLUDED_SOURCES = ("agent_call", "agent_evaluation")
@dataclass(frozen=True)
class WorkItemRow:
    run_id: str
    thread_id: str
    conversation_id: str | None
    agent_slug: str | None
    agent_name: str | None
    title: str | None
    input_text: str | None
    source: str | None
    run_status: str
    error_type: str | None
    created_at: datetime
    updated_at: datetime
class WorkItemRepository:
    async def list_latest(self, uid: str, *, status: str | None, limit: int, offset: int) -> tuple[list[WorkItemRow], int]: ...
    async def get_latest_run(self, uid: str, run_id: str) -> WorkItemRow | None: ...
    async def summary(self, uid: str) -> dict[str, int]: ...
    async def viewed_keys(self, uid: str, keys: list[str]) -> set[str]: ...
    async def mark_viewed(self, uid: str, key: str) -> None: ...
```
- Produces SQL status expression: `pending->queued`, `running|cancel_requested->running`, approval interruption->`waiting_approval`, other interruption->`failed`, `completed|failed|cancelled` passthrough.

- [x] **Step 1: 写失败测试**

用真实 PostgreSQL fixture 插入同线程旧 Run/新 Run、其他用户 Run、subagent Run、agent_call Run 和 pending Run；断言：
```python
rows, total = await repo.list_latest(uid, status=None, limit=50, offset=0)
assert [row.run_id for row in rows] == ["run-new", "run-pending"]
assert total == 2
rows, total = await repo.list_latest(uid, status="waiting_approval", limit=50, offset=0)
assert [row.run_id for row in rows] == ["run-new"]
assert total == 1
assert await repo.get_latest_run(uid, "run-old") is None
```

- [x] **Step 2: 运行测试确认失败**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/unit/repositories/test_work_item_repository.py -p no:cacheprovider -q`
Expected: FAIL，模块不存在。

- [x] **Step 3: 实现窗口查询**

用 `row_number() OVER (PARTITION BY conversation_thread_id ORDER BY created_at DESC, id DESC)` 生成 `latest` 子查询；`rn=1` 后再按状态表达式过滤、计数、分页。`get_latest_run` 用相同子查询定位 `run_id`，确保旧 Run 不可见。左连 `Conversation`、`Message`、`Agent`，只读 `Conversation.title` 和 `Message.content` 作为标题回退。`summary` 对 `rn=1` 结果 group by 状态表达式。`viewed_keys` 一次 `IN` 查询；`mark_viewed` 用 PostgreSQL `ON CONFLICT (uid,item_key) DO UPDATE SET viewed_at=EXCLUDED.viewed_at`。

- [x] **Step 4: 跑 repository 测试**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/unit/repositories/test_work_item_repository.py -p no:cacheprovider -q`
Expected: PASS。

- [x] **Step 5: 提交**

```bash
git add backend/package/yuxi/repositories/work_item_repository.py backend/test/unit/repositories/test_work_item_repository.py
git commit -m "feat(待办): 实现每线程最新 Run 窗口投影"
```

---

### Task 3: 待办 service 与 API

**Files:**
- Create: `backend/package/yuxi/services/workbench_service.py`
- Create: `backend/server/routers/workbench_router.py`
- Modify: `backend/server/routers/__init__.py`
- Test: `backend/test/unit/services/test_workbench_service.py`
- Test: `backend/test/integration/api/test_workbench_api.py`

**Interfaces:**
- Consumes: `WorkItemRepository`, `AgentRun` terminal constants.
- Produces:
```python
def run_work_item_key(run_id: str) -> str: return f"run:{run_id}"
def parse_work_item_key(key: str) -> tuple[str, str]: ...
async def list_work_items(db, user, *, status=None, limit=50, offset=0) -> dict
async def get_work_item(db, user, key) -> dict
async def mark_work_item_viewed(db, user, key) -> dict
async def get_work_item_summary(db, user) -> dict
```
DTO exactly from spec plus `unread: bool` and `approval_summary: str | None`.

- [x] **Step 1: 写失败测试**

unit 测试固定 `WorkItemRow`，断言 `source=scheduled_agent` 得到 `initiator_type="digital_employee"`、`initiator_name=agent_name`；`pending` 得到 `queued`；审批中断得到 `waiting_approval` 和 `approval_kind="tool"`；旧 key 返回 404。API 测试用真实登录用户和真实 `AgentRun`，断言：
```python
resp = await client.get("/api/workbench/todos")
assert resp.json()["total"] == 1
assert resp.json()["items"][0]["source_type"] == "agent_run"
assert resp.json()["items"][0]["unread"] is True
resp = await client.post(f"/api/workbench/todos/{key}/viewed")
assert resp.status_code == 200
assert (await client.get("/api/workbench/todos/summary")).json()["unread"] == 0
```

- [x] **Step 2: 运行测试确认失败**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/unit/services/test_workbench_service.py backend/test/integration/api/test_workbench_api.py -p no:cacheprovider -q`
Expected: FAIL，路由不存在。

- [x] **Step 3: 实现 service、router 和注册**

`workbench_service` 负责 key 解析、标题回退（`Conversation.title` 为空时截断 `input_text` 80 字）、发起方、审批映射和 `unread`。`workbench_router` 提供四个只读/标记接口，使用 `get_required_user` 和 `get_db`，详情不存在统一 404「待办不存在」。在 `routers/__init__.py` 导入，在 `api.py` 注册 `prefix="/workbench"`。

- [x] **Step 4: 跑 API 测试**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/unit/services/test_workbench_service.py backend/test/integration/api/test_workbench_api.py -p no:cacheprovider -q`
Expected: PASS。

- [x] **Step 5: 提交**

```bash
git add backend/package/yuxi/services/workbench_service.py backend/server/routers/workbench_router.py backend/server/routers/__init__.py backend/test/unit/services/test_workbench_service.py backend/test/integration/api/test_workbench_api.py
git commit -m "feat(待办): 提供运行时投影 API"
```

---

### Task 4: 前端待办页与导航

**Files:**
- Create: `web/src/apis/workbench_api.js`
- Create: `web/src/utils/workItem.js`
- Create: `web/src/utils/workItem.test.js`
- Create: `web/src/views/WorkbenchView.vue`
- Modify: `web/src/apis/index.js`
- Modify: `web/src/router/index.js`
- Modify: `web/src/layouts/AppLayout.vue`

**Interfaces:**
- Consumes: `/api/workbench/todos`, `/api/workbench/todos/{key}`, `/api/workbench/todos/{key}/viewed`, `/api/workbench/todos/summary`.
- Produces: `workbenchApi`、`WORK_ITEM_STATUS_META`、`workItemStatusLabel`、`workItemAction`、`workItemTarget`。

- [x] **Step 1: 写 failing util 测试**

断言 `pending`/`interrupted` 映射、`waiting_approval` 动作是 `open_thread`、`agent_run` 目标路由是 `AgentCompWithThreadId`、`handoff` 目标路由是 `FounderTwinComp`。
```javascript
assert.equal(workItemStatusLabel('waiting_approval'), '待审批')
assert.equal(workItemAction({ source_type: 'agent_run', status: 'waiting_approval' }).label, '去对话处理')
```

- [x] **Step 2: 运行测试确认失败**

Run: `cd web && node --test src/utils/workItem.test.js`
Expected: FAIL，模块不存在。

- [x] **Step 3: 实现 API、工具和页面**

`WorkbenchView.vue`：
- 顶部 `PageHeader` + 四个汇总卡（全部、未读、待审批、失败）。
- 状态筛选和刷新；列表卡片显示来源、标题、Agent、发起方、状态、更新时间。
- 点击卡片打开详情抽屉；打开时调用 `markViewed`，刷新 summary 和列表。
- 审批/Run 详情按钮 `去对话处理` 跳 `AgentCompWithThreadId`，query 带 `thread_id`；不复制审批 UI。
- handoff 类型保留 `去处理转交` 分支，路由名 `FounderTwinComp`，P2 没有数据时自然为空。
- 空态和错误态显式展示。

`AppLayout.vue` 的 `mainList` 增加 `/workbench`「待办」，挂载时和每 30 秒拉取 summary，导航显示未读数。`router/index.js` 增加 `/workbench` 路由（name `WorkbenchComp`）。

- [x] **Step 4: 跑前端验证**

Run: `cd web && node --test src/utils/workItem.test.js && ./node_modules/.bin/eslint src/views/WorkbenchView.vue src/layouts/AppLayout.vue src/apis/workbench_api.js src/utils/workItem.js && ./node_modules/.bin/vite build`
Expected: 测试 PASS，lint 0 error/warning，build 成功。

- [x] **Step 5: 提交**

```bash
git add web/src/apis/workbench_api.js web/src/apis/index.js web/src/utils/workItem.js web/src/utils/workItem.test.js web/src/views/WorkbenchView.vue web/src/router/index.js web/src/layouts/AppLayout.vue
git commit -m "feat(待办): 增加统一执行收件箱页面"
```

---

### Task 5: 集成验收、文档与决策记录

**Files:**
- Create: `docs/develop-guides/decisions/implemented/2026-10-26-workbench-todos-p2.md`
- Modify: `docs/develop-guides/decisions/README.md`
- Modify: `docs/superpowers/specs/2026-10-26-enterprise-workbench-design.md`

**Interfaces:**
- Consumes: 前四个任务的 API、页面、迁移。
- Produces: P2 implemented 决策记录，记录事实来源、状态映射、queued 口径、已读粒度、可见范围、验证和未验证范围。

- [x] **Step 1: 补真实 HTTP integration 场景**

在 `test_workbench_api.py` 增加两个用户隔离、过期 Run key 404、`status=waiting_approval` 筛选、分页 `limit=1` 且 `total` 不变、scheduled run 发起方为数字员工。

- [x] **Step 2: 跑完整后端相关测试**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/unit/repositories/test_work_item_repository.py backend/test/unit/services/test_workbench_service.py backend/test/integration/api/test_workbench_api.py backend/test/integration/storage/test_work_item_views_migration.py backend/test/integration/services/test_schema_migration_version.py -p no:cacheprovider -q`
Expected: PASS。

- [x] **Step 3: 写决策记录和文档**

决策记录包含：
- 选择运行时投影而不是 `todo_items`；后果是审批详情仍需回会话。
- `queued` 只来自 pending Run；排队中的 `AgentRunRequest` 不单列。
- 已读按 `item_key`；只按 Run 所属用户可见。
- 排除 subagent、agent_call、agent_evaluation。
- 验证命令与结果；真实浏览器和 deterministic E2E 若未运行必须写 Not run。

- [x] **Step 4: 跑工程契约**

Run: `python3 scripts/verify_engineering_contracts.py && python3 -m unittest scripts.test_verify_engineering_contracts`
Expected: PASS。

- [x] **Step 5: 提交**

```bash
git add docs/develop-guides/decisions/implemented/2026-10-26-workbench-todos-p2.md docs/develop-guides/decisions/README.md docs/superpowers/specs/2026-10-26-enterprise-workbench-design.md
git commit -m "docs(待办): 记录 P2 决策与验证"
```
