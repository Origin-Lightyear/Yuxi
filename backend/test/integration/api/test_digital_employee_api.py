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


async def test_standard_user_cannot_create_digital_employee(test_client, standard_user):
    """普通用户不能创建数字员工。"""
    response = await test_client.post(
        "/api/digital-employees",
        headers=standard_user["headers"],
        json={"name": "越权员工", "profile": {"position": "岗位"}},
    )
    assert response.status_code == 403, response.text


def _valid_profile() -> dict:
    return {
        "position": "经营分析官",
        "department": "经营管理",
        "responsibilities": "输出经营分析",
        "capabilities": ["数据查询", "报告生成"],
        "boundaries": "不代替管理层决策",
        "acceptance": "结论包含数据口径",
    }


async def test_disabled_employee_rejects_new_run_but_keeps_history(test_client, local_admin_headers):
    """停用员工后旧线程仍可读，但新 Run 在入队前被拒绝。"""
    slug = f"pytest-de-{uuid.uuid4().hex[:8]}"
    created = await test_client.post(
        "/api/digital-employees",
        headers=local_admin_headers,
        json={"name": "停用守卫员工", "slug": slug, "profile": _valid_profile()},
    )
    assert created.status_code == 200, created.text
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
        engine = create_async_engine(os.environ["POSTGRES_URL"])
        try:
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM agents WHERE slug = :slug"), {"slug": slug})
        finally:
            await engine.dispose()
