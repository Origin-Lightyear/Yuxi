"""Channel 入口按 IM 绑定解析真实执行身份的路由级集成测试。

进程内直接调用 ``receive_channel_message``，用真实 PostgreSQL 验证
``resolve_channel_identity`` 的绑定命中、归属人回退、强绑拦截与机器人归属校验，
并断言每次成功响应都带 ``identity_source``。下游 ``submit_agent_request`` 以
monkeypatch 捕获，避免依赖 Redis / Tenant 服务（本槽位 Tenant 不可达）。
"""

from __future__ import annotations

import importlib
import os
import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.storage.postgres.models_business import IMAccountBinding, IMBotConfig, User

channel_router = importlib.import_module("server.routers.agent_invocation_channel_router")

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture()
async def binding_engine():
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


async def _create_bot(session, *, platform: str, owner_uid: str, require_binding: bool = False) -> IMBotConfig:
    """创建临时机器人配置并返回；身份解析不读取凭证，占位密文即可。"""
    bot = IMBotConfig(
        platform=platform,
        name=f"bot-{uuid.uuid4().hex[:8]}",
        enabled=True,
        credentials_encrypted="pytest-placeholder",
        owner_uid=owner_uid,
        agent_slug="default-chatbot",
        require_binding=require_binding,
    )
    session.add(bot)
    await session.commit()
    return bot


async def _cleanup(session, user_uids: list[str]) -> None:
    """删除测试机器人与用户；绑定随用户 CASCADE，机器人 bot_id 置空。"""
    await session.execute(delete(IMBotConfig).where(IMBotConfig.owner_uid.in_(user_uids)))
    await session.execute(delete(User).where(User.uid.in_(user_uids)))
    await session.commit()


def _payload(account_id: str, sender_id: str | None, *, text: str = "你好") -> channel_router.ChannelMessageRequest:
    """构造一条指向指定机器人与发送者的纯文本 Channel 消息。"""
    return channel_router.ChannelMessageRequest(
        channel="feishu",
        account_id=account_id,
        chat_id=f"chat-{uuid.uuid4().hex[:8]}",
        sender_id=sender_id,
        message_id=f"msg-{uuid.uuid4().hex[:8]}",
        agent_slug="default-chatbot",
        message={"type": "text", "text": text},
    )


async def test_bound_sender_resolves_to_bound_user(binding_engine, monkeypatch):
    """已绑定 sender 的消息以绑定用户身份提交，响应 identity_source=binding。"""
    uid = uuid.uuid4().hex[:10]
    owner_uid = f"owner-{uid}"
    bound_uid = f"bound-{uid}"
    captured: dict[str, str] = {}

    async def fake_submit_agent_request(*, request_input, current_user, **_kwargs):
        captured["current_uid"] = current_user.uid
        return {"run_id": "run-1", "thread_id": request_input.thread_id, "status": "dispatched"}

    monkeypatch.setattr(channel_router, "submit_agent_request", fake_submit_agent_request)

    try:
        async with binding_engine() as session:
            owner = await _create_user(session, owner_uid)
            bound = await _create_user(session, bound_uid)
            bot = await _create_bot(session, platform="feishu", owner_uid=owner.uid)
            session.add(
                IMAccountBinding(user_id=bound.id, platform="feishu", platform_user_id="sender-1", bot_id=bot.id)
            )
            await session.commit()

            result = await channel_router.receive_channel_message(
                _payload(str(bot.id), "sender-1"),
                current_user=owner,
                db=session,
            )
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [owner_uid, bound_uid])

    assert result["identity_source"] == "binding"
    assert captured["current_uid"] == bound.uid


async def test_unbound_sender_falls_back_to_owner(binding_engine, monkeypatch):
    """未绑定且 require_binding=false 时回退到归属人，identity_source=owner_fallback。"""
    uid = uuid.uuid4().hex[:10]
    owner_uid = f"owner-{uid}"
    captured: dict[str, str] = {}

    async def fake_submit_agent_request(*, request_input, current_user, **_kwargs):
        captured["current_uid"] = current_user.uid
        return {"run_id": "run-1", "thread_id": request_input.thread_id, "status": "dispatched"}

    monkeypatch.setattr(channel_router, "submit_agent_request", fake_submit_agent_request)

    try:
        async with binding_engine() as session:
            owner = await _create_user(session, owner_uid)
            bot = await _create_bot(session, platform="feishu", owner_uid=owner.uid, require_binding=False)

            result = await channel_router.receive_channel_message(
                _payload(str(bot.id), "unknown-sender"),
                current_user=owner,
                db=session,
            )
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [owner_uid])

    assert result["identity_source"] == "owner_fallback"
    assert captured["current_uid"] == owner.uid


async def test_unbound_sender_required_binding_rejects(binding_engine, monkeypatch):
    """未绑定且 require_binding=true 时返回 409，且不提交任何 run。"""
    uid = uuid.uuid4().hex[:10]
    owner_uid = f"owner-{uid}"
    calls: list[dict] = []

    async def fake_submit_agent_request(**kwargs):
        calls.append(kwargs)
        raise AssertionError("require_binding 拦截时不应进入提交服务")

    monkeypatch.setattr(channel_router, "submit_agent_request", fake_submit_agent_request)

    try:
        async with binding_engine() as session:
            owner = await _create_user(session, owner_uid)
            bot = await _create_bot(session, platform="feishu", owner_uid=owner.uid, require_binding=True)

            with pytest.raises(HTTPException) as exc_info:
                await channel_router.receive_channel_message(
                    _payload(str(bot.id), "unknown-sender"),
                    current_user=owner,
                    db=session,
                )
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [owner_uid])

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "im_account_not_bound"
    assert calls == []


async def test_bot_not_owned_by_transport_user_rejects(binding_engine):
    """transport_user 的 API Key 提交他人机器人时返回 403。"""
    uid = uuid.uuid4().hex[:10]
    owner_a_uid = f"owner-a-{uid}"
    owner_b_uid = f"owner-b-{uid}"

    try:
        async with binding_engine() as session:
            owner_a = await _create_user(session, owner_a_uid)
            owner_b = await _create_user(session, owner_b_uid)
            bot_b = await _create_bot(session, platform="feishu", owner_uid=owner_b.uid)

            with pytest.raises(HTTPException) as exc_info:
                await channel_router.receive_channel_message(
                    _payload(str(bot_b.id), "sender-1"),
                    current_user=owner_a,
                    db=session,
                )
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [owner_a_uid, owner_b_uid])

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "im_bot_not_owned"


async def test_state_command_returns_identity_source(binding_engine, monkeypatch):
    """slash command 走同一身份解析，响应同样带 identity_source。"""
    uid = uuid.uuid4().hex[:10]
    owner_uid = f"owner-{uid}"
    bound_uid = f"bound-{uid}"
    captured: dict[str, str] = {}

    async def fake_state(**kwargs):
        captured["state_uid"] = kwargs["current_user"].uid
        return {"agent_state": {"todos": []}}

    monkeypatch.setattr(channel_router, "get_agent_state_view", fake_state)

    try:
        async with binding_engine() as session:
            owner = await _create_user(session, owner_uid)
            bound = await _create_user(session, bound_uid)
            bot = await _create_bot(session, platform="feishu", owner_uid=owner.uid)
            session.add(
                IMAccountBinding(user_id=bound.id, platform="feishu", platform_user_id="sender-1", bot_id=bot.id)
            )
            await session.commit()

            result = await channel_router.receive_channel_message(
                _payload(str(bot.id), "sender-1", text="/state"),
                current_user=owner,
                db=session,
            )
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [owner_uid, bound_uid])

    assert result["command"] == "state"
    assert result["identity_source"] == "binding"
    assert captured["state_uid"] == bound.uid
