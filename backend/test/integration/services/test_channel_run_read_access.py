"""真实 PostgreSQL 上的 Channel Run 读权限回归测试。

网关只持有机器人归属人的 API Key，绑定用户的 Run 也必须能被归属人读取，
否则绑定后的 IM 主链路无法回传事件与产物。
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import yuxi.services.agent_run_service as agent_run_service
from yuxi.services.im_binding_service import (
    can_read_channel_run_as_bot_owner,
    can_read_channel_thread_as_bot_owner,
)
from yuxi.services.im_bot_service import encrypt_credentials
from yuxi.storage.postgres.models_business import AgentRun, Base, IMBotConfig, User

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_CREDENTIALS = {"client_id": "ding-client", "client_secret": "ding-secret"}


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    monkeypatch.setenv("IM_CREDENTIAL_ENCRYPTION_KEY", "im-binding-test-key")


@pytest_asyncio.fixture()
async def binding_engine():
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _create_user(session, uid: str) -> User:
    user = User(username=uid, uid=uid, password_hash="pytest")
    session.add(user)
    await session.commit()
    return user


async def _create_bot(session, *, owner_uid: str, enabled: bool = True) -> IMBotConfig:
    bot = IMBotConfig(
        platform="dingtalk",
        name=f"bot-{uuid.uuid4().hex[:8]}",
        enabled=enabled,
        credentials_encrypted=encrypt_credentials("dingtalk", _CREDENTIALS),
        owner_uid=owner_uid,
        agent_slug="default-chatbot",
    )
    session.add(bot)
    await session.commit()
    return bot


def _run(*, uid: str, thread_id: str, source: str, account_id: str | None) -> AgentRun:
    metadata = {"account_id": account_id} if account_id else {}
    return AgentRun(
        id=uuid.uuid4().hex,
        conversation_thread_id=thread_id,
        runtime_scope_id=uuid.uuid4().hex,
        agent_slug="default-chatbot",
        uid=uid,
        status="completed",
        request_id=uuid.uuid4().hex,
        source=source,
        channel="dingtalk" if source == "channel" else "web",
        origin_metadata=metadata,
    )


async def _cleanup(session, *, owner_uids: list[str], uids: list[str]) -> None:
    await session.execute(delete(AgentRun).where(AgentRun.uid.in_(uids)))
    await session.execute(delete(IMBotConfig).where(IMBotConfig.owner_uid.in_(owner_uids)))
    await session.execute(delete(User).where(User.uid.in_(owner_uids + uids)))
    await session.commit()


async def test_channel_run_is_readable_by_bot_owner_and_thread_artifact_resolves(binding_engine, monkeypatch):
    """绑定用户的 Channel Run 可由机器人归属人读取，无关用户仍不可读。"""
    suffix = uuid.uuid4().hex[:10]
    owner_uid = f"owner-{suffix}"
    bound_uid = f"bound-{suffix}"
    stranger_uid = f"stranger-{suffix}"
    thread_id = f"channel_{uuid.uuid4().hex}"
    try:
        async with binding_engine() as session:
            await _create_user(session, owner_uid)
            await _create_user(session, bound_uid)
            await _create_user(session, stranger_uid)
            bot = await _create_bot(session, owner_uid=owner_uid)
            channel_run = _run(uid=bound_uid, thread_id=thread_id, source="channel", account_id=str(bot.id))
            chat_run = _run(uid=bound_uid, thread_id=f"chat_{uuid.uuid4().hex}", source="chat", account_id=None)
            session.add_all([channel_run, chat_run])
            await session.commit()

            assert await can_read_channel_run_as_bot_owner(session, run=channel_run, caller_uid=owner_uid) is True
            assert await can_read_channel_run_as_bot_owner(session, run=channel_run, caller_uid=stranger_uid) is False
            assert await can_read_channel_run_as_bot_owner(session, run=chat_run, caller_uid=owner_uid) is False
            assert await can_read_channel_thread_as_bot_owner(
                session, thread_id=thread_id, caller_uid=owner_uid
            ) is True
            assert await can_read_channel_thread_as_bot_owner(
                session, thread_id=thread_id, caller_uid=stranger_uid
            ) is False

        @asynccontextmanager
        async def fake_session_context():
            async with binding_engine() as session:
                yield session

        monkeypatch.setattr(agent_run_service.pg_manager, "get_async_session_context", fake_session_context)
        assert await agent_run_service._load_stream_run_for_user(channel_run.id, owner_uid) is not None
        assert await agent_run_service._load_stream_run_for_user(channel_run.id, stranger_uid) is None
    finally:
        async with binding_engine() as session:
            await _cleanup(session, owner_uids=[owner_uid], uids=[bound_uid, stranger_uid])


async def test_disabled_bot_does_not_grant_run_read(binding_engine):
    """机器人停用后不再授予 Channel Run 读取权限。"""
    suffix = uuid.uuid4().hex[:10]
    owner_uid = f"owner-{suffix}"
    bound_uid = f"bound-{suffix}"
    try:
        async with binding_engine() as session:
            await _create_user(session, owner_uid)
            await _create_user(session, bound_uid)
            bot = await _create_bot(session, owner_uid=owner_uid, enabled=False)
            run = _run(uid=bound_uid, thread_id=f"channel_{uuid.uuid4().hex}", source="channel", account_id=str(bot.id))
            session.add(run)
            await session.commit()

            assert await can_read_channel_run_as_bot_owner(session, run=run, caller_uid=owner_uid) is False
    finally:
        async with binding_engine() as session:
            await _cleanup(session, owner_uids=[owner_uid], uids=[bound_uid])
