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


async def _create_workbench_user(test_client, local_admin_headers) -> dict:
    username = f"pytest_wb_{uuid.uuid4().hex[:8]}"
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
    return {
        "payload": payload,
        "uid": payload["uid"],
        "headers": {"Authorization": f"Bearer {token}"},
    }


@pytest_asyncio.fixture()
async def workbench_user(test_client, local_admin_headers):
    user = await _create_workbench_user(test_client, local_admin_headers)
    try:
        yield user
    finally:
        await test_client.delete(f"/api/auth/users/{user['payload']['id']}", headers=local_admin_headers)


@pytest_asyncio.fixture()
async def other_workbench_user(test_client, local_admin_headers):
    user = await _create_workbench_user(test_client, local_admin_headers)
    try:
        yield user
    finally:
        await test_client.delete(f"/api/auth/users/{user['payload']['id']}", headers=local_admin_headers)


@pytest_asyncio.fixture()
async def seeded_work_item(workbench_user):
    """为独立测试用户造一条审批中断的新 Run 和一条同线程旧 Run。"""
    suffix = uuid.uuid4().hex[:12]
    uid = workbench_user["uid"]
    project_id = f"pytest-wb-project-{suffix}"
    agent_slug = f"pytest-wb-agent-{suffix}"
    thread_id = f"pytest-wb-thread-{suffix}"
    run_id = f"pytest-wb-run-{suffix}"
    old_run_id = f"pytest-wb-old-{suffix}"
    await _db(
        """
        INSERT INTO projects (id, uid, name, selection_status, workdir_path, directory_mode, status)
        VALUES (:project_id, :uid, '待办测试项目', 'implicit', :workdir, 'managed', 'active')
        """,
        {"project_id": project_id, "uid": uid, "workdir": f"pytest/{suffix}"},
    )
    await _db(
        """
        INSERT INTO agents (
            slug, backend_id, name, pics, config_json, share_config, kind,
            digital_employee_profile, is_default, is_subagent
        )
        VALUES (
            :slug, 'ChatbotAgent', '待办测试助手', '[]'::jsonb, '{}'::jsonb, '{}'::jsonb,
            'agent', '{}'::jsonb, FALSE, FALSE
        )
        """,
        {"slug": agent_slug},
    )
    conversation = await _db(
        """
        INSERT INTO conversations (thread_id, uid, agent_id, title, status, is_pinned, project_id)
        VALUES (:thread_id, :uid, :agent_slug, '季度复盘', 'active', FALSE, :project_id)
        RETURNING id
        """,
        {"thread_id": thread_id, "uid": uid, "agent_slug": agent_slug, "project_id": project_id},
    )
    conversation_id = conversation.id
    await _db(
        """
        INSERT INTO agent_runs (
            id, conversation_thread_id, runtime_scope_id, agent_slug, uid, status, request_id,
            source, channel, conversation_id, run_type, input_payload, origin_metadata, token_usage,
            created_at, updated_at
        )
        VALUES (
            :run_id, :thread_id, :thread_id, :agent_slug, :uid, 'completed', :request_id,
            'chat', 'web', :conversation_id, 'chat', '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,
            NOW() - INTERVAL '2 minutes', NOW() - INTERVAL '2 minutes'
        )
        """,
        {
            "run_id": old_run_id,
            "thread_id": thread_id,
            "agent_slug": agent_slug,
            "uid": uid,
            "request_id": f"pytest-wb-old-req-{suffix}",
            "conversation_id": conversation_id,
        },
    )
    await _db(
        """
        INSERT INTO agent_runs (
            id, conversation_thread_id, runtime_scope_id, agent_slug, uid, status, request_id,
            source, channel, conversation_id, run_type, input_payload, origin_metadata, token_usage,
            error_type, created_at, updated_at
        )
        VALUES (
            :run_id, :thread_id, :thread_id, :agent_slug, :uid, 'interrupted', :request_id,
            'chat', 'web', :conversation_id, 'chat', '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,
            'human_approval_required', NOW(), NOW()
        )
        """,
        {
            "run_id": run_id,
            "thread_id": thread_id,
            "agent_slug": agent_slug,
            "uid": uid,
            "request_id": f"pytest-wb-req-{suffix}",
            "conversation_id": conversation_id,
        },
    )
    try:
        yield {
            "uid": uid,
            "run_id": run_id,
            "old_run_id": old_run_id,
            "key": f"run:{run_id}",
            "project_id": project_id,
            "agent_slug": agent_slug,
            "conversation_id": conversation_id,
            "thread_id": thread_id,
        }
    finally:
        await _db("DELETE FROM work_item_views WHERE uid = :uid", {"uid": uid})
        await _db("DELETE FROM agent_runs WHERE id IN (:run_id, :old_run_id)", {
            "run_id": run_id,
            "old_run_id": old_run_id,
        })
        await _db("DELETE FROM conversations WHERE id = :cid", {"cid": conversation_id})
        await _db("DELETE FROM projects WHERE id = :project_id", {"project_id": project_id})
        await _db("DELETE FROM agents WHERE slug = :slug", {"slug": agent_slug})


@pytest_asyncio.fixture()
async def seeded_two_work_items(workbench_user, seeded_work_item):
    """在同一用户下追加一条定时任务发起的完成 Run。"""
    suffix = uuid.uuid4().hex[:12]
    thread_id = f"pytest-wb-thread2-{suffix}"
    run_id = f"pytest-wb-scheduled-{suffix}"
    conversation = await _db(
        """
        INSERT INTO conversations (thread_id, uid, agent_id, title, status, is_pinned, project_id)
        VALUES (:thread_id, :uid, :agent_slug, '定时提醒', 'active', FALSE, :project_id)
        RETURNING id
        """,
        {
            "thread_id": thread_id,
            "uid": workbench_user["uid"],
            "agent_slug": seeded_work_item["agent_slug"],
            "project_id": seeded_work_item["project_id"],
        },
    )
    conversation_id = conversation.id
    await _db(
        """
        INSERT INTO agent_runs (
            id, conversation_thread_id, runtime_scope_id, agent_slug, uid, status, request_id,
            source, channel, conversation_id, run_type, input_payload, origin_metadata, token_usage,
            created_at, updated_at
        )
        VALUES (
            :run_id, :thread_id, :thread_id, :agent_slug, :uid, 'completed', :request_id,
            'scheduled_agent', 'web', :conversation_id, 'chat', '{}'::jsonb, '{}'::jsonb,
            '{}'::jsonb, NOW() + INTERVAL '1 minute', NOW() + INTERVAL '1 minute'
        )
        """,
        {
            "run_id": run_id,
            "thread_id": thread_id,
            "agent_slug": seeded_work_item["agent_slug"],
            "uid": workbench_user["uid"],
            "request_id": f"pytest-wb-scheduled-req-{suffix}",
            "conversation_id": conversation_id,
        },
    )
    try:
        yield {"run_id": run_id, "thread_id": thread_id, "conversation_id": conversation_id}
    finally:
        await _db("DELETE FROM agent_runs WHERE id = :run_id", {"run_id": run_id})
        await _db("DELETE FROM conversations WHERE id = :cid", {"cid": conversation_id})


async def test_workbench_todos_list_detail_and_mark_viewed(
    test_client,
    workbench_user,
    seeded_work_item,
):
    response = await test_client.get("/api/workbench/todos", headers=workbench_user["headers"])
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["source_type"] == "agent_run"
    assert payload["items"][0]["status"] == "waiting_approval"
    assert payload["items"][0]["approval_kind"] == "tool"
    assert payload["items"][0]["unread"] is True

    key = seeded_work_item["key"]
    detail = await test_client.get(f"/api/workbench/todos/{key}", headers=workbench_user["headers"])
    assert detail.status_code == 200, detail.text
    assert detail.json()["item"]["thread_id"] == seeded_work_item["thread_id"]

    viewed = await test_client.post(
        f"/api/workbench/todos/{key}/viewed",
        headers=workbench_user["headers"],
    )
    assert viewed.status_code == 200, viewed.text
    summary = await test_client.get("/api/workbench/todos/summary", headers=workbench_user["headers"])
    assert summary.status_code == 200, summary.text
    assert summary.json()["unread"] == 0
    assert summary.json()["counts"]["waiting_approval"] == 1


async def test_workbench_stale_run_key_is_not_found(
    test_client,
    workbench_user,
    seeded_work_item,
):
    response = await test_client.get(
        f"/api/workbench/todos/run:{seeded_work_item['old_run_id']}",
        headers=workbench_user["headers"],
    )
    assert response.status_code == 404


async def test_workbench_invalid_status_is_rejected(
    test_client,
    workbench_user,
):
    response = await test_client.get(
        "/api/workbench/todos?status=unknown",
        headers=workbench_user["headers"],
    )
    assert response.status_code == 422


async def test_workbench_isolates_users(
    test_client,
    workbench_user,
    other_workbench_user,
    seeded_work_item,
):
    response = await test_client.get("/api/workbench/todos", headers=other_workbench_user["headers"])
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0


async def test_workbench_filters_paginates_and_marks_scheduled_initiator(
    test_client,
    workbench_user,
    seeded_work_item,
    seeded_two_work_items,
):
    filtered = await test_client.get(
        "/api/workbench/todos?status=waiting_approval",
        headers=workbench_user["headers"],
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["key"] == seeded_work_item["key"]

    first_page = await test_client.get(
        "/api/workbench/todos?limit=1",
        headers=workbench_user["headers"],
    )
    assert first_page.status_code == 200, first_page.text
    assert len(first_page.json()["items"]) == 1
    assert first_page.json()["total"] == 2

    all_items = await test_client.get("/api/workbench/todos", headers=workbench_user["headers"])
    scheduled = next(
        item for item in all_items.json()["items"] if item["run_id"] == seeded_two_work_items["run_id"]
    )
    assert scheduled["initiator_type"] == "digital_employee"
    assert scheduled["initiator_name"] == "待办测试助手"
    assert scheduled["source_type"] == "agent_run"
