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
    username = f"pytest_mtg_{uuid.uuid4().hex[:8]}"
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
async def meeting_users(test_client, local_admin_headers):
    first = await _create_user(test_client, local_admin_headers)
    second = await _create_user(test_client, local_admin_headers)
    try:
        yield first, second
    finally:
        for user in (first, second):
            await test_client.delete(f"/api/auth/users/{user['payload']['id']}", headers=local_admin_headers)


@pytest_asyncio.fixture()
async def meeting_twin(test_client, local_admin_headers):
    slug = f"pytest-meeting-twin-{uuid.uuid4().hex[:10]}"
    response = await test_client.post(
        "/api/founder-twins",
        headers=local_admin_headers,
        json={
            "name": "会议测试分身",
            "slug": slug,
            "description": "只用于会议真实 HTTP 验收",
            "system_prompt": "按已确认经验回答。",
        },
    )
    assert response.status_code == 200, response.text
    try:
        yield response.json()["twin"]
    finally:
        await _db("DELETE FROM meetings WHERE participant_slugs ? :slug", {"slug": slug})
        await _db("DELETE FROM meetings WHERE created_by IN (SELECT uid FROM users WHERE uid LIKE 'pytest_mtg_%')")
        await _db("DELETE FROM experience_entries WHERE twin_slug = :slug", {"slug": slug})
        await _db("DELETE FROM agents WHERE slug = :slug", {"slug": slug})


@pytest_asyncio.fixture()
async def meeting_employee_factory(test_client, local_admin_headers):
    """创建真实数字员工，并按需发布为在岗状态。"""
    created_slugs: list[str] = []

    async def create(*, publish: bool = True) -> str:
        slug = f"pytest-meeting-employee-{uuid.uuid4().hex[:10]}"
        response = await test_client.post(
            "/api/digital-employees",
            headers=local_admin_headers,
            json={
                "name": "会议测试数字员工",
                "slug": slug,
                "profile": {
                    "position": "经营分析官",
                    "department": "经营管理",
                    "responsibilities": "输出经营分析",
                    "capabilities": ["数据查询"],
                    "boundaries": "不代替管理层决策",
                    "acceptance": "结论包含数据口径",
                },
            },
        )
        assert response.status_code == 200, response.text
        if publish:
            published = await test_client.post(
                f"/api/digital-employees/{slug}/publish", headers=local_admin_headers
            )
            assert published.status_code == 200, published.text
        created_slugs.append(slug)
        return slug

    yield create

    for slug in created_slugs:
        await _db("DELETE FROM meetings WHERE participant_slugs ? :slug", {"slug": slug})
        await _db("DELETE FROM scheduled_agent_jobs WHERE agent_slug = :slug", {"slug": slug})
        await _db("DELETE FROM im_bot_configs WHERE agent_slug = :slug", {"slug": slug})
        await _db("DELETE FROM agents WHERE slug = :slug", {"slug": slug})


async def _seed_pending_decision(
    *,
    meeting_id: int,
    slug: str,
    signer_uid: str = ADMIN_UID,
    code: str = "D1",
) -> int:
    decision_row = await _db(
        """
        INSERT INTO meeting_decisions
            (meeting_id, code, title, detail, impact, proposer_slug, status)
        VALUES (:meeting_id, :code, '报价前置确认预算', '所有超过十万元的报价必须先确认客户预算。',
                '适用于大客户报价。', :slug, 'pending')
        RETURNING id
        """,
        {"meeting_id": meeting_id, "slug": slug, "code": code},
    )
    decision_id = int(decision_row[0])
    await _db(
        """
        INSERT INTO meeting_decision_signers (decision_id, uid, status)
        VALUES (:decision_id, :uid, 'pending')
        """,
        {"decision_id": decision_id, "uid": signer_uid},
    )
    await _db(
        """
        UPDATE meetings
        SET status = 'awaiting_decisions',
            summary = '会议同意先确认预算再报价。',
            consensus = '["先确认预算"]'::jsonb
        WHERE id = :meeting_id
        """,
        {"meeting_id": meeting_id},
    )
    return decision_id


async def test_twin_meeting_generation_signature_and_permissions(
    test_client,
    local_admin_headers,
    meeting_users,
    meeting_twin,
):
    first_user, second_user = meeting_users
    slug = meeting_twin["slug"]

    created = await test_client.post(
        "/api/meetings",
        headers=local_admin_headers,
        json={"kind": "twin", "topic": "大客户报价流程", "participant_slugs": [slug], "rounds": 2},
    )
    assert created.status_code == 200, created.text
    meeting = created.json()["meeting"]
    assert meeting["status"] == "draft"
    assert meeting["can_manage"] is True

    hidden = await test_client.get(f"/api/meetings/{meeting['id']}", headers=first_user["headers"])
    assert hidden.status_code == 404

    # 纪要生成的真实模型调用由 unit service 测试覆盖；这里直接写入已生成事实，
    # 验证真实 HTTP 的可见性、人工发言、签字和生效回写。
    decision_id = await _seed_pending_decision(meeting_id=meeting["id"], slug=slug)
    await _db(
        """
        INSERT INTO meeting_messages
            (meeting_id, round, speaker_type, speaker_slug, speaker_name, content, citations)
        VALUES (:meeting_id, 1, 'twin', :slug, :name, '先确认预算，再报价。', '[]'::jsonb)
        """,
        {"meeting_id": meeting["id"], "slug": slug, "name": meeting_twin["name"]},
    )

    detail_response = await test_client.get(
        f"/api/meetings/{meeting['id']}", headers=local_admin_headers
    )
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()["meeting"]
    assert detail["status"] == "awaiting_decisions"
    assert len(detail["decisions"]) == 1
    decision = detail["decisions"][0]
    assert decision["can_sign"] is True

    pending_todos = await test_client.get("/api/workbench/todos", headers=local_admin_headers)
    assert pending_todos.status_code == 200, pending_todos.text
    pending_item = next(
        (item for item in pending_todos.json()["items"] if item["key"] == f"decision:{decision_id}"),
        None,
    )
    assert pending_item is not None
    assert pending_item["source_type"] == "decision"
    assert pending_item["status"] == "waiting_approval"
    assert pending_item["approval_summary"] == "等待负责人签字"

    hidden_todos = await test_client.get("/api/workbench/todos", headers=first_user["headers"])
    assert hidden_todos.status_code == 200
    assert all(item["key"] != f"decision:{decision_id}" for item in hidden_todos.json()["items"])

    # 默认签字人是分身归属人；未受邀的测试用户不能越权签字。
    message = await test_client.post(
        f"/api/meetings/{meeting['id']}/messages",
        headers=local_admin_headers,
        json={"content": "我补充一条人工意见。", "round": 2},
    )
    assert message.status_code == 200, message.text
    assert message.json()["meeting"]["can_join"] is True
    assert any(item["speaker_type"] == "human" for item in message.json()["meeting"]["messages"])

    forbidden_sign = await test_client.post(
        f"/api/meetings/{meeting['id']}/decisions/{decision_id}/sign",
        headers=first_user["headers"],
        json={"action": "approve"},
    )
    assert forbidden_sign.status_code in {403, 404}

    signed = await test_client.post(
        f"/api/meetings/{meeting['id']}/decisions/{decision_id}/sign",
        headers=local_admin_headers,
        json={"action": "approve", "comment": "同意执行"},
    )
    assert signed.status_code == 200, signed.text
    payload = signed.json()["meeting"]
    assert payload["status"] == "effective"
    assert payload["decisions"][0]["status"] == "effective"
    assert payload["decisions"][0]["experience_entry_id"] is not None

    signed_todos = await test_client.get("/api/workbench/todos", headers=local_admin_headers)
    assert signed_todos.status_code == 200
    signed_item = next(
        (item for item in signed_todos.json()["items"] if item["key"] == f"decision:{decision_id}"),
        None,
    )
    assert signed_item is not None
    assert signed_item["status"] == "completed"

    entries = await test_client.get(f"/api/founder-twins/{slug}/entries", headers=local_admin_headers)
    assert entries.status_code == 200, entries.text
    meeting_entries = [item for item in entries.json()["entries"] if item["source_type"] == "meeting"]
    assert len(meeting_entries) == 1
    assert meeting_entries[0]["status"] == "draft"

    # 决议回写的草稿经验只对分身管理者可见，普通用户看不到。
    hidden_entries = await test_client.get(f"/api/founder-twins/{slug}/entries", headers=first_user["headers"])
    assert hidden_entries.status_code == 200
    assert all(item["source_type"] != "meeting" for item in hidden_entries.json()["entries"])


async def test_rejected_decision_has_no_experience_and_rejects_meeting(
    test_client,
    local_admin_headers,
    meeting_twin,
):
    slug = meeting_twin["slug"]
    created = await test_client.post(
        "/api/meetings",
        headers=local_admin_headers,
        json={"kind": "twin", "topic": "是否进入新市场", "participant_slugs": [slug], "rounds": 1},
    )
    assert created.status_code == 200, created.text
    meeting = created.json()["meeting"]
    decision_id = await _seed_pending_decision(meeting_id=meeting["id"], slug=slug)

    rejected = await test_client.post(
        f"/api/meetings/{meeting['id']}/decisions/{decision_id}/sign",
        headers=local_admin_headers,
        json={"action": "reject", "comment": "证据不足，暂不进入"},
    )
    assert rejected.status_code == 200, rejected.text
    payload = rejected.json()["meeting"]
    assert payload["status"] == "rejected"
    assert payload["decisions"][0]["status"] == "rejected"
    assert payload["decisions"][0]["experience_entry_id"] is None
    assert payload["decisions"][0]["signers"][0]["comment"] == "证据不足，暂不进入"

    rejected_todos = await test_client.get("/api/workbench/todos", headers=local_admin_headers)
    assert rejected_todos.status_code == 200
    rejected_item = next(
        (item for item in rejected_todos.json()["items"] if item["key"] == f"decision:{decision_id}"),
        None,
    )
    assert rejected_item is not None
    assert rejected_item["status"] == "failed"

    entries = await test_client.get(f"/api/founder-twins/{slug}/entries", headers=local_admin_headers)
    assert entries.status_code == 200
    assert all(item["source_type"] != "meeting" for item in entries.json()["entries"])


async def test_digital_employee_meeting_requires_active_visible_participants(
    test_client,
    local_admin_headers,
    meeting_employee_factory,
):
    active_slug = await meeting_employee_factory(publish=True)
    draft_slug = await meeting_employee_factory(publish=False)

    created = await test_client.post(
        "/api/meetings",
        headers=local_admin_headers,
        json={
            "kind": "digital_employee",
            "topic": "经营分析口径对齐",
            "participant_slugs": [active_slug],
            "rounds": 2,
        },
    )
    assert created.status_code == 200, created.text
    meeting = created.json()["meeting"]
    assert meeting["kind"] == "digital_employee"
    assert meeting["status"] == "draft"
    assert meeting["participants"][0]["slug"] == active_slug
    assert meeting["participants"][0]["position"] == "经营分析官"
    assert meeting["can_manage"] is True
    assert meeting["can_join"] is True

    rejected = await test_client.post(
        "/api/meetings",
        headers=local_admin_headers,
        json={
            "kind": "digital_employee",
            "topic": "草稿员工不能参会",
            "participant_slugs": [draft_slug],
        },
    )
    assert rejected.status_code == 409
