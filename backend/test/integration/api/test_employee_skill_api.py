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


async def _db(sql: str, params: dict | None = None):
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params or {})
            return result.first() if result.returns_rows else None
    finally:
        await engine.dispose()


@pytest_asyncio.fixture()
async def local_admin_headers() -> dict[str, str]:
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


async def _create_user(test_client, local_admin_headers) -> dict:
    username = f"pytest_skl_{uuid.uuid4().hex[:8]}"
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
    payload = response.json()
    token = AuthUtils.create_access_token({"sub": str(payload["id"])})
    return {"payload": payload, "uid": payload["uid"], "headers": {"Authorization": f"Bearer {token}"}}


@pytest_asyncio.fixture()
async def skill_users(test_client, local_admin_headers):
    owner = await _create_user(test_client, local_admin_headers)
    colleague = await _create_user(test_client, local_admin_headers)
    successor = await _create_user(test_client, local_admin_headers)
    try:
        yield owner, colleague, successor
    finally:
        for user in (owner, colleague, successor):
            await test_client.delete(f"/api/auth/users/{user['payload']['id']}", headers=local_admin_headers)
        await _db("DELETE FROM employee_skills WHERE owner_uid LIKE 'pytest_skl_%'")


async def test_employee_skill_visibility_handover_and_permissions(
    test_client,
    skill_users,
):
    owner, colleague, successor = skill_users

    created = await test_client.post(
        "/api/employee-skills",
        headers=owner["headers"],
        json={
            "title": "大客户报价复盘",
            "department": "销售",
            "capture_method": "review",
            "steps": ["确认预算", "核对成本", "给出折扣区间"],
            "rules": ["超过十万元必须二次确认"],
            "boundary": "不承诺未审批折扣",
            "acceptance": "报价单包含预算、成本和审批人",
        },
    )
    assert created.status_code == 200, created.text
    skill = created.json()["skill"]
    assert skill["status"] == "draft"
    assert skill["can_manage"] is True

    duplicate = await test_client.post(
        "/api/employee-skills",
        headers=owner["headers"],
        json={"title": "重复编码", "code": skill["code"]},
    )
    assert duplicate.status_code == 409

    owner_list = await test_client.get("/api/employee-skills", headers=owner["headers"])
    assert owner_list.status_code == 200
    assert any(item["id"] == skill["id"] for item in owner_list.json()["skills"])

    colleague_list = await test_client.get("/api/employee-skills", headers=colleague["headers"])
    assert colleague_list.status_code == 200
    assert all(item["id"] != skill["id"] for item in colleague_list.json()["skills"])

    hidden = await test_client.get(f"/api/employee-skills/{skill['id']}", headers=colleague["headers"])
    assert hidden.status_code == 404

    forbidden = await test_client.post(
        f"/api/employee-skills/{skill['id']}/verify", headers=colleague["headers"]
    )
    assert forbidden.status_code == 404

    verified = await test_client.post(f"/api/employee-skills/{skill['id']}/verify", headers=owner["headers"])
    assert verified.status_code == 200, verified.text
    assert verified.json()["skill"]["status"] == "ready"

    colleague_list = await test_client.get("/api/employee-skills", headers=colleague["headers"])
    assert any(item["id"] == skill["id"] for item in colleague_list.json()["skills"])

    candidates = await test_client.get(
        f"/api/employee-skills/{skill['id']}/handover-candidates",
        headers=owner["headers"],
    )
    assert candidates.status_code == 200, candidates.text
    candidate_uids = {item["uid"] for item in candidates.json()["users"]}
    assert successor["uid"] in candidate_uids
    assert owner["uid"] not in candidate_uids

    forbidden_candidates = await test_client.get(
        f"/api/employee-skills/{skill['id']}/handover-candidates",
        headers=colleague["headers"],
    )
    assert forbidden_candidates.status_code == 403

    assigned = await test_client.post(
        f"/api/employee-skills/{skill['id']}/handover",
        headers=owner["headers"],
        json={"successor_uid": successor["uid"]},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["skill"]["handover_status"] == "assigned"

    wrong_accept = await test_client.post(
        f"/api/employee-skills/{skill['id']}/accept", headers=colleague["headers"]
    )
    assert wrong_accept.status_code == 403

    accepted = await test_client.post(
        f"/api/employee-skills/{skill['id']}/accept", headers=successor["headers"]
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["skill"]["handover_status"] == "accepted"

    updated = await test_client.put(
        f"/api/employee-skills/{skill['id']}",
        headers=owner["headers"],
        json={"steps": ["确认预算", "核对成本", "给出折扣区间", "店长复核"]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["skill"]["status"] == "needs_review"

    forbidden_edit = await test_client.put(
        f"/api/employee-skills/{skill['id']}",
        headers=colleague["headers"],
        json={"title": "越权修改"},
    )
    assert forbidden_edit.status_code == 404
