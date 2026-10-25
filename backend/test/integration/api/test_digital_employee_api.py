from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from test.live_api_cleanup import cleanup_test_chat_resources, make_test_conversation_title
from yuxi.utils.auth_utils import AuthUtils

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

ADMIN_UID = "pytest-digital-admin"


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


def _user_share(uid: str) -> dict:
    return {
        "version": 2,
        "read_scope": {"access_level": "user", "department_ids": [], "user_uids": [uid]},
        "manage_scope": None,
    }


async def _execute(sql: str, params: dict | None = None) -> None:
    """在测试所需的最小事务中执行 PostgreSQL 清理或引用造数。"""
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params or {})
    finally:
        await engine.dispose()


@pytest_asyncio.fixture()
async def local_admin_headers() -> dict[str, str]:
    """为已存在的本地测试管理员签发 JWT，避开开发环境的 SaaS 登录。"""
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT id FROM users WHERE uid = :uid AND is_deleted = 0"),
                    {"uid": ADMIN_UID},
                )
            ).first()
    finally:
        await engine.dispose()
    if row is None:
        pytest.skip(f"local integration admin {ADMIN_UID} is not provisioned")
    token = AuthUtils.create_access_token({"sub": str(row[0])})
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture()
async def standard_user(test_client, local_admin_headers):
    """创建真实普通用户并直接签发 JWT，保留真实 HTTP 权限路径。"""
    username = f"pytest_de_{uuid.uuid4().hex[:8]}"
    password = f"Pw!{uuid.uuid4().hex[:12]}"
    departments = await test_client.get("/api/departments", headers=local_admin_headers)
    assert departments.status_code == 200, departments.text
    department_id = departments.json()[0]["id"]
    response = await test_client.post(
        "/api/auth/users",
        headers=local_admin_headers,
        json={"username": username, "password": password, "role": "user", "department_id": department_id},
    )
    assert response.status_code == 200, response.text
    user_payload = response.json()
    token = AuthUtils.create_access_token({"sub": str(user_payload["id"])})
    try:
        yield {
            "user": user_payload,
            "password": password,
            "headers": {"Authorization": f"Bearer {token}"},
        }
    finally:
        await test_client.delete(f"/api/auth/users/{user_payload['id']}", headers=local_admin_headers)


@pytest_asyncio.fixture()
async def employee_factory(test_client, local_admin_headers):
    """创建唯一 slug 的数字员工，并在测试结束后移除持久引用和员工行。"""
    created_slugs: list[str] = []

    async def create(*, name: str = "测试数字员工", share_config: dict | None = None):
        slug = f"pytest-de-{uuid.uuid4().hex[:10]}"
        response = await test_client.post(
            "/api/digital-employees",
            headers=local_admin_headers,
            json={"name": name, "slug": slug, "profile": _valid_profile(), "share_config": share_config},
        )
        assert response.status_code == 200, response.text
        created_slugs.append(slug)
        return response.json()["employee"]

    yield create

    for slug in created_slugs:
        await _execute("DELETE FROM im_bot_configs WHERE agent_slug = :slug", {"slug": slug})
        await _execute("DELETE FROM scheduled_agent_jobs WHERE agent_slug = :slug", {"slug": slug})
        await _execute("DELETE FROM agents WHERE slug = :slug", {"slug": slug})


async def test_digital_employee_draft_hidden_from_standard_user_and_readable_by_manager(
    test_client, local_admin_headers, standard_user, employee_factory
):
    """草稿员工对普通用户完全不可见，管理者仍能读取详情。"""
    employee = await employee_factory(share_config=_user_share(standard_user["user"]["uid"]))
    slug = employee["slug"]

    visible = await test_client.get("/api/digital-employees", headers=standard_user["headers"])
    assert visible.status_code == 200, visible.text
    assert slug not in {item["slug"] for item in visible.json()["employees"]}
    assert visible.json()["summary"]["draft"] == 0

    assert (await test_client.get(f"/api/digital-employees/{slug}", headers=standard_user["headers"])).status_code == 404
    assert (await test_client.get(f"/api/agent/{slug}", headers=standard_user["headers"])).status_code == 404
    thread = await test_client.post(
        "/api/chat/thread",
        headers=standard_user["headers"],
        json={"agent_id": slug, "title": make_test_conversation_title("草稿员工线程")},
    )
    assert thread.status_code == 404, thread.text

    manager_detail = await test_client.get(f"/api/digital-employees/{slug}", headers=local_admin_headers)
    assert manager_detail.status_code == 200, manager_detail.text
    assert manager_detail.json()["employee"]["digital_employee_profile"]["status"] == "draft"
    generic_detail = await test_client.get(f"/api/agent/{slug}", headers=local_admin_headers)
    assert generic_detail.status_code == 200, generic_detail.text
    assert generic_detail.json()["agent"]["kind"] == "digital_employee"


async def test_digital_employee_publish_then_visible_and_generic_agent_isolation(
    test_client, local_admin_headers, standard_user, employee_factory
):
    """发布后按共享范围可见，且通用 Agent 路由不能修改、删除或复制员工。"""
    employee = await employee_factory(share_config=_user_share(standard_user["user"]["uid"]))
    slug = employee["slug"]

    published = await test_client.post(f"/api/digital-employees/{slug}/publish", headers=local_admin_headers)
    assert published.status_code == 200, published.text
    assert published.json()["employee"]["digital_employee_profile"]["status"] == "active"

    visible = await test_client.get("/api/digital-employees", headers=standard_user["headers"])
    assert visible.status_code == 200, visible.text
    assert slug in {item["slug"] for item in visible.json()["employees"]}

    detail = await test_client.get(f"/api/digital-employees/{slug}", headers=standard_user["headers"])
    assert detail.status_code == 200, detail.text
    payload = detail.json()["employee"]
    assert payload["kind"] == "digital_employee"
    assert payload["can_manage"] is False
    assert "config_json" not in payload
    assert "configurable_items" not in payload

    generic_list = await test_client.get("/api/agent", headers=standard_user["headers"])
    assert generic_list.status_code == 200, generic_list.text
    generic_employee = next(item for item in generic_list.json()["agents"] if item["slug"] == slug)
    assert "config_json" not in generic_employee
    generic_detail = await test_client.get(f"/api/agent/{slug}", headers=standard_user["headers"])
    assert generic_detail.status_code == 200, generic_detail.text
    assert "config_json" not in generic_detail.json()["agent"]
    assert "configurable_items" not in generic_detail.json()["agent"]

    agent_only = await test_client.get("/api/agent?kind=agent", headers=local_admin_headers)
    assert agent_only.status_code == 200, agent_only.text
    assert slug not in {item["slug"] for item in agent_only.json()["agents"]}
    employee_only = await test_client.get("/api/agent?kind=digital_employee", headers=local_admin_headers)
    assert employee_only.status_code == 200, employee_only.text
    assert slug in {item["slug"] for item in employee_only.json()["agents"]}
    invalid_kind = await test_client.get("/api/agent?kind=bogus", headers=local_admin_headers)
    assert invalid_kind.status_code == 422, invalid_kind.text

    for response in (
        await test_client.put(f"/api/agent/{slug}", headers=local_admin_headers, json={"name": "越权改名"}),
        await test_client.delete(f"/api/agent/{slug}", headers=local_admin_headers),
        await test_client.post(f"/api/agent/{slug}/duplicate", headers=local_admin_headers),
    ):
        assert response.status_code == 400, response.text


async def test_digital_employee_disable_blocks_new_run_and_keeps_history(
    test_client, local_admin_headers, employee_factory
):
    """停用员工后旧线程仍可读，但新线程和新 Run 在入队前被拒绝。"""
    employee = await employee_factory()
    slug = employee["slug"]
    published = await test_client.post(f"/api/digital-employees/{slug}/publish", headers=local_admin_headers)
    assert published.status_code == 200, published.text

    thread_id = None
    try:
        thread = await test_client.post(
            "/api/chat/thread",
            headers=local_admin_headers,
            json={"agent_id": slug, "title": make_test_conversation_title("数字员工停用守卫")},
        )
        assert thread.status_code == 200, thread.text
        thread_id = thread.json()["id"]

        disabled = await test_client.post(f"/api/digital-employees/{slug}/disable", headers=local_admin_headers)
        assert disabled.status_code == 200, disabled.text

        run = await test_client.post(
            "/api/agent/runs",
            headers=local_admin_headers,
            json={"agent_slug": slug, "thread_id": thread_id, "query": "还能运行吗"},
        )
        assert run.status_code == 409, run.text
        new_thread = await test_client.post(
            "/api/chat/thread",
            headers=local_admin_headers,
            json={"agent_id": slug, "title": make_test_conversation_title("停用后新线程")},
        )
        assert new_thread.status_code == 409, new_thread.text

        detail = await test_client.get(f"/api/agent/{slug}", headers=local_admin_headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["agent"]["digital_employee_profile"]["status"] == "disabled"

        history = await test_client.get(f"/api/chat/thread/{thread_id}/history", headers=local_admin_headers)
        assert history.status_code == 200, history.text
    finally:
        if thread_id:
            await test_client.delete(f"/api/chat/thread/{thread_id}", headers=local_admin_headers)
        me = await test_client.get("/api/auth/me", headers=local_admin_headers)
        assert me.status_code == 200, me.text
        await cleanup_test_chat_resources(
            test_client,
            local_admin_headers,
            owner_uid=str(me.json()["uid"]),
        )


async def test_digital_employee_active_update_rejects_empty_profile(
    test_client, local_admin_headers, employee_factory
):
    """active 员工被改成空岗位或空能力时失败，原档案保持不变。"""
    employee = await employee_factory()
    slug = employee["slug"]
    published = await test_client.post(f"/api/digital-employees/{slug}/publish", headers=local_admin_headers)
    assert published.status_code == 200, published.text

    for patch in ({"profile": {"position": ""}}, {"profile": {"capabilities": []}}):
        response = await test_client.put(f"/api/digital-employees/{slug}", headers=local_admin_headers, json=patch)
        assert response.status_code == 422, response.text

    detail = await test_client.get(f"/api/digital-employees/{slug}", headers=local_admin_headers)
    assert detail.status_code == 200, detail.text
    profile = detail.json()["employee"]["digital_employee_profile"]
    assert profile["status"] == "active"
    assert profile["position"] == "经营分析官"
    assert profile["capabilities"] == ["数据查询", "报告生成"]


async def test_digital_employee_delete_draft_with_reference_returns_409(
    test_client, local_admin_headers, employee_factory
):
    """草稿被定时任务或 IM Bot 绑定时拒绝删除，并覆盖两条绑定入口。"""
    employee = await employee_factory()
    slug = employee["slug"]
    me = await test_client.get("/api/auth/me", headers=local_admin_headers)
    assert me.status_code == 200, me.text
    admin_uid = str(me.json()["uid"])

    thread_id = None
    project_id = None
    try:
        thread = await test_client.post(
            "/api/chat/thread",
            headers=local_admin_headers,
            json={"agent_id": "default-chatbot", "title": make_test_conversation_title("草稿引用测试")},
        )
        assert thread.status_code == 200, thread.text
        thread_id = thread.json()["id"]
        project_id = thread.json().get("project_id")
        assert project_id

        scheduled = await test_client.post(
            "/api/scheduled-tasks",
            headers=local_admin_headers,
            json={
                "request_id": f"pytest-sched-{uuid.uuid4().hex[:10]}",
                "name": "草稿定时任务",
                "project_id": project_id,
                "agent_slug": slug,
                "prompt": "测试",
                "cron_expression": "0 9 * * *",
                "timezone": "Asia/Shanghai",
            },
        )
        assert scheduled.status_code == 409, scheduled.text

        bot = await test_client.post(
            "/api/system/im-bots",
            headers=local_admin_headers,
            json={
                "platform": "feishu",
                "name": "草稿机器人",
                "enabled": False,
                "owner_uid": admin_uid,
                "agent_slug": slug,
                "credentials": {"app_id": "cli_pytest", "app_secret": "secret"},
            },
        )
        assert bot.status_code == 409, bot.text

        await _execute(
            """
            INSERT INTO im_bot_configs (
                platform, name, enabled, credentials_encrypted, owner_uid, agent_slug,
                allowed_users, require_mention, group_sessions_per_user, created_at, updated_at
            ) VALUES (
                'feishu', 'pytest-reference-bot', false, 'encrypted', :uid, :slug,
                '[]'::json, false, false, now(), now()
            )
            """,
            {"uid": admin_uid, "slug": slug},
        )
        blocked = await test_client.delete(f"/api/digital-employees/{slug}", headers=local_admin_headers)
        assert blocked.status_code == 409, blocked.text

        await _execute(
            "DELETE FROM im_bot_configs WHERE agent_slug = :slug AND name = 'pytest-reference-bot'",
            {"slug": slug},
        )
        deleted = await test_client.delete(f"/api/digital-employees/{slug}", headers=local_admin_headers)
        assert deleted.status_code == 200, deleted.text
        assert (await test_client.get(f"/api/digital-employees/{slug}", headers=local_admin_headers)).status_code == 404
    finally:
        await _execute("DELETE FROM im_bot_configs WHERE agent_slug = :slug", {"slug": slug})
        if thread_id:
            await test_client.delete(f"/api/chat/thread/{thread_id}", headers=local_admin_headers)
        if project_id:
            await test_client.delete(f"/api/projects/{project_id}", headers=local_admin_headers)


async def test_digital_employee_non_manager_detail_hides_private_resources(
    test_client, local_admin_headers, standard_user, employee_factory
):
    """非管理者只看到岗位档案，不看到运行时资源和可配置项。"""
    employee = await employee_factory(share_config=_user_share(standard_user["user"]["uid"]))
    slug = employee["slug"]
    published = await test_client.post(f"/api/digital-employees/{slug}/publish", headers=local_admin_headers)
    assert published.status_code == 200, published.text

    manager_detail = await test_client.get(f"/api/digital-employees/{slug}", headers=local_admin_headers)
    assert manager_detail.status_code == 200, manager_detail.text
    assert "config_json" in manager_detail.json()["employee"]
    assert "configurable_items" in manager_detail.json()["employee"]

    user_detail = await test_client.get(f"/api/digital-employees/{slug}", headers=standard_user["headers"])
    assert user_detail.status_code == 200, user_detail.text
    user_payload = user_detail.json()["employee"]
    assert user_payload["can_manage"] is False
    assert "config_json" not in user_payload
    assert "configurable_items" not in user_payload
    assert user_payload["digital_employee_profile"]["position"] == "经营分析官"
    assert user_payload["digital_employee_profile"]["capabilities"] == ["数据查询", "报告生成"]

    generic_detail = await test_client.get(f"/api/agent/{slug}", headers=standard_user["headers"])
    assert generic_detail.status_code == 200, generic_detail.text
    assert "config_json" not in generic_detail.json()["agent"]
    assert "configurable_items" not in generic_detail.json()["agent"]


async def test_standard_user_cannot_create_digital_employee(test_client, standard_user):
    """普通用户不能创建数字员工。"""
    response = await test_client.post(
        "/api/digital-employees",
        headers=standard_user["headers"],
        json={"name": "越权员工", "profile": {"position": "岗位"}},
    )
    assert response.status_code == 403, response.text
