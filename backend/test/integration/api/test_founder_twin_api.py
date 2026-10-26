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
    username = f"pytest_ft_{uuid.uuid4().hex[:8]}"
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
async def twin_user(test_client, local_admin_headers):
    user = await _create_user(test_client, local_admin_headers)
    try:
        yield user
    finally:
        await test_client.delete(f"/api/auth/users/{user['payload']['id']}", headers=local_admin_headers)


@pytest_asyncio.fixture()
async def other_twin_user(test_client, local_admin_headers):
    user = await _create_user(test_client, local_admin_headers)
    try:
        yield user
    finally:
        await test_client.delete(f"/api/auth/users/{user['payload']['id']}", headers=local_admin_headers)


@pytest_asyncio.fixture()
async def founder_twin(test_client, local_admin_headers):
    slug = f"pytest-twin-{uuid.uuid4().hex[:10]}"
    response = await test_client.post(
        "/api/founder-twins",
        headers=local_admin_headers,
        json={
            "name": "测试老板分身",
            "slug": slug,
            "description": "只用于真实 HTTP 验收",
            "system_prompt": "按已确认经验回答。",
        },
    )
    assert response.status_code == 200, response.text
    try:
        yield response.json()["twin"]
    finally:
        await _db("DELETE FROM experience_consultations WHERE twin_slug = :slug", {"slug": slug})
        await _db("DELETE FROM handoff_requests WHERE twin_slug = :slug", {"slug": slug})
        await _db("DELETE FROM experience_entries WHERE twin_slug = :slug", {"slug": slug})
        await _db("DELETE FROM agents WHERE slug = :slug", {"slug": slug})


async def _set_confirmed(entry_id: int, *, embedding: str = "[1.0, 0.0]") -> None:
    await _db(
        """
        UPDATE experience_entries
        SET status = 'confirmed',
            embedding = CAST(:embedding AS jsonb),
            embedding_model = 'pytest-mock',
            confirmed_by = :uid,
            confirmed_at = NOW()
        WHERE id = :entry_id
        """,
        {"entry_id": entry_id, "embedding": embedding, "uid": ADMIN_UID},
    )


async def test_founder_twin_admin_lifecycle_and_employee_read_only(
    test_client,
    local_admin_headers,
    twin_user,
    founder_twin,
):
    slug = founder_twin["slug"]

    employee_list = await test_client.get("/api/founder-twins", headers=twin_user["headers"])
    assert employee_list.status_code == 200, employee_list.text
    listed = next(item for item in employee_list.json()["twins"] if item["slug"] == slug)
    assert listed["can_manage"] is False
    assert "draft_entries" not in listed["stats"]
    assert "open_handoffs" not in listed["stats"]

    employee_agent_detail = await test_client.get(f"/api/agent/{slug}", headers=twin_user["headers"])
    assert employee_agent_detail.status_code == 200, employee_agent_detail.text
    assert "config_json" not in employee_agent_detail.json()["agent"]

    forbidden = await test_client.post(
        f"/api/founder-twins/{slug}/entries",
        headers=twin_user["headers"],
        json={"title": "员工不能写", "judgment": "无权"},
    )
    assert forbidden.status_code == 403

    first = await test_client.post(
        f"/api/founder-twins/{slug}/entries",
        headers=local_admin_headers,
        json={"title": "定价经验", "judgment": "先看成本再报价", "situation": "客户问价"},
    )
    assert first.status_code == 200, first.text
    first_entry = first.json()["entry"]
    assert first_entry["status"] == "draft"

    second = await test_client.post(
        f"/api/founder-twins/{slug}/entries",
        headers=local_admin_headers,
        json={"title": "交付经验", "judgment": "先确认边界再排期"},
    )
    assert second.status_code == 200, second.text
    second_entry = second.json()["entry"]
    await _set_confirmed(second_entry["id"])

    employee_entries = await test_client.get(
        f"/api/founder-twins/{slug}/entries",
        headers=twin_user["headers"],
    )
    assert employee_entries.status_code == 200, employee_entries.text
    assert [item["id"] for item in employee_entries.json()["entries"]] == [second_entry["id"]]

    edited = await test_client.put(
        f"/api/founder-twins/{slug}/entries/{second_entry['id']}",
        headers=local_admin_headers,
        json={"title": "交付经验（修订）"},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["entry"]["status"] == "draft"
    assert edited.json()["entry"]["title"] == "交付经验（修订）"

    invalidated = await test_client.post(
        f"/api/founder-twins/{slug}/entries/{first_entry['id']}/invalidate",
        headers=local_admin_headers,
    )
    assert invalidated.status_code == 200, invalidated.text
    assert invalidated.json()["entry"]["status"] == "invalid"

    invalid_confirm = await test_client.post(
        f"/api/founder-twins/{slug}/entries/{first_entry['id']}/confirm",
        headers=local_admin_headers,
    )
    assert invalid_confirm.status_code == 422

    agent_list = await test_client.get(
        "/api/agent?kind=founder_twin",
        headers=twin_user["headers"],
    )
    assert agent_list.status_code == 200, agent_list.text
    assert any(item["slug"] == slug for item in agent_list.json()["agents"])

    forbidden_confirm = await test_client.post(
        f"/api/founder-twins/{slug}/entries/{first_entry['id']}/confirm",
        headers=twin_user["headers"],
    )
    assert forbidden_confirm.status_code == 403

    generic_update = await test_client.put(
        f"/api/agent/{slug}",
        headers=local_admin_headers,
        json={"description": "不能走通用更新"},
    )
    assert generic_update.status_code == 400
    duplicate = await test_client.post(f"/api/agent/{slug}/duplicate", headers=twin_user["headers"])
    assert duplicate.status_code == 400


async def test_founder_twin_consultations_and_handoffs_respect_owner_scope(
    test_client,
    local_admin_headers,
    twin_user,
    other_twin_user,
    founder_twin,
):
    slug = founder_twin["slug"]
    cited_entry = await test_client.post(
        f"/api/founder-twins/{slug}/entries",
        headers=local_admin_headers,
        json={"title": "引用经验", "judgment": "先确认边界"},
    )
    assert cited_entry.status_code == 200, cited_entry.text
    cited_entry_id = cited_entry.json()["entry"]["id"]
    await _set_confirmed(cited_entry_id)
    first_handoff = await _db(
        """
        INSERT INTO handoff_requests (twin_slug, uid, assigned_to_uid, question, status)
        VALUES (:slug, :uid, :assigned_to_uid, '第一个转交问题', 'open')
        RETURNING id
        """,
        {
            "slug": slug,
            "uid": twin_user["uid"],
            "assigned_to_uid": other_twin_user["uid"],
        },
    )
    second_handoff = await _db(
        """
        INSERT INTO handoff_requests (twin_slug, uid, question, status)
        VALUES (:slug, :uid, '第二个转交问题', 'open')
        RETURNING id
        """,
        {"slug": slug, "uid": other_twin_user["uid"]},
    )
    await _db(
        """
        INSERT INTO experience_consultations (
            twin_slug, uid, thread_id, run_id, question, answer, cited_entry_ids, confidence
        )
        VALUES (:slug, :uid, 'thread-a', 'run-a', '问题 A', '答案 A', CAST(:cited AS jsonb), 0.8)
        """,
        {"slug": slug, "uid": twin_user["uid"], "cited": f"[{cited_entry_id}]"},
    )
    await _db(
        """
        INSERT INTO experience_consultations (
            twin_slug, uid, thread_id, run_id, question, answer, cited_entry_ids, confidence
        )
        VALUES (:slug, :uid, 'thread-b', 'run-b', '问题 B', '答案 B', '[]'::jsonb, 0.7)
        """,
        {"slug": slug, "uid": other_twin_user["uid"]},
    )

    own_handoffs = await test_client.get(
        f"/api/founder-twins/{slug}/handoffs",
        headers=twin_user["headers"],
    )
    assert own_handoffs.status_code == 200, own_handoffs.text
    assert [item["id"] for item in own_handoffs.json()["handoffs"]] == [first_handoff.id]

    own_consultations = await test_client.get(
        f"/api/founder-twins/{slug}/consultations",
        headers=twin_user["headers"],
    )
    assert own_consultations.status_code == 200, own_consultations.text
    assert [item["question"] for item in own_consultations.json()["consultations"]] == ["问题 A"]
    assert own_consultations.json()["consultations"][0]["cited_entries"] == [
        {"id": cited_entry_id, "title": "引用经验", "status": "confirmed"}
    ]

    forbidden_update = await test_client.put(
        f"/api/founder-twins/{slug}/handoffs/{first_handoff.id}",
        headers=twin_user["headers"],
        json={"answer": "员工不能替老板回答"},
    )
    assert forbidden_update.status_code == 403

    forbidden_reassign = await test_client.put(
        f"/api/founder-twins/{slug}/handoffs/{first_handoff.id}",
        headers=other_twin_user["headers"],
        json={"assigned_to_uid": twin_user["uid"]},
    )
    assert forbidden_reassign.status_code == 403

    assigned_handoffs = await test_client.get(
        f"/api/founder-twins/{slug}/handoffs",
        headers=other_twin_user["headers"],
    )
    assert assigned_handoffs.status_code == 200, assigned_handoffs.text
    assigned_items = assigned_handoffs.json()["handoffs"]
    assert {item["id"] for item in assigned_items} == {first_handoff.id, second_handoff.id}
    can_answer = {item["id"]: item["can_answer"] for item in assigned_items}
    assert can_answer[first_handoff.id] is True
    assert can_answer[second_handoff.id] is False

    answered = await test_client.put(
        f"/api/founder-twins/{slug}/handoffs/{first_handoff.id}",
        headers=other_twin_user["headers"],
        json={"answer": "老板本人的回答"},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["handoff"]["status"] == "answered"
    assert answered.json()["handoff"]["answer"] == "老板本人的回答"

    admin_handoffs = await test_client.get(
        f"/api/founder-twins/{slug}/handoffs",
        headers=local_admin_headers,
    )
    assert admin_handoffs.status_code == 200, admin_handoffs.text
    assert {item["id"] for item in admin_handoffs.json()["handoffs"]} == {first_handoff.id, second_handoff.id}

    missing = await test_client.put(
        f"/api/founder-twins/{slug}/handoffs/999999",
        headers=local_admin_headers,
        json={"status": "closed"},
    )
    assert missing.status_code == 404
