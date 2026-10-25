from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from test.live_api_cleanup import cleanup_test_chat_resources, make_test_conversation_title
from yuxi.repositories.agent_repository import AgentRepository
from yuxi.services.agent_request_queue_service import dispatch_next_request
from yuxi.storage.postgres.manager import pg_manager
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


async def _fetch_one(sql: str, params: dict | None = None):
    """读取一行测试造数结果，例如 RETURNING 的消息主键。"""
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params or {})
            return result.first()
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
async def read_only_admin(test_client, local_admin_headers):
    """创建没有员工 manage_scope 的管理员，验证角色本身不能绕过资源权限。"""
    username = f"pytest_ro_{uuid.uuid4().hex[:6]}"
    password = f"Pw!{uuid.uuid4().hex[:12]}"
    departments = await test_client.get("/api/departments", headers=local_admin_headers)
    assert departments.status_code == 200, departments.text
    department_id = departments.json()[0]["id"]
    response = await test_client.post(
        "/api/auth/users",
        headers=local_admin_headers,
        json={"username": username, "password": password, "role": "admin", "department_id": department_id},
    )
    assert response.status_code == 200, response.text
    user_payload = response.json()
    token = AuthUtils.create_access_token({"sub": str(user_payload["id"])})
    try:
        yield {"user": user_payload, "password": password, "headers": {"Authorization": f"Bearer {token}"}}
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

    employee_detail = await test_client.get(f"/api/digital-employees/{slug}", headers=standard_user["headers"])
    assert employee_detail.status_code == 404
    assert (await test_client.get(f"/api/agent/{slug}", headers=standard_user["headers"])).status_code == 404
    thread = await test_client.post(
        "/api/chat/thread",
        headers=standard_user["headers"],
        json={"agent_id": slug, "title": make_test_conversation_title("草稿员工线程")},
    )
    assert thread.status_code == 404, thread.text

    manager_thread = await test_client.post(
        "/api/chat/thread",
        headers=local_admin_headers,
        json={"agent_id": slug, "title": make_test_conversation_title("管理者草稿线程")},
    )
    assert manager_thread.status_code == 409, manager_thread.text
    assert (
        await test_client.get(f"/api/agent/{slug}/memories", headers=standard_user["headers"])
    ).status_code == 404
    assert (
        await test_client.get(f"/api/agent/{slug}/memories", headers=local_admin_headers)
    ).status_code == 200
    draft_disable = await test_client.post(f"/api/digital-employees/{slug}/disable", headers=local_admin_headers)
    assert draft_disable.status_code == 409, draft_disable.text

    manager_detail = await test_client.get(f"/api/digital-employees/{slug}", headers=local_admin_headers)
    assert manager_detail.status_code == 200, manager_detail.text
    assert manager_detail.json()["employee"]["digital_employee_profile"]["status"] == "draft"
    generic_detail = await test_client.get(f"/api/agent/{slug}", headers=local_admin_headers)
    assert generic_detail.status_code == 200, generic_detail.text
    assert generic_detail.json()["agent"]["kind"] == "digital_employee"


async def test_digital_employee_publish_then_visible_and_generic_agent_isolation(
    test_client, local_admin_headers, standard_user, read_only_admin, employee_factory
):
    """发布后按共享范围可见，且通用 Agent 路由不能修改、删除或复制员工。"""
    share_config = _user_share(standard_user["user"]["uid"])
    share_config["read_scope"]["user_uids"].append(read_only_admin["user"]["uid"])
    employee = await employee_factory(share_config=share_config)
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

    read_only_detail = await test_client.get(f"/api/digital-employees/{slug}", headers=read_only_admin["headers"])
    assert read_only_detail.status_code == 200, read_only_detail.text
    assert read_only_detail.json()["employee"]["can_manage"] is False
    for response in (
        await test_client.put(
            f"/api/digital-employees/{slug}", headers=read_only_admin["headers"], json={"name": "越权改名"}
        ),
        await test_client.post(f"/api/digital-employees/{slug}/publish", headers=read_only_admin["headers"]),
        await test_client.post(f"/api/digital-employees/{slug}/disable", headers=read_only_admin["headers"]),
        await test_client.delete(f"/api/digital-employees/{slug}", headers=read_only_admin["headers"]),
    ):
        assert response.status_code == 403, response.text


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


async def test_digital_employee_locking_read_refreshes_identity_map_snapshot(employee_factory):
    """锁定读取必须用数据库最新行覆盖同一 session 中的旧员工快照。"""
    employee = await employee_factory()
    slug = employee["slug"]
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as reader:
            repo = AgentRepository(reader)
            before = await repo.get_by_slug(slug)
            assert before.digital_employee_profile["status"] == "draft"
            async with session_factory() as writer:
                await writer.execute(
                    text(
                        "UPDATE agents SET digital_employee_profile = "
                        "jsonb_set(digital_employee_profile, '{status}', '\"active\"') "
                        "WHERE slug = :slug"
                    ),
                    {"slug": slug},
                )
                await writer.commit()
            after = await repo.get_by_slug_for_update(slug=slug)
            assert after is before
            assert after.digital_employee_profile["status"] == "active"
            await reader.rollback()
    finally:
        await engine.dispose()


async def test_digital_employee_disable_blocks_queued_dispatch_and_continue(
    test_client, local_admin_headers, employee_factory
):
    """停用后由队列派发或人工继续产生的 Run 也必须被拒绝。"""
    employee = await employee_factory()
    slug = employee["slug"]
    published = await test_client.post(f"/api/digital-employees/{slug}/publish", headers=local_admin_headers)
    assert published.status_code == 200, published.text
    me = await test_client.get("/api/auth/me", headers=local_admin_headers)
    assert me.status_code == 200, me.text
    admin_uid = str(me.json()["uid"])

    thread_id = None
    request_id = f"pytest-queue-{uuid.uuid4().hex[:10]}"
    message_id = None
    try:
        thread = await test_client.post(
            "/api/chat/thread",
            headers=local_admin_headers,
            json={"agent_id": slug, "title": make_test_conversation_title("停用队列守卫")},
        )
        assert thread.status_code == 200, thread.text
        thread_id = thread.json()["id"]
        conversation = await _fetch_one(
            "SELECT id FROM conversations WHERE thread_id = :thread_id",
            {"thread_id": thread_id},
        )
        assert conversation is not None

        disabled = await test_client.post(f"/api/digital-employees/{slug}/disable", headers=local_admin_headers)
        assert disabled.status_code == 200, disabled.text

        message = await _fetch_one(
            """
            INSERT INTO messages (conversation_id, role, content, delivery_status, created_at, request_id)
            VALUES (:conversation_id, 'user', '停用后队列请求', 'queued', now(), :request_id)
            RETURNING id
            """,
            {"conversation_id": conversation[0], "request_id": request_id},
        )
        assert message is not None
        message_id = int(message[0])
        await _execute(
            """
            INSERT INTO agent_run_requests (
                request_id, uid, agent_slug, conversation_thread_id, source, channel,
                origin_metadata, queue_policy, status, input_message_id, input_payload, created_at, updated_at
            ) VALUES (
                :request_id, :uid, :slug, :thread_id, 'chat', 'web',
                '{}'::json, 'enqueue', 'queued', :message_id, '{}'::json, now(), now()
            )
            """,
            {
                "request_id": request_id,
                "uid": admin_uid,
                "slug": slug,
                "thread_id": thread_id,
                "message_id": message_id,
            },
        )

        continued = await test_client.post(
            f"/api/agent/thread/{thread_id}/requests/continue",
            params={"agent_slug": slug},
            headers=local_admin_headers,
        )
        assert continued.status_code == 409, continued.text
        assert continued.json()["detail"]["code"] == "agent_not_runnable"

        dispatched = await dispatch_next_request(uid=admin_uid, agent_slug=slug, thread_id=thread_id)
        assert dispatched is None
        row = await _fetch_one(
            "SELECT status, error_message FROM agent_run_requests WHERE request_id = :request_id",
            {"request_id": request_id},
        )
        assert row is not None
        assert row[0] == "rejected"
        assert "未发布或已停用" in row[1]
        message_row = await _fetch_one(
            "SELECT delivery_status FROM messages WHERE id = :message_id",
            {"message_id": message_id},
        )
        assert message_row[0] == "rejected"
        runs = await _fetch_one(
            "SELECT count(*) FROM agent_runs WHERE request_id = :request_id",
            {"request_id": request_id},
        )
        assert runs[0] == 0
    finally:
        await _execute("DELETE FROM agent_run_requests WHERE request_id = :request_id", {"request_id": request_id})
        if message_id is not None:
            await _execute("DELETE FROM messages WHERE id = :message_id", {"message_id": message_id})
        if thread_id:
            await test_client.delete(f"/api/chat/thread/{thread_id}", headers=local_admin_headers)
        # dispatch_next_request 使用进程内 pg_manager；每个 pytest 测试有独立事件循环，
        # 必须在这里释放连接池，避免后续测试拿到上一轮 loop 的连接。
        await pg_manager.close()


async def test_digital_employee_delete_draft_with_reference_returns_409(
    test_client, local_admin_headers, employee_factory
):
    """草稿被对话、定时任务或 IM Bot 引用时拒绝删除，并覆盖三类引用入口。"""
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

        ref_thread_id = f"pytest-ref-{uuid.uuid4().hex[:12]}"
        await _execute(
            """
            INSERT INTO conversations (
                thread_id, uid, agent_id, title, status, is_pinned, created_at, updated_at, project_id
            ) VALUES (
                :thread_id, :uid, :slug, 'pytest-reference-conversation', 'active', false, now(), now(), :project_id
            )
            """,
            {"thread_id": ref_thread_id, "uid": admin_uid, "slug": slug, "project_id": project_id},
        )
        blocked_by_conversation = await test_client.delete(
            f"/api/digital-employees/{slug}", headers=local_admin_headers
        )
        assert blocked_by_conversation.status_code == 409, blocked_by_conversation.text
        await _execute("DELETE FROM conversations WHERE thread_id = :thread_id", {"thread_id": ref_thread_id})

        ref_job_id = f"pytest-ref-job-{uuid.uuid4().hex[:8]}"
        await _execute(
            """
            INSERT INTO scheduled_agent_jobs (
                id, uid, creation_request_id, creation_intent_hash, project_id, agent_slug,
                name, prompt, tool_approval_mode, cron_expression, timezone, enabled,
                next_run_at, created_at, updated_at
            ) VALUES (
                :job_id, :uid, :request_id, 'pytest-reference', :project_id, :slug,
                'pytest-reference-job', 'test', 'default', '0 9 * * *', 'Asia/Shanghai', false,
                now(), now(), now()
            )
            """,
            {
                "job_id": ref_job_id,
                "uid": admin_uid,
                "request_id": f"pytest-ref-job-req-{uuid.uuid4().hex[:8]}",
                "project_id": project_id,
                "slug": slug,
            },
        )
        blocked_by_scheduled = await test_client.delete(f"/api/digital-employees/{slug}", headers=local_admin_headers)
        assert blocked_by_scheduled.status_code == 409, blocked_by_scheduled.text
        await _execute("DELETE FROM scheduled_agent_jobs WHERE id = :job_id", {"job_id": ref_job_id})

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
        blocked_by_bot = await test_client.delete(f"/api/digital-employees/{slug}", headers=local_admin_headers)
        assert blocked_by_bot.status_code == 409, blocked_by_bot.text
        await _execute("DELETE FROM im_bot_configs WHERE agent_slug = :slug", {"slug": slug})

        deleted = await test_client.delete(f"/api/digital-employees/{slug}", headers=local_admin_headers)
        assert deleted.status_code == 200, deleted.text
        assert (await test_client.get(f"/api/digital-employees/{slug}", headers=local_admin_headers)).status_code == 404
    finally:
        await _execute("DELETE FROM im_bot_configs WHERE agent_slug = :slug", {"slug": slug})
        await _execute("DELETE FROM scheduled_agent_jobs WHERE agent_slug = :slug", {"slug": slug})
        await _execute("DELETE FROM conversations WHERE agent_id = :slug", {"slug": slug})
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
