# 数字员工 P1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 在 Yuxi 的后端和前端交付 P1「数字员工」：管理员可创建、发布、停用员工；员工在共享范围内可见并可直接对话；状态、权限和运行时入口由后端拥有。

**Architecture:** 扩展 `Agent` 增加 `kind` 与 `digital_employee_profile`，不新建员工表。新增 `digital_employee_service` 拥有档案校验、状态迁移、可见性和运行守卫；新增 `/api/digital-employees` 路由；通用 `/api/agent` 保持普通智能体语义。前端新增 `/digital-employees` 页面，复用 `AgentView` 对话入口，并扩展现有 `AgentEditModal` 的岗位信息分组。

**Tech Stack:** FastAPI、SQLAlchemy async、PostgreSQL、pytest、Vue 3、Pinia、Ant Design Vue、Node test runner。

**Spec:** `docs/superpowers/specs/2026-10-26-enterprise-workbench-design.md`

**执行状态（2026-10-26）：** Tasks 1–10 已实现并提交。unit 2677 passed / 58 skipped；数字员工 integration + migration 8 passed；前端 eslint 0 warning、unit 470 passed、vite build 通过；工程契约与 unittest 通过。差异：本机 pnpm 包装器损坏，前端改用 `node_modules/.bin/eslint`、`node --test`、`vite build` 等价执行；deterministic E2E 因 replay 网关不可用未运行；真实浏览器创建/编辑/发布/停用/去对话未执行。详见决策记录 `docs/develop-guides/decisions/implemented/2026-10-26-digital-employee-workbench.md`。

## Global Constraints

- 部署形态为 A：单企业自建。P1 不新增 `Agent.tenant_id`；SaaS 多租户共享实例不在本计划。
- 内部命名使用 `digital_employee`；用户界面文案使用「数字员工」。
- 不新建独立员工表、不新增服务账号、不引入新依赖。
- 权限在后端依赖/repository 可见性查询处执行；前端隐藏按钮不是授权边界。
- 员工数据权限跟随调用身份；员工不会获得调用身份没有的知识库/Skills/工具权限。
- 停用只阻止新请求、新 resume、新线程、新定时任务、新 IM Bot 绑定；历史对话仍可读；运行中的 Run 不强制中断。
- 草稿对普通用户完全不可见；管理者可通过数字员工接口和通用详情读取草稿。
- 所有新增函数/类使用简洁中文 docstring；文件以一个换行结尾。
- 提交前运行 `python3 scripts/verify_engineering_contracts.py`、`python3 -m unittest scripts.test_verify_engineering_contracts`、`git diff --check`。

## Review Focus

- 普通用户通过 `/api/agent` 或 `/api/agent/{slug}` 看到草稿员工。测试必须覆盖列表过滤和详情 404。
- 停用员工的旧会话无法打开，或反而还能发起新 Run。测试必须覆盖历史 200 与新请求 409。
- 已发布员工被编辑成空岗位/空能力后仍可运行。测试必须覆盖 active 更新后重新校验。
- 通用 `/api/agent` 路由修改、删除或复制员工。测试必须覆盖 400。
- 非管理者看到员工配置的私有知识库/Skills 名称。测试必须覆盖序列化结果不含 `config_json`/`configurable_items`。
- 草稿员工被定时任务或 IM Bot 绑定后无法删除。测试必须覆盖 guard 和有引用时 409。

---

### Task 1: 数字员工领域常量、档案归一化与发布校验

**Files:**
- Create: `backend/package/yuxi/agents/digital_employee.py`
- Create: `backend/package/yuxi/services/digital_employee_service.py`
- Test: `backend/test/unit/services/test_digital_employee_service.py`

**Interfaces:**
- Produces:
  - `DIGITAL_EMPLOYEE_KIND = "digital_employee"`
  - `STATUS_DRAFT = "draft"`、`STATUS_ACTIVE = "active"`、`STATUS_DISABLED = "disabled"`
  - `normalize_profile_patch(patch: dict | None, current: dict | None = None) -> dict`
  - `validate_profile_for_publish(profile: dict) -> None`
  - `digital_employee_status(agent) -> str | None`
  - `digital_employee_is_runnable(agent) -> bool`
  - `digital_employee_can_view(user, agent) -> bool`
  - `digital_employee_is_visible(user, agent) -> bool`
  - `ensure_digital_employee_can_start(user, agent) -> None`
- Consumes: `user_can_access_agent`、`user_can_manage_agent` from `agent_repository`; `utc_now_naive`。

- [x] **Step 1: Write the failing unit test**

```python
# backend/test/unit/services/test_digital_employee_service.py
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from yuxi.services.digital_employee_service import (
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_DRAFT,
    ensure_digital_employee_can_start,
    normalize_profile_patch,
    validate_profile_for_publish,
)


def _agent(*, status="active", share_config=None, created_by="owner", slug="employee"):
    return SimpleNamespace(
        slug=slug,
        kind="digital_employee",
        created_by=created_by,
        is_subagent=False,
        share_config=share_config
        or {
            "version": 2,
            "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
            "manage_scope": None,
        },
        digital_employee_profile={
            "position": "经营分析官",
            "department": "经营管理",
            "responsibilities": "输出分析",
            "capabilities": ["数据查询"],
            "boundaries": "不代替决策",
            "acceptance": "包含数据口径",
            "status": status,
        },
    )


def _user(role="user", uid="reader"):
    return SimpleNamespace(uid=uid, role=role, department_id=1, tenant_department_id=None)


def test_normalize_profile_patch_preserves_publish_metadata_and_ignores_client_status():
    current = {
        "position": "旧岗位",
        "capabilities": ["旧能力"],
        "status": STATUS_ACTIVE,
        "published_at": "2026-10-26T10:00:00",
        "published_by": "admin",
    }
    merged = normalize_profile_patch(
        {"position": " 新岗位 ", "capabilities": [" 数据查询 ", "数据查询", "报告生成"], "status": "draft"},
        current,
    )
    assert merged["position"] == "新岗位"
    assert merged["capabilities"] == ["数据查询", "报告生成"]
    assert merged["status"] == STATUS_ACTIVE
    assert merged["published_at"] == "2026-10-26T10:00:00"
    assert merged["published_by"] == "admin"


def test_validate_profile_for_publish_rejects_missing_position_and_capabilities():
    with pytest.raises(ValueError, match="岗位"):
        validate_profile_for_publish(normalize_profile_patch({"capabilities": ["数据查询"]}))
    with pytest.raises(ValueError, match="能力"):
        validate_profile_for_publish(
            normalize_profile_patch({"position": "经营分析官", "department": "经营管理", "capabilities": []})
        )


def test_digital_employee_start_guard_hides_draft_from_non_manager_and_blocks_disabled():
    draft = _agent(status=STATUS_DRAFT)
    disabled = _agent(status=STATUS_DISABLED)
    active = _agent(status=STATUS_ACTIVE)

    with pytest.raises(HTTPException) as hidden:
        ensure_digital_employee_can_start(_user(), draft)
    assert hidden.value.status_code == 404

    with pytest.raises(HTTPException) as blocked:
        ensure_digital_employee_can_start(_user(), disabled)
    assert blocked.value.status_code == 409

    ensure_digital_employee_can_start(_user(), active)

    owner = _user(uid="owner")
    with pytest.raises(HTTPException) as manager_blocked:
        ensure_digital_employee_can_start(owner, draft)
    assert manager_blocked.value.status_code == 409
```

- [x] **Step 2: Run test to verify it fails**

Run: `docker compose exec api uv run --group test pytest test/unit/services/test_digital_employee_service.py -v`
Expected: FAIL with `ModuleNotFoundError` / missing functions.

- [x] **Step 3: Implement constants and pure profile functions**

Create `backend/package/yuxi/agents/digital_employee.py` with the three status constants and `DIGITAL_EMPLOYEE_KIND`. Create `digital_employee_service.py` with:

```python
TEXT_LIMITS = {"position": 80, "department": 80, "responsibilities": 2000, "boundaries": 2000, "acceptance": 2000}
SERVER_FIELDS = ("status", "published_at", "published_by", "disabled_at", "disabled_by")

def normalize_profile_patch(patch, current=None):
    current = dict(current or {})
    result = {field: str(current.get(field) or "").strip() for field in TEXT_LIMITS}
    result["capabilities"] = _normalize_capabilities(current.get("capabilities") or [])
    for field in TEXT_LIMITS:
        if field in (patch or {}):
            result[field] = _clean_text(patch[field], field, TEXT_LIMITS[field])
    if "capabilities" in (patch or {}):
        result["capabilities"] = _normalize_capabilities(patch["capabilities"])
    for field in SERVER_FIELDS:
        result[field] = current.get(field)
    result.setdefault("status", STATUS_DRAFT)
    return result

def validate_profile_for_publish(profile):
    missing = [label for field, label in (("position", "岗位"), ("department", "部门"),
               ("responsibilities", "职责"), ("boundaries", "边界"), ("acceptance", "验收")) if not profile.get(field)]
    if missing:
        raise ValueError("发布前请填写：" + "、".join(missing))
    if not profile.get("capabilities"):
        raise ValueError("发布前至少填写一项能力")
```

`_normalize_capabilities` trims, drops empties, de-duplicates preserving order, rejects non-list, rejects each >40, rejects >12. `_clean_text` enforces max length.

- [x] **Step 4: Implement visibility/start guards**

`digital_employee_status` returns `None` for non-employees. `digital_employee_is_runnable` is `kind != employee or status == active`. `digital_employee_can_view` returns true for non-employees, otherwise access plus (`active`/`disabled` or manage). `digital_employee_is_visible` returns true for non-employees, otherwise access plus (`active` or manage). `ensure_digital_employee_can_start` raises 404 when `can_view` is false and 409 when not runnable.

- [x] **Step 5: Run test to verify it passes**

Run: `docker compose exec api uv run --group test pytest test/unit/services/test_digital_employee_service.py -v`
Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add backend/package/yuxi/agents/digital_employee.py backend/package/yuxi/services/digital_employee_service.py backend/test/unit/services/test_digital_employee_service.py
git commit -m "feat: 增加数字员工档案领域规则"
```

---

### Task 2: Agent 持久化字段、仓储能力与幂等迁移

**Files:**
- Modify: `backend/package/yuxi/storage/postgres/models_business.py:300-350`
- Modify: `backend/package/yuxi/storage/postgres/manager.py:1013-1045`
- Modify: `backend/package/yuxi/repositories/agent_repository.py:236-360`
- Test: `backend/test/unit/repositories/test_agent_repository.py`
- Test: `backend/test/integration/api/test_digital_employee_migration.py`

**Interfaces:**
- Produces:
  - `Agent.kind: str`、`Agent.digital_employee_profile: dict`
  - `AgentRepository.list_by_kind(kind: str) -> list[Agent]`
  - `AgentRepository.get_by_slug_for_update(slug: str) -> Agent | None`
  - `AgentRepository.count_digital_employee_references(slug: str) -> dict[str, int]`
  - `AgentRepository.create(..., kind: str = "agent", digital_employee_profile: dict | None = None)`
  - `AgentRepository.update(..., digital_employee_profile: dict | None = None)`

- [x] **Step 1: Write the failing unit test**

```python
def test_agent_defaults_to_normal_agent_and_serializes_profile():
    from yuxi.storage.postgres.models_business import Agent

    agent = Agent(slug="employee", backend_id="ChatbotAgent", name="员工", share_config={})
    assert agent.kind is None or agent.kind == "agent"
    data = Agent(
        slug="employee",
        backend_id="ChatbotAgent",
        name="员工",
        share_config={},
        kind="digital_employee",
        digital_employee_profile={"status": "draft", "position": "经营分析官"},
    ).to_dict()
    assert data["kind"] == "digital_employee"
    assert data["digital_employee_profile"]["position"] == "经营分析官"
```

- [x] **Step 2: Run test to verify it fails**

Run: `docker compose exec api uv run --group test pytest test/unit/repositories/test_agent_repository.py -k digital_employee -v`
Expected: FAIL because `kind` is not a mapped attribute or `to_dict` lacks it.

- [x] **Step 3: Add model columns and serialization**

Add to `Agent`:

```python
kind = Column(String(32), nullable=False, default="agent", index=True)
digital_employee_profile = Column(JSON_VALUE, nullable=False, default=dict)
```

Add `"kind": self.kind or "agent"` and `"digital_employee_profile": self.digital_employee_profile or {}` to `to_dict()`.

- [x] **Step 4: Add repository methods and parameters**

In `AgentRepository.create`, add `kind` and `digital_employee_profile` parameters, set them on the `Agent(...)`. In `update`, add `digital_employee_profile: dict | None = None` and set it when not `None`. Add:

```python
async def list_by_kind(self, *, kind: str) -> list[Agent]:
    result = await self.db.execute(select(Agent).where(Agent.kind == kind).order_by(Agent.id.asc()))
    return list(result.scalars().all())

async def get_by_slug_for_update(self, *, slug: str) -> Agent | None:
    result = await self.db.execute(select(Agent).where(Agent.slug == slug).with_for_update())
    return result.scalar_one_or_none()

async def count_digital_employee_references(self, *, slug: str) -> dict[str, int]:
    # count conversations, scheduled_agent_jobs, im_bot_configs by agent slug
```

- [x] **Step 5: Add migration statements**

In `manager.py` `ensure_business_schema`, after the agents table DDL, add:

```python
"ALTER TABLE IF EXISTS agents ADD COLUMN IF NOT EXISTS kind VARCHAR(32) NOT NULL DEFAULT 'agent'",
"ALTER TABLE IF EXISTS agents ADD COLUMN IF NOT EXISTS digital_employee_profile JSONB NOT NULL DEFAULT '{}'::jsonb",
"CREATE INDEX IF NOT EXISTS ix_agents_kind ON agents (kind)",
```

Add `kind VARCHAR(32) NOT NULL DEFAULT 'agent'` and `digital_employee_profile JSONB NOT NULL DEFAULT '{}'::jsonb` to the `CREATE TABLE IF NOT EXISTS agents` block.

- [x] **Step 6: Write the migration integration test**

```python
# backend/test/integration/api/test_digital_employee_migration.py
import pytest
from sqlalchemy import text

from yuxi.storage.postgres.manager import pg_manager

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_digital_employee_migration_is_idempotent_and_preserves_existing_agents():
    pg_manager.initialize()
    await pg_manager.ensure_business_schema()
    await pg_manager.ensure_business_schema()
    async with pg_manager.async_engine.connect() as conn:
        columns = await conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='agents'"
        ))
        names = {row[0] for row in columns}
        assert {"kind", "digital_employee_profile"} <= names
        non_agent = await conn.scalar(text("SELECT COUNT(*) FROM agents WHERE kind <> 'agent'"))
        assert non_agent == 0
```

- [x] **Step 7: Run tests**

Run: `docker compose exec api uv run --group test pytest test/unit/repositories/test_agent_repository.py -k digital_employee -v`
Expected: PASS.

Run: `docker compose exec api uv run --group test pytest test/integration/api/test_digital_employee_migration.py -v`
Expected: PASS.

- [x] **Step 8: Commit**

```bash
git add backend/package/yuxi/storage/postgres/models_business.py backend/package/yuxi/storage/postgres/manager.py backend/package/yuxi/repositories/agent_repository.py backend/test/unit/repositories/test_agent_repository.py backend/test/integration/api/test_digital_employee_migration.py
git commit -m "feat: 为 Agent 增加数字员工档案字段与迁移"
```

---

### Task 3: 数字员工生命周期服务

**Files:**
- Modify: `backend/package/yuxi/services/digital_employee_service.py`
- Test: `backend/test/unit/services/test_digital_employee_service.py`
- Test: `backend/test/integration/api/test_digital_employee_api.py`

**Interfaces:**
- Produces:
  - `DEFAULT_EMPLOYEE_SHARE_CONFIG`
  - `list_digital_employees(*, user, db, status=None, q=None) -> tuple[list[Agent], dict[str, int]]`
  - `get_digital_employee(*, user, db, slug) -> Agent | None`
  - `get_manageable_digital_employee(*, user, db, slug) -> Agent`
  - `create_digital_employee(*, user, db, name, backend_id, slug=None, description=None, icon=None, profile=None, config_json=None, share_config=None) -> Agent`
  - `update_digital_employee(*, user, db, agent, changes: dict) -> Agent`
  - `publish_digital_employee(*, user, db, agent) -> Agent`
  - `disable_digital_employee(*, user, db, agent) -> Agent`
  - `delete_digital_employee(*, user, db, agent) -> None`
- Consumes: `AgentRepository` methods from Task 2; `prepare_agent_config_write`; `get_agent_backend`; `SUB_AGENT_BACKEND_ID`.

- [x] **Step 1: Write the failing lifecycle test**

Add to `test_digital_employee_service.py`:

```python
def test_normalize_profile_patch_rejects_too_many_capabilities():
    with pytest.raises(ValueError, match="12"):
        normalize_profile_patch({"capabilities": [f"能力{i}" for i in range(13)]})
```

This is a small pure test; the DB lifecycle is covered by integration in Task 8. The service implementation must still be written now so the router can use it.

- [x] **Step 2: Run test to verify it fails**

Run: `docker compose exec api uv run --group test pytest test/unit/services/test_digital_employee_service.py -v`
Expected: FAIL because the capability limit is not implemented.

- [x] **Step 3: Implement lifecycle functions**

Implementation rules:

- `create_digital_employee`: reject `backend_id == SUB_AGENT_BACKEND_ID`; normalize profile and force `status=draft`; `published_at/by` and `disabled_at/by` are `None`; call `prepare_agent_config_write`; use `DEFAULT_EMPLOYEE_SHARE_CONFIG` when `share_config` is omitted; `repo.create(..., kind=DIGITAL_EMPLOYEE_KIND, digital_employee_profile=profile, creator=user)`.
- `list_digital_employees`: `repo.list_by_kind(kind=DIGITAL_EMPLOYEE_KIND)`, filter with `digital_employee_is_visible`, compute `{"active": n, "draft": n, "disabled": n}` before status/q filters, sort active first then name; `q` matches name/position/department/responsibilities/capabilities case-insensitively.
- `get_digital_employee`: return `None` for non-employee or invisible.
- `get_manageable_digital_employee`: 404 for missing/invisible; 403 when `user_can_manage_agent` is false.
- `update_digital_employee`: lock via `repo.get_by_slug_for_update`; reject unknown keys; normalize profile; when current status is active, `validate_profile_for_publish` on the merged profile; preserve status; call `prepare_agent_config_write` when `config_json` is provided; set `description`/`icon` even when `None`; call `repo.update(..., digital_employee_profile=profile, updater=user)`.
- `publish_digital_employee`: lock; if already active return; validate; set `status=active`, `published_at=utc_now_naive().isoformat()`, `published_by=str(user.uid)`, clear disabled fields; `repo.update`.
- `disable_digital_employee`: lock; set `status=disabled`, `disabled_at`, `disabled_by`; `repo.update`; idempotent.
- `delete_digital_employee`: lock; 409 unless status is draft; `count_digital_employee_references`; 409 if any count > 0; `repo.delete`.

Use `HTTPException(409, detail="...")` for state/reference conflicts and `HTTPException(403, ...)` for manage denial.

- [x] **Step 4: Run unit test**

Run: `docker compose exec api uv run --group test pytest test/unit/services/test_digital_employee_service.py -v`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add backend/package/yuxi/services/digital_employee_service.py backend/test/unit/services/test_digital_employee_service.py
git commit -m "feat: 增加数字员工生命周期服务"
```

---

### Task 4: 数字员工 HTTP 路由与通用 Agent 隔离

**Files:**
- Create: `backend/server/routers/digital_employee_router.py`
- Modify: `backend/server/routers/__init__.py`
- Modify: `backend/server/routers/agent_router.py:135-300`
- Modify: `backend/package/yuxi/services/agent_config_service.py:55-94`
- Test: `backend/test/integration/api/test_digital_employee_api.py`

**Interfaces:**
- Produces:
  - `digital_employee_router = APIRouter(prefix="/digital-employees", tags=["digital-employee"])`
  - `GET /api/digital-employees` → `{"employees": [...], "summary": {...}}`
  - `GET /api/digital-employees/{slug}` → `{"employee": {...}}`
  - `POST /api/digital-employees` → `{"employee": {...}}`
  - `PUT /api/digital-employees/{slug}` → `{"employee": {...}}`
  - `POST /api/digital-employees/{slug}/publish` → `{"employee": {...}}`
  - `POST /api/digital-employees/{slug}/disable` → `{"employee": {...}}`
  - `DELETE /api/digital-employees/{slug}` → `{"success": true}`
- Consumes: Task 3 service functions; `get_admin_user`、`get_required_user`、`get_db`; `AgentRepository.serialize`.

- [x] **Step 1: Write the failing HTTP smoke test**

Add to `test_digital_employee_api.py`:

```python
async def test_standard_user_cannot_create_digital_employee(test_client, standard_user):
    response = await test_client.post(
        "/api/digital-employees",
        headers=standard_user["headers"],
        json={"name": "越权员工", "profile": {"position": "岗位"}},
    )
    assert response.status_code == 403, response.text
```

- [x] **Step 2: Run test to verify it fails**

Run with test credentials (see Task 8 setup): `docker compose exec api uv run --group test pytest test/integration/api/test_digital_employee_api.py::test_standard_user_cannot_create_digital_employee -v`
Expected: FAIL with 404 (route missing).

- [x] **Step 3: Implement the router**

Define Pydantic models `DigitalEmployeeProfile`, `DigitalEmployeeCreate`, `DigitalEmployeeUpdate`. Use `model_fields_set` for optional `description`/`icon`. Serialize with `AgentRepository.serialize`; for non-managers remove `config_json` and `configurable_items`. Managers get `include_configurable_items=True` on detail and update responses. List always omits `configurable_items`; managers keep `config_json` only on detail.

Route behavior:

- `GET ""`: `status: str | None = Query(None)`, `q: str | None = Query(None)`; call list service; return summary.
- `GET "/{slug}"`: 404 when service returns `None`; serialize with `include_configurable_items=can_manage`.
- `POST ""`: `Depends(get_admin_user)`; call create; 422 on `ValueError`.
- `PUT "/{slug}"`: `get_manageable_digital_employee`; build `changes` from `payload.model_dump(exclude_unset=True)`; for `profile`, use `payload.profile.model_dump(exclude_unset=True)`; 422 on `ValueError`.
- `POST "/{slug}/publish"` and `"/disable"`: manage check; call service; 422 on `ValueError`.
- `DELETE "/{slug}"`: manage check; call delete; 409 propagates.

Register `digital_employee_router` in `backend/server/routers/__init__.py` immediately after `agent_router`.

- [x] **Step 4: Add generic Agent isolation**

In `agent_router.list_agents`:

```python
kind: str | None = Query(None)
...
if kind not in (None, "agent", "digital_employee"):
    raise HTTPException(status_code=422, detail="kind 只支持 agent 或 digital_employee")
items = await repo.list_visible(...)
if kind:
    items = [item for item in items if (item.kind or "agent") == kind]
items = [item for item in items if digital_employee_is_runnable(item)]
```

In `get_agent`, after loading `item`: if `item.kind == DIGITAL_EMPLOYEE_KIND` and not `digital_employee_can_view(current_user, item)`, return 404. In `update_agent` and `delete_agent`, after loading `item`: if `item.kind == DIGITAL_EMPLOYEE_KIND`, return 400 with detail `数字员工请使用 /api/digital-employees 接口`.

In `agent_config_service.duplicate_agent`, after source is loaded and access is confirmed, reject employees with 400 `数字员工不能另存为普通智能体` (local import the constant to avoid a module cycle).

- [x] **Step 5: Run test**

Run: `docker compose exec api uv run --group test pytest test/integration/api/test_digital_employee_api.py::test_standard_user_cannot_create_digital_employee -v`
Expected: PASS (403).

- [x] **Step 6: Commit**

```bash
git add backend/server/routers/digital_employee_router.py backend/server/routers/__init__.py backend/server/routers/agent_router.py backend/package/yuxi/services/agent_config_service.py backend/test/integration/api/test_digital_employee_api.py
git commit -m "feat: 增加数字员工 API 与通用 Agent 隔离"
```

---

### Task 5: 运行时入口守卫

**Files:**
- Modify: `backend/package/yuxi/services/agent_request_service.py:100-107`
- Modify: `backend/package/yuxi/services/agent_run_service.py:693-697`
- Modify: `backend/package/yuxi/services/conversation_service.py:95-97`
- Modify: `backend/package/yuxi/services/scheduled_agent_service.py:101-110`
- Modify: `backend/package/yuxi/services/im_bot_service.py:107-178`
- Test: `backend/test/unit/services/test_digital_employee_service.py`
- Test: `backend/test/integration/api/test_digital_employee_api.py`

**Interfaces:**
- Consumes: `ensure_digital_employee_can_start(user, agent)` from Task 1.
- Produces: 404 for invisible drafts, 409 for visible non-active employees on new requests; invalid IM Bot agent references fail explicitly.

- [x] **Step 1: Write the failing unit test for runnable guard**

Add to `test_digital_employee_service.py`:

```python
def test_ensure_can_start_allows_non_employee_agents():
    from yuxi.services.digital_employee_service import ensure_digital_employee_can_start
    agent = SimpleNamespace(kind="agent", share_config={}, created_by="owner", slug="normal")
    ensure_digital_employee_can_start(_user(), agent)
```

- [x] **Step 2: Run test to verify it fails/passes**

Run: `docker compose exec api uv run --group test pytest test/unit/services/test_digital_employee_service.py -k non_employee -v`
Expected: PASS after Task 1; this pins the non-employee path. If it fails, fix `ensure_digital_employee_can_start` to return early for non-employees.

- [x] **Step 3: Insert guards**

- `submit_agent_request`: after `agent_item` is loaded, call `ensure_digital_employee_can_start(current_user, agent_item)`.
- `prepare_agent_run_creation_scope`: after `agent_item` is loaded, call `ensure_digital_employee_can_start(current_user, agent_item)`. This covers both chat and resume.
- `create_thread_view`: after `agent_item` is loaded, call guard.
- `scheduled_agent_service._validate_agent`: after `agent` is loaded and backend checked, call guard.
- `im_bot_service`: add `async def _validate_agent_binding(db, agent_slug, owner)` that loads `AgentRepository.get_visible_by_slug(kind="main")`, raises `ValueError("智能体不存在或不可访问")` when missing, and calls the guard. Call it in `create_im_bot` after owner resolution, and in `update_im_bot` when `agent_slug` or `owner_uid` is provided. Use the effective owner (`new owner` if changed, else `bot.owner_uid`).

- [x] **Step 4: Add an integration guard test**

In `test_digital_employee_api.py`:

```python
async def test_disabled_employee_rejects_new_run_but_keeps_history(test_client, admin_headers):
    # create + publish employee, create thread, disable, POST /api/agent/runs -> 409,
    # GET /api/agent/{slug} -> 200 and GET /api/chat/thread/{thread_id}/history -> 200.
```

Use `POST /api/chat/thread` with `{"agent_id": slug, "title": ...}`; clean up the thread in `finally`. Do not submit an active run (avoid model cost); only assert the disabled guard.

- [x] **Step 5: Run tests**

Run: `docker compose exec api uv run --group test pytest test/unit/services/test_digital_employee_service.py -v`
Expected: PASS.

Run: `docker compose exec api uv run --group test pytest test/integration/api/test_digital_employee_api.py -k disabled -v`
Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add backend/package/yuxi/services/agent_request_service.py backend/package/yuxi/services/agent_run_service.py backend/package/yuxi/services/conversation_service.py backend/package/yuxi/services/scheduled_agent_service.py backend/package/yuxi/services/im_bot_service.py backend/test/unit/services/test_digital_employee_service.py backend/test/integration/api/test_digital_employee_api.py
git commit -m "feat: 在运行时入口拦截未发布和已停用数字员工"
```

---

### Task 6: 前端 API 与展示工具

**Files:**
- Create: `web/src/apis/digital_employee_api.js`
- Create: `web/src/utils/digitalEmployee.js`
- Test: `web/test/unit/digitalEmployee.test.js`
- Modify: `web/src/apis/agent_api.js`

**Interfaces:**
- Produces:
  - `digitalEmployeeApi.listDigitalEmployees(params)`
  - `digitalEmployeeApi.getDigitalEmployee(slug)`
  - `digitalEmployeeApi.createDigitalEmployee(payload)`
  - `digitalEmployeeApi.updateDigitalEmployee(slug, payload)`
  - `digitalEmployeeApi.publishDigitalEmployee(slug)`
  - `digitalEmployeeApi.disableDigitalEmployee(slug)`
  - `digitalEmployeeApi.deleteDigitalEmployee(slug)`
  - `normalizeDigitalEmployee(agent)`
  - `digitalEmployeeStatusMeta(status)`
  - `digitalEmployeeCapabilities(employee)`
  - `digitalEmployeeSearchText(employee)`
  - `agentApi.getAgents({ includeSubagents, kind })`

- [x] **Step 1: Write the failing utility test**

```javascript
// web/test/unit/digitalEmployee.test.js
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  digitalEmployeeCapabilities,
  digitalEmployeeSearchText,
  digitalEmployeeStatusMeta,
  normalizeDigitalEmployee
} from '../../src/utils/digitalEmployee.js'

test('normalizeDigitalEmployee 统一 slug/id/profile 并补齐草稿状态', () => {
  const employee = normalizeDigitalEmployee({
    slug: 'analyst',
    name: '经营分析官',
    digital_employee_profile: { position: '经营分析官', capabilities: ['数据查询'] }
  })
  assert.equal(employee.id, 'analyst')
  assert.equal(employee.slug, 'analyst')
  assert.equal(employee.profile.status, 'draft')
  assert.deepEqual(digitalEmployeeCapabilities(employee), ['数据查询'])
  assert.match(digitalEmployeeSearchText(employee), /经营分析官/)
  assert.equal(digitalEmployeeStatusMeta('active').label, '在岗')
  assert.equal(digitalEmployeeStatusMeta('disabled').label, '已停用')
})
```

- [x] **Step 2: Run test to verify it fails**

Run: `docker compose exec web pnpm run test:unit -- digitalEmployee.test.js`
Expected: FAIL with module not found.

- [x] **Step 3: Implement the API and util**

`digital_employee_api.js` uses `apiGet`/`apiPost`/`apiPut`/`apiDelete` from `./base`; encodes slug with `encodeURIComponent`; builds query string for `status` and `q`.

`digitalEmployee.js`:

```js
export const DIGITAL_EMPLOYEE_STATUS = Object.freeze({
  draft: { label: '草稿', tone: 'warning' },
  active: { label: '在岗', tone: 'success' },
  disabled: { label: '已停用', tone: 'muted' }
})

export function normalizeDigitalEmployee(agent) {
  const profile = agent?.digital_employee_profile || agent?.profile || {}
  const slug = agent?.slug || agent?.agent_id || agent?.id
  return {
    ...agent,
    id: agent?.id || slug,
    slug,
    kind: agent?.kind || 'digital_employee',
    profile: {
      position: '',
      department: '',
      responsibilities: '',
      capabilities: [],
      boundaries: '',
      acceptance: '',
      status: 'draft',
      ...profile
    }
  }
}

export function digitalEmployeeStatusMeta(status) {
  return DIGITAL_EMPLOYEE_STATUS[status] || DIGITAL_EMPLOYEE_STATUS.draft
}

export function digitalEmployeeCapabilities(employee) {
  return Array.isArray(employee?.profile?.capabilities) ? employee.profile.capabilities.filter(Boolean) : []
}

export function digitalEmployeeSearchText(employee) {
  const profile = employee?.profile || {}
  return [employee?.name, profile.position, profile.department, profile.responsibilities,
    ...(profile.capabilities || [])].filter(Boolean).join(' ').toLowerCase()
}
```

Modify `agentApi.getAgents` to accept `kind` and set `params.set('kind', kind)`.

- [x] **Step 4: Run test to verify it passes**

Run: `docker compose exec web pnpm run test:unit -- digitalEmployee.test.js`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add web/src/apis/digital_employee_api.js web/src/utils/digitalEmployee.js web/test/unit/digitalEmployee.test.js web/src/apis/agent_api.js
git commit -m "feat: 增加数字员工前端 API 与展示工具"
```

---

### Task 7: 数字员工页面、导航与路由

**Files:**
- Create: `web/src/views/DigitalEmployeeView.vue`
- Modify: `web/src/router/index.js`
- Modify: `web/src/layouts/AppLayout.vue:1-185`
- Test: `web/test/unit/digitalEmployee.test.js` (utility coverage already added)

**Interfaces:**
- Produces: `/digital-employees` route with `name: DigitalEmployeeComp`; nav item「数字员工」after「智能体」; page consumes Task 6 API/util and `AgentEditModal` with `digital-employee`.

- [x] **Step 1: Add the route and nav item**

In `router/index.js`, add an `AppLayout` child route:

```javascript
{
  path: '/digital-employees',
  name: 'digital-employees',
  component: AppLayout,
  children: [
    {
      path: '',
      name: 'DigitalEmployeeComp',
      component: () => import('../views/DigitalEmployeeView.vue'),
      meta: { keepAlive: false, requiresAuth: true }
    }
  ]
}
```

In `AppLayout.vue`, import `Users` from `@lucide/vue` and push after 智能体:

```javascript
items.push({ name: '数字员工', path: '/digital-employees', icon: Users, activeIcon: Users })
```

- [x] **Step 2: Implement the page**

Load `digitalEmployeeApi.listDigitalEmployees()` and `agentApi.getAgentBackends()` for managers. Use `PageHeader` (title「数字员工」, info「N 个在岗 / M 个草稿」), `PageShoulder` (search + status filter + 配置数字员工 for admins), `ExtensionCardGrid` + `InfoCard`. Card actions: 「详情」and「去对话」; detail drawer shows profile, share label, published time, capability tags, boundaries, acceptance. Managers see edit/publish/disable/delete-draft actions and a runtime resource summary from `config_json.context` (model/knownledges/skills counts); non-managers see the note「运行时资源按你的权限解析」and no resource names.

Use `getShareConfigLabel` from `@/utils/shareConfig`. Use `Modal.confirm` for publish/disable/delete. After mutations call `loadEmployees()`.

- [x] **Step 3: Build check**

Run: `docker compose exec web pnpm run lint:check`
Expected: PASS.

Run: `docker compose exec web pnpm run build`
Expected: PASS.

- [x] **Step 4: Commit**

```bash
git add web/src/views/DigitalEmployeeView.vue web/src/router/index.js web/src/layouts/AppLayout.vue
git commit -m "feat: 增加数字员工页面与导航"
```

---

### Task 8: 扩展 AgentEditModal 的岗位信息与员工保存分支

**Files:**
- Modify: `web/src/components/model-management/AgentEditModal.vue`
- Modify: `backend/test/integration/api/test_digital_employee_api.py` (full lifecycle/permissions/isolation tests)

**Interfaces:**
- Consumes: `digitalEmployeeApi`、`normalizeDigitalEmployee`; props `digitalEmployee: Boolean`.
- Produces: `openCreate()` / `openEdit(agent)` work for employees; save calls `/api/digital-employees`; ordinary Agent behavior unchanged.

- [x] **Step 1: Write the failing integration tests**

Replace the single smoke test with the full suite in `test_digital_employee_api.py`:

1. `test_digital_employee_draft_hidden_from_standard_user_and_readable_by_manager`
2. `test_digital_employee_publish_then_visible_and_generic_agent_isolation`
3. `test_digital_employee_disable_blocks_new_run_and_keeps_history`
4. `test_digital_employee_active_update_rejects_empty_profile`
5. `test_digital_employee_delete_draft_with_reference_returns_409`
6. `test_digital_employee_non_manager_detail_hides_private_resources`

Use the `admin_headers` and `standard_user` fixtures. Create with `share_config` scoped to the standard user where a negative visibility assertion is needed. Cleanup drafts with `DELETE /api/digital-employees/{slug}`; for published employees, disable them and delete their conversations. Assert persisted state by re-reading the API, not only status codes.

- [x] **Step 2: Run tests to verify they fail**

Run with credentials (see setup below): `docker compose exec api uv run --group test pytest test/integration/api/test_digital_employee_api.py -v`
Expected: FAIL on missing UI-independent backend pieces or missing guard behavior; fix backend first.

- [x] **Step 3: Extend the modal**

Add `digitalEmployee` prop. Add `employeeProfileForm` (position, department, responsibilities, capabilitiesText, boundaries, acceptance) and `employeeProfileBaseline`. Include it in `snapshotProfile`/`hasProfileChanges`/`captureProfileBaseline`/`resetAgentForm`. In `openEdit`, set the form from `detail.digital_employee_profile`. In `buildAgentPayload`, add `profile: { ..., capabilities: splitCapabilities(employeeProfileForm.capabilitiesText) }` when `digitalEmployee`. In `saveAgent`, branch:

```javascript
if (props.digitalEmployee) {
  const response = editingAgentId.value
    ? await digitalEmployeeApi.updateDigitalEmployee(editingAgentId.value, payload)
    : await digitalEmployeeApi.createDigitalEmployee(payload)
  emit('saved', { mode: editingAgentId.value ? 'edit' : 'create', agent: response.employee })
  message.success(editingAgentId.value ? '数字员工已保存' : '数字员工已创建')
} else { /* existing agentStore path */ }
```

Title becomes「配置数字员工」/「编辑数字员工」when `digitalEmployee`. Add profile fields to the basic section, after description, with the section heading「岗位信息」. Keep share config and runtime config unchanged. Update `restoreChatAgentSelectionIfNeeded` so closing an employee edit restores a normal chat agent.

- [x] **Step 4: Run frontend tests and build**

Run: `docker compose exec web pnpm run test:unit -- digitalEmployee.test.js`
Expected: PASS.

Run: `docker compose exec web pnpm run lint:check`
Expected: PASS.

Run: `docker compose exec web pnpm run build`
Expected: PASS.

- [x] **Step 5: Run backend integration tests**

Set up a temporary superadmin (do not commit credentials):

```bash
docker compose exec -T api python - <<'PY'
import asyncio, os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from yuxi.utils.auth_utils import AuthUtils

async def main():
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO users (username, uid, password_hash, role, department_id, is_deleted)
            VALUES (:uid, :uid, :hash, 'superadmin', 1, 0)
            ON CONFLICT (uid) DO UPDATE SET password_hash = EXCLUDED.password_hash,
              role = 'superadmin', department_id = 1, is_deleted = 0
        """), {"uid": "pytest-digital-admin", "hash": AuthUtils.hash_password("Pytest!Digital1")})
    await engine.dispose()

asyncio.run(main())
PY
```

Run:

```bash
docker compose exec -T -e TEST_USERNAME=pytest-digital-admin -e TEST_PASSWORD='Pytest!Digital1' api uv run --group test pytest test/integration/api/test_digital_employee_api.py test/integration/api/test_digital_employee_migration.py -v
```

Expected: PASS. Clean up the temporary admin with `DELETE FROM users WHERE uid='pytest-digital-admin'` after the run.

- [x] **Step 6: Commit**

```bash
git add web/src/components/model-management/AgentEditModal.vue backend/test/integration/api/test_digital_employee_api.py
git commit -m "feat: 支持在员工页面配置岗位并保存数字员工"
```

---

### Task 9: E2E 数字员工对话链路

**Files:**
- Create: `backend/test/e2e/test_digital_employee_e2e.py`
- Modify: `backend/test/integration/api/test_digital_employee_api.py` if cleanup helpers are needed.

**Interfaces:**
- Consumes: `e2e_client`、`e2e_headers`、`e2e_helpers.wait_for_run`; `POST /api/system/model-providers`; `/api/digital-employees`; `/api/chat/thread`; `/api/agent/runs`.
- Produces: a slow E2E that proves create→publish→thread→Run completed→output, using the deterministic replay model.

- [x] **Step 1: Write the E2E test**

Use a unique provider `ci-replay-digital`, base URL `http://api:8765/v1`, model `deterministic-chat`. Create an employee with `config_json: {"context": {"model": "ci-replay-digital:deterministic-chat"}}`, publish it, create a thread, submit `只输出 DETERMINISTIC_AGENT_E2E_OK`, wait for the run to complete, and assert the result output. Cleanup: delete the thread, delete the provider, and remove the employee row via direct PostgreSQL cleanup (published employees are not deletable through the product API by design).

- [ ] **Step 2: Run the E2E when replay is available**

Run: `docker compose exec -T -e E2E_USERNAME=pytest-digital-admin -e E2E_PASSWORD='Pytest!Digital1' api uv run --group test pytest test/e2e/test_digital_employee_e2e.py -m e2e -v`
Expected: PASS. If the replay service at `http://api:8765` is not running, record「Not run: deterministic replay gateway unavailable」in the final report. 当前结果：`http://localhost:8765/health` 不可达，pytest 明确 skip，未写成通过。

- [x] **Step 3: Commit**

```bash
git add backend/test/e2e/test_digital_employee_e2e.py
git commit -m "test: 增加数字员工 deterministic E2E"
```

---

### Task 10: 文档收敛、决策记录迁移与完整验证

**Files:**
- Modify: `docs/superpowers/specs/2026-10-26-enterprise-workbench-design.md`
- Move: `docs/develop-guides/decisions/proposed/2026-10-26-digital-employee-workbench.md` → `docs/develop-guides/decisions/implemented/2026-10-26-digital-employee-workbench.md`
- Modify: `docs/develop-guides/decisions/implemented/2026-10-26-digital-employee-workbench.md`

**Interfaces:**
- Produces: current design/decision facts matching the shipped code; no stale proposed checklist.

- [x] **Step 1: Update the design doc**

Record the final implementation facts: dedicated `/api/digital-employees` router; `GET /api/agent/{slug}` allows managers to read drafts but normal users 404; active employees appear in runtime catalog; duplicate employees rejected; frontend page + extended modal. Change status to `implemented`.

- [x] **Step 2: Move and rewrite the decision record**

Move to `implemented/`, change status to `implemented`, and use the required sections `## 问题`、`## 决策`、`## 替代方案`、`## 后果`、`## 验证`. Keep the acceptance matrix as `## 验证` evidence, replacing `Not run` with actual command results. Do not keep proposal/checklist wording.

- [x] **Step 3: Run the full verification set**

```bash
python3 scripts/verify_engineering_contracts.py
python3 -m unittest scripts.test_verify_engineering_contracts
docker compose exec api uv run --group test pytest test/unit -m "not slow"
docker compose exec api uv run --group test pytest test/integration/api/test_digital_employee_api.py test/integration/api/test_digital_employee_migration.py -v
docker compose exec web pnpm run lint:check
docker compose exec web pnpm run test:unit
docker compose exec web pnpm run build
git diff --check
```

Expected: all commands pass; E2E either passes or is recorded as not run with the replay gateway reason.

- [x] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-10-26-enterprise-workbench-design.md docs/develop-guides/decisions/implemented/2026-10-26-digital-employee-workbench.md
git commit -m "docs: 记录数字员工 P1 实现与验证"
```

---

## Self-Review

- **Spec coverage:** Every P1 goal/非目标 in the design maps to Tasks 1–10. P2/P3 remain boundary-only and are not implemented, as the spec requires.
- **Type consistency:** `kind`/`digital_employee_profile` names are consistent across model, repository, service, router, and frontend. Status constants are defined once in `yuxi.agents.digital_employee` and imported by the service/router.
- **Review Focus mapping:** draft visibility → Task 8 integration; disabled history/new run → Task 5 integration; active update validation → Task 8; generic isolation → Task 4/8; private resource leak → Task 4/8; draft references → Task 3/8.
- **Proportion:** The plan names files, signatures, and assertions; it leaves UI layout, test fixture details, and error copy to the implementer.
