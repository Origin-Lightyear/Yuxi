"""租户平台数字员工与老板分身管理 API 的真实 HTTP 验收。"""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.agent_repository import AgentRepository
from yuxi.repositories.user_repository import UserRepository
from yuxi.storage.postgres.models_business import User, UserConfig

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """本模块直接创建本地测试身份，不使用 SaaS 登录清理钩子。"""
    yield


def _valid_profile() -> dict:
    return {
        "position": "经营分析官",
        "department": "经营管理",
        "responsibilities": "输出经营分析",
        "capabilities": ["数据查询", "报告生成"],
        "boundaries": "不代替管理层决策",
        "acceptance": "结论包含数据口径",
    }


@pytest.fixture
def tenant_api() -> dict[str, str]:
    key = os.environ.get("TENANT_ADMIN_API_KEY", "")
    if not key:
        pytest.skip("TENANT_ADMIN_API_KEY 未配置，跳过租户 Agent 管理 API 集成测试")
    return {"X-Tenant-Admin-Key": key, "X-Tenant-Id": "100"}


async def _db(sql: str, params: dict | None = None):
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params or {})
            return result.first() if result.returns_rows else None
    finally:
        await engine.dispose()


async def test_tenant_agent_router_requires_admin_key(test_client, tenant_api):
    response = await test_client.get(
        "/api/tenant/digital-employees",
        headers={"X-Tenant-Admin-Key": "wrong", "X-Tenant-Id": "100"},
    )
    assert response.status_code == 401

    response = await test_client.get(
        "/api/tenant/digital-employees",
        headers={"X-Tenant-Admin-Key": tenant_api["X-Tenant-Admin-Key"]},
    )
    assert response.status_code == 422


async def test_database_loaded_saas_user_can_resolve_same_tenant_agent(test_client, tenant_api):
    """Worker/定时任务从数据库加载的 User 也能解析同租户 Agent。"""
    slug = f"pytest-tenant-de-worker-{uuid.uuid4().hex[:8]}"
    uid = f"pytest-tenant-user-{uuid.uuid4().hex[:8]}"
    tenant_id, other_tenant_id = 100, 200
    try:
        created = await test_client.post(
            "/api/tenant/digital-employees",
            headers=tenant_api,
            json={"name": "租户执行员工", "slug": slug, "profile": _valid_profile()},
        )
        assert created.status_code == 201, created.text

        engine = create_async_engine(os.environ["POSTGRES_URL"])
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                db.add(User(username=uid, uid=uid, password_hash="x", role="user", department_id=None))
                await db.flush()
                db.add(UserConfig(uid=uid, tenant_id=tenant_id, employee_id=1))
                await db.commit()

            user = await UserRepository().get_by_uid(uid)
            assert user is not None
            assert user.tenant_id == tenant_id

            async with factory() as db:
                agent = await AgentRepository(db).get_visible_by_slug(slug=slug, user=user, kind="any")
                assert agent is not None
                assert agent.tenant_id == tenant_id

            await _db(
                "UPDATE user_config SET tenant_id = :tenant_id WHERE uid = :uid",
                {"tenant_id": other_tenant_id, "uid": uid},
            )
            other_user = await UserRepository().get_by_uid(uid)
            assert other_user is not None
            assert other_user.tenant_id == other_tenant_id
            async with factory() as db:
                denied = await AgentRepository(db).get_visible_by_slug(slug=slug, user=other_user, kind="any")
                assert denied is None
        finally:
            await engine.dispose()
    finally:
        await _db("DELETE FROM user_config WHERE uid = :uid", {"uid": uid})
        await _db("DELETE FROM users WHERE uid = :uid", {"uid": uid})
        await _db("DELETE FROM agents WHERE slug = :slug", {"slug": slug})


async def test_tenant_digital_employee_lifecycle_and_isolation(test_client, tenant_api):
    slug = f"pytest-tenant-de-{uuid.uuid4().hex[:10]}"
    try:
        created = await test_client.post(
            "/api/tenant/digital-employees",
            headers=tenant_api,
            json={
                "name": "租户数字员工",
                "slug": slug,
                "tenant_id": 999,
                "profile": _valid_profile(),
                "system_prompt": "你是租户数字员工，先确认数据口径。",
                "config_json": {"context": {"model": "pytest:model"}},
            },
        )
        assert created.status_code == 201, created.text
        employee = created.json()["employee"]
        assert employee["tenant_id"] == 100
        assert employee["slug"] == slug
        assert employee["digital_employee_profile"]["status"] == "draft"
        assert employee["config_json"]["context"]["system_prompt"] == "你是租户数字员工，先确认数据口径。"
        assert employee["config_json"]["context"]["model"] == "pytest:model"

        listed = await test_client.get("/api/tenant/digital-employees", headers=tenant_api)
        assert listed.status_code == 200, listed.text
        assert slug in {item["slug"] for item in listed.json()["employees"]}
        assert listed.json()["summary"]["draft"] >= 1

        detail = await test_client.get(f"/api/tenant/digital-employees/{slug}", headers=tenant_api)
        assert detail.status_code == 200, detail.text
        assert detail.json()["employee"]["tenant_id"] == 100

        other_headers = {**tenant_api, "X-Tenant-Id": "999"}
        assert (
            await test_client.get(f"/api/tenant/digital-employees/{slug}", headers=other_headers)
        ).status_code == 404
        assert (
            await test_client.put(
                f"/api/tenant/digital-employees/{slug}",
                headers=other_headers,
                json={"name": "越权修改"},
            )
        ).status_code == 404
        assert (
            await test_client.post(f"/api/tenant/digital-employees/{slug}/publish", headers=other_headers)
        ).status_code == 404

        updated = await test_client.put(
            f"/api/tenant/digital-employees/{slug}",
            headers=tenant_api,
            json={
                "name": "更新后的数字员工",
                "tenant_id": 999,
                "system_prompt": "更新后的行为 Prompt。",
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["employee"]["name"] == "更新后的数字员工"
        assert updated.json()["employee"]["tenant_id"] == 100
        assert updated.json()["employee"]["config_json"]["context"]["system_prompt"] == "更新后的行为 Prompt。"
        assert updated.json()["employee"]["config_json"]["context"]["model"] == "pytest:model"

        published = await test_client.post(f"/api/tenant/digital-employees/{slug}/publish", headers=tenant_api)
        assert published.status_code == 200, published.text
        assert published.json()["employee"]["digital_employee_profile"]["status"] == "active"

        disabled = await test_client.post(f"/api/tenant/digital-employees/{slug}/disable", headers=tenant_api)
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["employee"]["digital_employee_profile"]["status"] == "disabled"
    finally:
        await _db("DELETE FROM agents WHERE slug = :slug", {"slug": slug})


async def test_tenant_digital_employee_delete_only_draft(test_client, tenant_api):
    draft_slug = f"pytest-tenant-de-draft-{uuid.uuid4().hex[:8]}"
    active_slug = f"pytest-tenant-de-active-{uuid.uuid4().hex[:8]}"
    try:
        draft = await test_client.post(
            "/api/tenant/digital-employees",
            headers=tenant_api,
            json={"name": "待删除数字员工", "slug": draft_slug, "profile": _valid_profile()},
        )
        assert draft.status_code == 201, draft.text
        deleted = await test_client.delete(f"/api/tenant/digital-employees/{draft_slug}", headers=tenant_api)
        assert deleted.status_code == 200, deleted.text
        assert (
            await test_client.get(f"/api/tenant/digital-employees/{draft_slug}", headers=tenant_api)
        ).status_code == 404

        active = await test_client.post(
            "/api/tenant/digital-employees",
            headers=tenant_api,
            json={"name": "已发布数字员工", "slug": active_slug, "profile": _valid_profile()},
        )
        assert active.status_code == 201, active.text
        published = await test_client.post(f"/api/tenant/digital-employees/{active_slug}/publish", headers=tenant_api)
        assert published.status_code == 200, published.text
        rejected = await test_client.delete(f"/api/tenant/digital-employees/{active_slug}", headers=tenant_api)
        assert rejected.status_code == 409, rejected.text
    finally:
        await _db("DELETE FROM agents WHERE slug IN (:draft_slug, :active_slug)", {
            "draft_slug": draft_slug,
            "active_slug": active_slug,
        })


async def test_tenant_digital_employee_delete_guard_includes_meetings(test_client, tenant_api):
    slug = f"pytest-tenant-de-meeting-{uuid.uuid4().hex[:8]}"
    try:
        created = await test_client.post(
            "/api/tenant/digital-employees",
            headers=tenant_api,
            json={"name": "会议引用员工", "slug": slug, "profile": _valid_profile()},
        )
        assert created.status_code == 201, created.text

        await _db(
            """
            INSERT INTO meetings (kind, topic, participant_slugs, created_by)
            VALUES ('digital_employee', 'pytest-delete-guard', CAST(jsonb_build_array(CAST(:slug AS text)) AS json), 'pytest')
            """,
            {"slug": slug},
        )
        rejected = await test_client.delete(f"/api/tenant/digital-employees/{slug}", headers=tenant_api)
        assert rejected.status_code == 409, rejected.text
        assert "会议" in rejected.json()["detail"]

        await _db("DELETE FROM meetings WHERE topic = 'pytest-delete-guard'")
        deleted = await test_client.delete(f"/api/tenant/digital-employees/{slug}", headers=tenant_api)
        assert deleted.status_code == 200, deleted.text
    finally:
        await _db("DELETE FROM meetings WHERE topic = 'pytest-delete-guard'")
        await _db("DELETE FROM agents WHERE slug = :slug", {"slug": slug})


async def test_tenant_create_rejects_global_slug_conflict(test_client, tenant_api):
    slug = f"pytest-tenant-conflict-{uuid.uuid4().hex[:8]}"
    try:
        created = await test_client.post(
            "/api/tenant/digital-employees",
            headers=tenant_api,
            json={"name": "冲突员工", "slug": slug, "profile": _valid_profile()},
        )
        assert created.status_code == 201, created.text

        other_headers = {**tenant_api, "X-Tenant-Id": "999"}
        conflict = await test_client.post(
            "/api/tenant/digital-employees",
            headers=other_headers,
            json={"name": "另一个租户的员工", "slug": slug, "profile": _valid_profile()},
        )
        assert conflict.status_code == 409, conflict.text
        assert "slug" in conflict.json()["detail"]
    finally:
        await _db("DELETE FROM agents WHERE slug = :slug", {"slug": slug})


async def test_tenant_agent_knowledge_names_are_displayed_and_ids_stored(test_client, tenant_api):
    """租户 API 用名称展示知识库，数据库仍保存 kb_id。"""
    kb_name = f"pytest-kb-{uuid.uuid4().hex[:8]}"
    slug = f"pytest-tenant-de-kb-{uuid.uuid4().hex[:8]}"
    kb_id = None
    try:
        kb_response = await test_client.post(
            "/api/tenant/knowledge/databases",
            headers=tenant_api,
            json={"database_name": kb_name, "description": "租户 Agent 知识库映射测试"},
        )
        assert kb_response.status_code == 201, kb_response.text
        kb_id = kb_response.json()["kb_id"]

        created = await test_client.post(
            "/api/tenant/digital-employees",
            headers=tenant_api,
            json={
                "name": "知识库映射员工",
                "slug": slug,
                "profile": _valid_profile(),
                "config_json": {"context": {"knowledges": [kb_name]}},
            },
        )
        assert created.status_code == 201, created.text
        employee = created.json()["employee"]
        assert employee["config_json"]["context"]["knowledges"] == [kb_name]
        assert employee["config_json"]["context"]["knowledge_ids"] == [kb_id]

        stored = await _db(
            "SELECT config_json FROM agents WHERE slug = :slug",
            {"slug": slug},
        )
        assert stored is not None
        stored_config = stored[0]
        if isinstance(stored_config, str):
            stored_config = json.loads(stored_config)
        assert stored_config["context"]["knowledges"] == [kb_id]

        updated = await test_client.put(
            f"/api/tenant/digital-employees/{slug}",
            headers=tenant_api,
            json={"config_json": {"context": {"knowledges": [kb_name]}}},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["employee"]["config_json"]["context"]["knowledges"] == [kb_name]
        assert updated.json()["employee"]["config_json"]["context"]["knowledge_ids"] == [kb_id]
    finally:
        await _db("DELETE FROM agents WHERE slug = :slug", {"slug": slug})
        if kb_id:
            await test_client.delete(f"/api/tenant/knowledge/databases/{kb_id}", headers=tenant_api)


async def test_tenant_founder_twin_lifecycle_and_delete_guard(test_client, tenant_api):
    slug = f"pytest-tenant-twin-{uuid.uuid4().hex[:10]}"
    try:
        created = await test_client.post(
            "/api/tenant/founder-twins",
            headers=tenant_api,
            json={"name": "租户老板分身", "slug": slug, "tenant_id": 999, "system_prompt": "按已确认经验回答。"},
        )
        assert created.status_code == 201, created.text
        twin = created.json()["twin"]
        assert twin["tenant_id"] == 100
        assert twin["slug"] == slug
        assert twin["share_config"]["version"] == 2
        assert twin["system_prompt"] == "按已确认经验回答。"

        listed = await test_client.get("/api/tenant/founder-twins", headers=tenant_api)
        assert listed.status_code == 200, listed.text
        assert slug in {item["slug"] for item in listed.json()["twins"]}

        detail = await test_client.get(f"/api/tenant/founder-twins/{slug}", headers=tenant_api)
        assert detail.status_code == 200, detail.text
        assert detail.json()["twin"]["tenant_id"] == 100

        other_headers = {**tenant_api, "X-Tenant-Id": "999"}
        assert (
            await test_client.get(f"/api/tenant/founder-twins/{slug}", headers=other_headers)
        ).status_code == 404
        assert (
            await test_client.put(
                f"/api/tenant/founder-twins/{slug}",
                headers=other_headers,
                json={"name": "越权修改"},
            )
        ).status_code == 404
        assert (
            await test_client.delete(f"/api/tenant/founder-twins/{slug}", headers=other_headers)
        ).status_code == 404

        updated = await test_client.put(
            f"/api/tenant/founder-twins/{slug}",
            headers=tenant_api,
            json={"name": "更新后的老板分身", "tenant_id": 999},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["twin"]["name"] == "更新后的老板分身"
        assert updated.json()["twin"]["tenant_id"] == 100

        await _db(
            """
            INSERT INTO experience_entries (twin_slug, code, title, situation, judgment, status)
            VALUES (:slug, 'pytest-ref', '删除守卫经验', '', '不应被删除', 'draft')
            """,
            {"slug": slug},
        )
        rejected = await test_client.delete(f"/api/tenant/founder-twins/{slug}", headers=tenant_api)
        assert rejected.status_code == 409, rejected.text

        await _db("DELETE FROM experience_entries WHERE twin_slug = :slug", {"slug": slug})
        deleted = await test_client.delete(f"/api/tenant/founder-twins/{slug}", headers=tenant_api)
        assert deleted.status_code == 200, deleted.text
        assert (await test_client.get(f"/api/tenant/founder-twins/{slug}", headers=tenant_api)).status_code == 404
    finally:
        await _db("DELETE FROM experience_entries WHERE twin_slug = :slug", {"slug": slug})
        await _db("DELETE FROM experience_consultations WHERE twin_slug = :slug", {"slug": slug})
        await _db("DELETE FROM handoff_requests WHERE twin_slug = :slug", {"slug": slug})
        await _db("DELETE FROM agents WHERE slug = :slug", {"slug": slug})
