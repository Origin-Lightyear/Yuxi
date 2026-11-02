"""IM 账号绑定端到端用例：真实 Channel 入口 + 真实 PostgreSQL。

验证两条不同 ``sender_id``（分别绑定不同用户）的消息落到两个 uid 的
``channel_*`` 线程，``agent_runs.uid`` 与会话归属一致；未绑定身份在
``require_binding`` 打开时被 409 拦截且不产生 Run。
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from e2e_helpers import postgres_dsn
from test.live_api_cleanup import delete_test_conversation_rows
from yuxi.storage.postgres.models_business import IMAccountBinding, IMBotConfig, User

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e, pytest.mark.slow]


@pytest_asyncio.fixture()
async def im_db():
    """自建真实 PostgreSQL 会话工厂，测试结束后释放连接池。"""
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _create_user(session, uid: str) -> User:
    """创建临时用户并返回。"""
    user = User(username=uid, uid=uid, password_hash="pytest")
    session.add(user)
    await session.commit()
    return user


async def _create_bot(
    session, *, platform: str, owner_uid: str, agent_slug: str, require_binding: bool
) -> IMBotConfig:
    """创建临时启用机器人配置并返回；身份解析不读取凭证，占位密文即可。"""
    bot = IMBotConfig(
        platform=platform,
        name=f"e2e-im-bot-{uuid.uuid4().hex[:8]}",
        enabled=True,
        credentials_encrypted="pytest-placeholder",
        owner_uid=owner_uid,
        agent_slug=agent_slug,
        require_binding=require_binding,
    )
    session.add(bot)
    await session.commit()
    return bot


def _channel_payload(*, account_id: str, chat_id: str, sender_id: str, agent_slug: str) -> dict:
    """构造一条指向指定机器人与发送者的纯文本 Channel 消息。"""
    return {
        "channel": "feishu",
        "account_id": account_id,
        "chat_id": chat_id,
        "sender_id": sender_id,
        "message_id": f"msg-{uuid.uuid4().hex}",
        "agent_slug": agent_slug,
        "message": {"type": "text", "text": "你好"},
    }


async def _assert_run_belongs_to(conn: asyncpg.Connection, run_id: str, thread_id: str, uid: str) -> None:
    """回读数据库，确认 Run 与会话都归属指定 uid，线程为 channel_* 派生。"""
    run = await conn.fetchrow("SELECT uid, conversation_thread_id FROM agent_runs WHERE id = $1", run_id)
    assert run is not None, f"agent_runs 行缺失: {run_id}"
    assert run["uid"] == uid, f"agent_runs.uid 不匹配: {run['uid']} != {uid}"
    assert run["conversation_thread_id"] == thread_id, run
    assert thread_id.startswith("channel_"), thread_id
    conversation = await conn.fetchrow("SELECT uid FROM conversations WHERE thread_id = $1", thread_id)
    assert conversation is not None, f"conversations 行缺失: {thread_id}"
    assert conversation["uid"] == uid, f"conversations.uid 不匹配: {conversation['uid']} != {uid}"


async def test_bound_senders_land_in_their_own_threads_and_unbound_is_rejected(
    e2e_client, e2e_headers, e2e_agent_context, im_db
):
    """已绑定 sender 各自落库到本人线程，未绑定身份在强绑开关下被拦。"""
    owner_uid = e2e_agent_context["uid"]
    agent_slug = e2e_agent_context["agent_slug"]

    run_token = uuid.uuid4().hex[:10]
    bound_a_uid = f"e2e-bound-a-{run_token}"
    bound_b_uid = f"e2e-bound-b-{run_token}"

    thread_ids: set[str] = set()
    bot_id: int | None = None
    enforce_bot_id: int | None = None

    try:
        async with im_db() as session:
            bound_a = await _create_user(session, bound_a_uid)
            bound_b = await _create_user(session, bound_b_uid)
            bot = await _create_bot(
                session, platform="feishu", owner_uid=owner_uid, agent_slug=agent_slug, require_binding=False
            )
            enforce_bot = await _create_bot(
                session, platform="feishu", owner_uid=owner_uid, agent_slug=agent_slug, require_binding=True
            )
            bot_id = bot.id
            enforce_bot_id = enforce_bot.id
            session.add(
                IMAccountBinding(user_id=bound_a.id, platform="feishu", platform_user_id="sender-a", bot_id=bot.id)
            )
            session.add(
                IMAccountBinding(user_id=bound_b.id, platform="feishu", platform_user_id="sender-b", bot_id=bot.id)
            )
            await session.commit()

        # 消息 A：sender-a 绑定 bound_a，落库到 bound_a 的 channel_* 线程。
        resp_a = await e2e_client.post(
            "/api/agent-invocation/channel/messages",
            json=_channel_payload(
                account_id=str(bot_id), chat_id=f"chat-a-{run_token}", sender_id="sender-a", agent_slug=agent_slug
            ),
            headers=e2e_headers,
        )
        assert resp_a.status_code == 200, resp_a.text
        payload_a = resp_a.json()
        assert payload_a["identity_source"] == "binding", payload_a
        thread_a = payload_a["thread_id"]
        run_a = payload_a["run_id"]
        assert run_a, payload_a
        thread_ids.add(thread_a)

        # 消息 B：sender-b 绑定 bound_b，落库到 bound_b 的独立线程。
        resp_b = await e2e_client.post(
            "/api/agent-invocation/channel/messages",
            json=_channel_payload(
                account_id=str(bot_id), chat_id=f"chat-b-{run_token}", sender_id="sender-b", agent_slug=agent_slug
            ),
            headers=e2e_headers,
        )
        assert resp_b.status_code == 200, resp_b.text
        payload_b = resp_b.json()
        assert payload_b["identity_source"] == "binding", payload_b
        thread_b = payload_b["thread_id"]
        run_b = payload_b["run_id"]
        assert run_b, payload_b
        assert thread_b != thread_a
        thread_ids.add(thread_b)

        conn = await asyncpg.connect(postgres_dsn())
        try:
            await _assert_run_belongs_to(conn, run_a, thread_a, bound_a_uid)
            await _assert_run_belongs_to(conn, run_b, thread_b, bound_b_uid)
        finally:
            await conn.close()

        # 未绑定身份在 require_binding 打开时被 409 拦截，不产生 Run。
        resp_reject = await e2e_client.post(
            "/api/agent-invocation/channel/messages",
            json=_channel_payload(
                account_id=str(enforce_bot_id),
                chat_id=f"chat-c-{run_token}",
                sender_id="unbound-sender",
                agent_slug=agent_slug,
            ),
            headers=e2e_headers,
        )
        assert resp_reject.status_code == 409, resp_reject.text
        assert resp_reject.json()["detail"]["code"] == "im_account_not_bound", resp_reject.text
    finally:
        if thread_ids:
            await delete_test_conversation_rows(thread_ids)
        bot_ids = [bid for bid in (bot_id, enforce_bot_id) if bid is not None]
        async with im_db() as session:
            if bot_ids:
                await session.execute(delete(IMBotConfig).where(IMBotConfig.id.in_(bot_ids)))
            await session.execute(delete(User).where(User.uid.in_([bound_a_uid, bound_b_uid])))
            await session.commit()
