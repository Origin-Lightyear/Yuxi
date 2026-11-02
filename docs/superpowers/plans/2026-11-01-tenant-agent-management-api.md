# 租户平台 Agent 管理 API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 为租户平台提供按租户隔离的数字员工与老板分身生命周期管理 API，并生成可直接对接的 Markdown 文档。

**Architecture:** 给 `agents` 增加可空 `tenant_id`，复用租户知识库的静态 Key 鉴权依赖；新增 `/api/tenant/digital-employees` 与 `/api/tenant/founder-twins` 两个薄路由，业务继续落在现有 service，查询落在 repository。普通员工侧通过权限解析器继承租户隔离。

**Tech Stack:** FastAPI、SQLAlchemy async、PostgreSQL、pytest、httpx、VitePress。

**Spec:** `docs/develop-guides/decisions/implemented/2026-11-01-tenant-agent-management-api.md`

## Global Constraints

- 不新增前端页面；不改变员工侧数字员工、老板分身交互。
- 租户 API 只接受 `X-Tenant-Admin-Key` + `X-Tenant-Id`，SaaS 未启用时 404。
- 所有租户资源查询按 `tenant_id` 精确过滤，跨租户统一 404。
- `agents.tenant_id` 可空；已有行不回填；内置 Agent 保持 `NULL`。
- 决策记录实现后从 `proposed/` 移到 `implemented/` 并改写为当前事实。
- 文档使用中文，包含鉴权、错误码、接口表、请求示例和代码位置。

## Review Focus

- 跨租户按 slug 直接访问必须 404，不能因 `share_config` 是 global 而泄漏。
- 租户 API 更新不能修改 `tenant_id` 或 `kind`。
- 内置 Agent 的 `tenant_id=NULL` 不能被误判为跨租户。
- 老板分身删除必须拒绝仍有经验、咨询、转交或会议引用的记录。
- 普通本地管理员不能通过现有列表接口看到租户平台创建的 Agent。

---

### Task 1: Agent 租户归属与权限隔离

**Files:**
- Modify: `backend/package/yuxi/storage/postgres/models_business.py`
- Modify: `backend/package/yuxi/storage/postgres/manager.py`
- Modify: `backend/package/yuxi/repositories/agent_repository.py`
- Modify: `backend/package/yuxi/permissions/resource_permission.py`
- Modify: `backend/server/utils/auth_middleware.py`
- Modify: `backend/package/yuxi/knowledge/manager.py`
- Test: `backend/test/unit/permissions/test_resource_permission.py`
- Test: `backend/test/integration/api/test_digital_employee_migration.py`

**Interfaces:**
- Produces: `Agent.tenant_id: int | None`
- Produces: `AgentRepository.create(..., tenant_id: int | None = None)`
- Produces: `AgentRepository.get_by_slug_for_tenant(slug: str, tenant_id: int) -> Agent | None`
- Produces: `AgentRepository.list_by_kind(kind: str, tenant_id: int | None = None) -> list[Agent]`
- Produces: `resolve_agent_permission` 在资源租户与用户租户不一致时返回 `ResourcePermission.NONE`

- [x] **Step 1: Write failing permission tests**

Add tests to `backend/test/unit/permissions/test_resource_permission.py`:

```python
def test_agent_tenant_scope_hides_cross_tenant_resource():
    resource = SimpleNamespace(
        created_by="owner",
        tenant_id=100,
        share_config={
            "version": 2,
            "read_scope": {"access_level": "global"},
            "manage_scope": {"access_level": "global"},
        },
    )
    user = _user(uid="employee", role="user")
    user.tenant_id = 100
    other = _user(uid="other", role="admin")
    other.tenant_id = 200

    assert resolve_agent_permission(user, resource) == ResourcePermission.READ
    assert resolve_agent_permission(other, resource) == ResourcePermission.NONE
    assert resolve_agent_permission(_user(role="superadmin"), resource) == ResourcePermission.MANAGE


def test_agent_null_tenant_stays_visible_to_saas_employee():
    resource = SimpleNamespace(
        created_by="system",
        tenant_id=None,
        share_config={"version": 2, "read_scope": {"access_level": "global"}},
    )
    user = _user(uid="employee", role="user")
    user.tenant_id = 100

    assert resolve_agent_permission(user, resource) == ResourcePermission.READ
```

- [x] **Step 2: Run tests to verify failure**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/unit/permissions/test_resource_permission.py -p no:cacheprovider -q`
Expected: cross-tenant assertion fails because tenant scope is not applied.

- [x] **Step 3: Implement tenant scope**

- Add `tenant_id = Column(Integer, nullable=True, index=True)` to `Agent`, include it in `to_dict`.
- Bump `BUSINESS_SCHEMA_VERSION` to 14, allow version 13, add `ALTER TABLE agents ADD COLUMN IF NOT EXISTS tenant_id INTEGER` and `CREATE INDEX IF NOT EXISTS ix_agents_tenant_id ON agents(tenant_id)`.
- In `resolve_resource_permission`, after the `superadmin` bypass, return `NONE` when resource `tenant_id` is not `None` and user tenant is missing or different.
- In auth middleware, set `user.tenant_id = saas_context.tenant_id` beside `tenant_department_id`.
- In `knowledge/manager.py#get_databases_by_user`, include `tenant_id` when given a `User` object.
- Add repository tenant filters and `get_by_slug_for_tenant`; pass `tenant_id` through `AgentRepository.create`.

- [x] **Step 4: Update migration test**

In `backend/test/integration/api/test_digital_employee_migration.py`, assert `"tenant_id"` is in agent columns after running `ensure_business_schema` twice. Also update hardcoded `BUSINESS_SCHEMA_VERSION == 13/12` assertions in existing migration tests to the new current version.

- [x] **Step 5: Run targeted tests**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/unit/permissions/test_resource_permission.py test/integration/api/test_digital_employee_migration.py -p no:cacheprovider -q`
Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add backend/package/yuxi/storage/postgres/models_business.py backend/package/yuxi/storage/postgres/manager.py backend/package/yuxi/repositories/agent_repository.py backend/package/yuxi/permissions/resource_permission.py backend/server/utils/auth_middleware.py backend/package/yuxi/knowledge/manager.py backend/test/unit/permissions/test_resource_permission.py backend/test/integration/api/test_digital_employee_migration.py
git commit -m "feat(tenant): 增加 Agent 租户归属与隔离"
```

### Task 2: 共享租户鉴权依赖

**Files:**
- Create: `backend/server/utils/tenant_admin.py`
- Modify: `backend/server/routers/tenant_knowledge_router.py`
- Test: `backend/test/unit/routers/test_tenant_knowledge_router_auth.py`

**Interfaces:**
- Produces: `TenantAdminContext(tenant_id: int)`
- Produces: `require_tenant_admin(x_tenant_admin_key, x_tenant_id) -> TenantAdminContext`
- 现有 `tenant_knowledge_router` 继续导出同名对象，避免调用方和测试迁移。

- [x] **Step 1: Extract dependency without behavior change**

Move `TenantAdminContext` and `require_tenant_admin` into `backend/server/utils/tenant_admin.py`. In `tenant_knowledge_router.py`, import them from the new module. Keep the existing docstring and 404/401/422 behavior identical.

- [x] **Step 2: Run auth tests**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/unit/routers/test_tenant_knowledge_router_auth.py -p no:cacheprovider -q`
Expected: PASS.

- [x] **Step 3: Commit**

```bash
git add backend/server/utils/tenant_admin.py backend/server/routers/tenant_knowledge_router.py
git commit -m "refactor(tenant): 提取租户管理鉴权依赖"
```

### Task 3: 租户数字员工管理路由

**Files:**
- Create: `backend/server/routers/tenant_agent_router.py`
- Modify: `backend/server/routers/__init__.py`
- Modify: `backend/package/yuxi/services/digital_employee_service.py`
- Test: `backend/test/integration/api/test_tenant_agent_router.py`

**Interfaces:**
- Consumes: `require_tenant_admin`, `Agent.tenant_id`, `AgentRepository.list_by_kind(..., tenant_id=...)`
- Produces: `/api/tenant/digital-employees` CRUD + publish/disable
- Produces: `list_digital_employees(..., tenant_id: int | None = None)`
- Produces: `create_digital_employee(..., tenant_id: int | None = None)`

- [x] **Step 1: Write failing integration tests**

Create `backend/test/integration/api/test_tenant_agent_router.py` with a `tenant_api` fixture using `TENANT_ADMIN_API_KEY` and `X-Tenant-Id: 100`. Add tests:

```python
async def test_tenant_digital_employee_lifecycle_and_cross_tenant_isolation(test_client, tenant_api):
    created = await test_client.post(
        "/api/tenant/digital-employees",
        headers=tenant_api,
        json={"name": "租户数字员工", "profile": valid_profile()},
    )
    assert created.status_code == 201, created.text
    employee = created.json()["employee"]
    assert employee["tenant_id"] == 100
    slug = employee["slug"]

    listed = await test_client.get("/api/tenant/digital-employees", headers=tenant_api)
    assert slug in {item["slug"] for item in listed.json()["employees"]}

    other = {**tenant_api, "X-Tenant-Id": "999"}
    assert (await test_client.get(f"/api/tenant/digital-employees/{slug}", headers=other)).status_code == 404
    assert (await test_client.put(f"/api/tenant/digital-employees/{slug}", headers=other, json={"name": "越权"})).status_code == 404

    published = await test_client.post(f"/api/tenant/digital-employees/{slug}/publish", headers=tenant_api)
    assert published.status_code == 200, published.text
    disabled = await test_client.post(f"/api/tenant/digital-employees/{slug}/disable", headers=tenant_api)
    assert disabled.status_code == 200, disabled.text
```

Add a second test that creates a draft, deletes it, and asserts a local admin's `/api/digital-employees` list does not contain it.

- [x] **Step 2: Run tests to verify failure**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/integration/api/test_tenant_agent_router.py -p no:cacheprovider -q`
Expected: 404 because the route does not exist.

- [x] **Step 3: Implement router and service tenant parameter**

- Add an internal tenant actor helper in the new router: a transient `User` with `uid=f"tenant:{tenant_id}"`, `role="admin"`, `tenant_id=tenant_id`, `department_id=None`.
- Use `DigitalEmployeeCreate` / `DigitalEmployeeUpdate` from the existing router.
- Implement list/get/create/update/publish/disable/delete with exact tenant checks.
- In `digital_employee_service`, add `tenant_id` to `list_digital_employees` and `create_digital_employee`; pass to `AgentRepository.list_by_kind` / `create`.
- Register the router in `server/routers/__init__.py`.

- [x] **Step 4: Run tests**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/integration/api/test_tenant_agent_router.py test/integration/api/test_digital_employee_api.py -p no:cacheprovider -q`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add backend/server/routers/tenant_agent_router.py backend/server/routers/__init__.py backend/package/yuxi/services/digital_employee_service.py backend/test/integration/api/test_tenant_agent_router.py
git commit -m "feat(tenant): 提供数字员工管理接口"
```

### Task 4: 租户老板分身管理路由

**Files:**
- Modify: `backend/server/routers/tenant_agent_router.py`
- Modify: `backend/package/yuxi/services/founder_twin_service.py`
- Modify: `backend/package/yuxi/repositories/founder_twin_repository.py`
- Modify: `backend/package/yuxi/repositories/agent_repository.py`
- Modify: `backend/server/routers/founder_twin_router.py`
- Test: `backend/test/integration/api/test_tenant_agent_router.py`

**Interfaces:**
- Consumes: `create_founder_twin`, `update_founder_twin`, `delete_founder_twin` service functions
- Produces: `/api/tenant/founder-twins` list/get/create/update/delete
- Produces: `FounderTwinRepository.count_references(twin_slug) -> dict[str, int]`

- [x] **Step 1: Write failing founder twin tests**

Append to `test_tenant_agent_router.py`:

```python
async def test_tenant_founder_twin_lifecycle_and_cross_tenant_isolation(test_client, tenant_api):
    created = await test_client.post(
        "/api/tenant/founder-twins",
        headers=tenant_api,
        json={"name": "租户老板分身", "system_prompt": "按已确认经验回答。"},
    )
    assert created.status_code == 201, created.text
    twin = created.json()["twin"]
    assert twin["tenant_id"] == 100
    slug = twin["slug"]

    listed = await test_client.get("/api/tenant/founder-twins", headers=tenant_api)
    assert slug in {item["slug"] for item in listed.json()["twins"]}

    other = {**tenant_api, "X-Tenant-Id": "999"}
    assert (await test_client.get(f"/api/tenant/founder-twins/{slug}", headers=other)).status_code == 404
    assert (await test_client.put(f"/api/tenant/founder-twins/{slug}", headers=other, json={"name": "越权"})).status_code == 404

    updated = await test_client.put(
        f"/api/tenant/founder-twins/{slug}", headers=tenant_api, json={"name": "更新后的分身"}
    )
    assert updated.status_code == 200, updated.text
    deleted = await test_client.delete(f"/api/tenant/founder-twins/{slug}", headers=tenant_api)
    assert deleted.status_code == 200, deleted.text
```

Add a deletion conflict test: insert an experience entry for the twin via SQL, call delete, assert 409.

- [x] **Step 2: Run tests to verify failure**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/integration/api/test_tenant_agent_router.py -p no:cacheprovider -q`
Expected: 404 for founder twin routes.

- [x] **Step 3: Implement service functions and routes**

- Add `create_founder_twin`, `update_founder_twin`, `delete_founder_twin` in `founder_twin_service.py`; move the existing create/update logic from `founder_twin_router.py` to these functions so regular and tenant routes share behavior.
- Add `tenant_id` optional parameter to create and pass it to `AgentRepository.create`.
- Add `get_by_slug_for_tenant` to `AgentRepository`.
- Add reference counting to `FounderTwinRepository` for entries, consultations, handoffs, and meetings; raise `409` on delete when any count is non-zero.
- Implement the founder twin routes in `tenant_agent_router.py` using exact tenant lookups and the shared service functions.

- [x] **Step 4: Run tests**

Run: `docker compose exec -T api uv run --no-sync --group test pytest test/integration/api/test_tenant_agent_router.py test/integration/api/test_founder_twin_api.py -p no:cacheprovider -q`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add backend/server/routers/tenant_agent_router.py backend/package/yuxi/services/founder_twin_service.py backend/package/yuxi/repositories/founder_twin_repository.py backend/package/yuxi/repositories/agent_repository.py backend/server/routers/founder_twin_router.py backend/test/integration/api/test_tenant_agent_router.py
git commit -m "feat(tenant): 提供老板分身管理接口"
```

### Task 5: 文档、决策记录与全量验证

**Files:**
- Create: `docs/advanced/tenant-agent-management-api.md`
- Modify: `docs/.vitepress/config.mts`
- Modify: `docs/knowledge-permission.md`
- Move: `docs/develop-guides/decisions/proposed/2026-11-01-tenant-agent-management-api.md` to `docs/develop-guides/decisions/implemented/`
- Modify: `docs/develop-guides/decisions/proposed/2026-11-01-tenant-agent-management-api.md` status to implemented

- [x] **Step 1: Write the integration document**

Document auth, error matrix, tenant isolation, request/response examples, digital employee endpoints, founder twin endpoints, `share_config`, deletion constraints, non-goals, and code locations. Add it to VitePress navigation and link from the tenant knowledge section.

- [x] **Step 2: Move decision record**

Move the proposed record to `implemented/`, change `状态：proposed` to `状态：implemented`, and replace verification placeholders with actual commands/results after tests.

- [x] **Step 3: Run full verification**

```bash
python3 scripts/verify_engineering_contracts.py
python3 -m unittest scripts.test_verify_engineering_contracts
docker compose exec -T api uv run --no-sync --group test pytest test/unit -m "not slow" -p no:cacheprovider -q
docker compose exec -T api uv run --no-sync --group test pytest test/integration/api/test_tenant_agent_router.py test/integration/api/test_digital_employee_api.py test/integration/api/test_founder_twin_api.py test/integration/api/test_digital_employee_migration.py -p no:cacheprovider -q
docs/node_modules/.bin/vitepress build
git diff --check
```

- [x] **Step 4: Commit**

```bash
git add docs/advanced/tenant-agent-management-api.md docs/.vitepress/config.mts docs/knowledge-permission.md docs/develop-guides/decisions/implemented/2026-11-01-tenant-agent-management-api.md
git commit -m "docs(tenant): 补充 Agent 管理 API 对接文档"
```
