from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

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
