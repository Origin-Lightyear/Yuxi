from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test.live_api_cleanup import make_test_conversation_metadata, make_test_conversation_title

from yuxi.storage.postgres.models_business import IMBotConfig

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _session_factory():
    """在 API 同一 PostgreSQL 上自建会话工厂，用于创建/清理测试机器人。"""
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture()
async def channel_bot(test_client, admin_headers):
    """创建归属 admin 的启用钉钉机器人，返回其 id，测试结束后删除。"""
    me = await test_client.get("/api/auth/me", headers=admin_headers)
    assert me.status_code == 200, me.text
    owner_uid = me.json()["uid"]

    engine, factory = _session_factory()
    bot_id = None
    try:
        async with factory() as db:
            bot = IMBotConfig(
                platform="dingtalk",
                name=f"pytest-channel-{uuid.uuid4().hex[:8]}",
                enabled=True,
                credentials_encrypted="pytest-placeholder",
                owner_uid=owner_uid,
                agent_slug="default-chatbot",
                require_binding=False,
            )
            db.add(bot)
            await db.commit()
            await db.refresh(bot)
            bot_id = bot.id
        yield str(bot_id)
    finally:
        async with factory() as db:
            await db.execute(delete(IMBotConfig).where(IMBotConfig.id == bot_id))
            await db.commit()
        await engine.dispose()


async def test_channel_state_command_reads_thread_without_creating_run(test_client, admin_headers, channel_bot):
    agents_response = await test_client.get("/api/agent", headers=admin_headers)
    assert agents_response.status_code == 200, agents_response.text
    agent_slug = agents_response.json()["agents"][0].get("slug") or agents_response.json()["agents"][0].get("agent_id")

    thread_response = await test_client.post(
        "/api/chat/thread",
        json={
            "agent_id": agent_slug,
            "title": make_test_conversation_title("agent-invocation-channel"),
            "metadata": make_test_conversation_metadata("agent-invocation-channel"),
        },
        headers=admin_headers,
    )
    assert thread_response.status_code == 200, thread_response.text
    thread_id = thread_response.json().get("thread_id") or thread_response.json().get("id")

    response = await test_client.post(
        "/api/agent-invocation/channel/messages",
        json={
            "channel": "dingtalk",
            "account_id": channel_bot,
            "chat_id": "state-check",
            "thread_id": thread_id,
            "message_id": f"state-{uuid.uuid4().hex}",
            "agent_slug": agent_slug,
            "message": {"type": "text", "text": "/state"},
        },
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "command"
    assert response.json()["command"] == "state"
    assert "agent_state" in response.json()["state"]


async def test_channel_reject_without_pending_approval_returns_conflict(test_client, admin_headers, channel_bot):
    agents_response = await test_client.get("/api/agent", headers=admin_headers)
    assert agents_response.status_code == 200, agents_response.text
    agent_slug = agents_response.json()["agents"][0].get("slug") or agents_response.json()["agents"][0].get(
        "agent_id"
    )

    thread_response = await test_client.post(
        "/api/chat/thread",
        json={"agent_id": agent_slug, "title": f"pytest-channel-reject-{uuid.uuid4().hex[:8]}", "metadata": {}},
        headers=admin_headers,
    )
    assert thread_response.status_code == 200, thread_response.text
    thread_id = thread_response.json().get("thread_id") or thread_response.json().get("id")

    response = await test_client.post(
        "/api/agent-invocation/channel/messages",
        json={
            "channel": "dingtalk",
            "account_id": channel_bot,
            "chat_id": "reject-check",
            "thread_id": thread_id,
            "message_id": f"reject-{uuid.uuid4().hex}",
            "agent_slug": agent_slug,
            "message": {"type": "text", "text": "/reject"},
        },
        headers=admin_headers,
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "no_pending_approval"
