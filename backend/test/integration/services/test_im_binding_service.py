"""真实 PostgreSQL 上的 IM 绑定会话与身份解析服务测试。"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import yuxi.services.im_binding_service as im_binding_service
from yuxi.services.im_binding_service import (
    _hash_state,
    complete_binding,
    create_binding_session,
    resolve_channel_identity,
)
from yuxi.services.im_bot_service import encrypt_credentials
from yuxi.storage.postgres.models_business import (
    Base,
    IMAccountBinding,
    IMBindingSession,
    IMBotConfig,
    User,
)
from yuxi.utils.datetime_utils import utc_now_naive

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture()
async def binding_engine():
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "ALTER TABLE IF EXISTS im_bot_configs "
                "ADD COLUMN IF NOT EXISTS require_binding BOOLEAN NOT NULL DEFAULT false"
            )
        )
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    monkeypatch.setenv("IM_CREDENTIAL_ENCRYPTION_KEY", "im-binding-test-key")


def _credentials(platform: str) -> dict:
    if platform == "dingtalk":
        return {"client_id": "ding-client", "client_secret": "ding-secret"}
    return {"app_id": "fei-app", "app_secret": "fei-secret"}


async def _create_user(session, uid: str) -> User:
    """创建临时用户并返回。"""
    user = User(username=uid, uid=uid, password_hash="pytest")
    session.add(user)
    await session.commit()
    return user


async def _create_bot(
    session, *, platform: str, owner_uid: str, enabled: bool = True, require_binding: bool = False
) -> IMBotConfig:
    """创建临时机器人配置并返回。"""
    bot = IMBotConfig(
        platform=platform,
        name=f"bot-{uuid.uuid4().hex[:8]}",
        enabled=enabled,
        credentials_encrypted=encrypt_credentials(platform, _credentials(platform)),
        owner_uid=owner_uid,
        agent_slug="default-chatbot",
        require_binding=require_binding,
    )
    session.add(bot)
    await session.commit()
    return bot


async def _cleanup(session, user_uids: list[str]) -> None:
    """删除测试机器人与用户，确保失败的用例不残留已提交行。"""
    await session.execute(delete(IMBotConfig).where(IMBotConfig.owner_uid.in_(user_uids)))
    await session.execute(delete(User).where(User.uid.in_(user_uids)))
    await session.commit()


async def _add_session(session, *, user_id: int, bot_id: int, state: str, expires_at) -> str:
    """插入一个 pending 会话并提交，返回其 state_hash。"""
    session.add(
        IMBindingSession(
            user_id=user_id,
            platform="dingtalk",
            bot_id=bot_id,
            state_hash=_hash_state(state),
            status="pending",
            expires_at=expires_at,
        )
    )
    await session.commit()
    return state


async def test_create_binding_session_rejects_ambiguous_platform(binding_engine):
    """同一平台存在多个启用机器人时必须拒绝，不能静默挑选凭据。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            with pytest.raises(ValueError, match="同一平台只能有一个启用的机器人"):
                await create_binding_session(session, user=user, platform="dingtalk", base_url="https://yuxi.example")
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])


async def test_complete_binding_consumes_state_once(binding_engine, monkeypatch):
    """state 只能消费一次：首次写入绑定，重放抛 400。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            state = f"state-{uuid.uuid4().hex}"
            await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=state,
                expires_at=utc_now_naive() + timedelta(seconds=600),
            )

            async def fake_exchange(platform, credentials, code, redirect_uri):
                return "dingtalk-userid-1", "union-1"

            monkeypatch.setattr(im_binding_service, "_exchange_code", fake_exchange)

            result_path = await complete_binding(session, platform="dingtalk", code="code", state=state)
            await session.commit()
            assert result_path == "/"

            with pytest.raises(HTTPException) as exc_info:
                await complete_binding(session, platform="dingtalk", code="code", state=state)
            assert exc_info.value.status_code == 400
            assert exc_info.value.detail["code"] == "invalid_binding_state"

            binding = (
                await session.execute(
                    select(IMAccountBinding).where(
                        IMAccountBinding.user_id == user.id,
                        IMAccountBinding.platform == "dingtalk",
                    )
                )
            ).scalar_one_or_none()
            assert binding is not None
            assert binding.platform_user_id == "dingtalk-userid-1"
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])


async def test_complete_binding_rejects_identity_owned_by_other_user(binding_engine, monkeypatch):
    """平台身份已被他人绑定时必须拒绝，不能覆盖既有绑定。"""
    uid = uuid.uuid4().hex[:10]
    user_a_uid = f"bind-a-{uid}"
    user_b_uid = f"bind-b-{uid}"
    try:
        async with binding_engine() as session:
            user_a = await _create_user(session, user_a_uid)
            user_b = await _create_user(session, user_b_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user_a.uid)
            session.add(IMAccountBinding(user_id=user_a.id, platform="dingtalk", platform_user_id="taken-id"))
            await session.commit()
            state = f"state-{uuid.uuid4().hex}"
            await _add_session(
                session,
                user_id=user_b.id,
                bot_id=bot.id,
                state=state,
                expires_at=utc_now_naive() + timedelta(seconds=600),
            )

            async def fake_exchange(platform, credentials, code, redirect_uri):
                return "taken-id", "union-1"

            monkeypatch.setattr(im_binding_service, "_exchange_code", fake_exchange)

            with pytest.raises(HTTPException) as exc_info:
                await complete_binding(session, platform="dingtalk", code="code", state=state)
            assert exc_info.value.status_code == 409
            assert exc_info.value.detail["code"] == "im_account_already_bound"
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_a_uid, user_b_uid])


async def test_complete_binding_rejects_expired_state(binding_engine, monkeypatch):
    """过期 state 必须拒绝且不写绑定。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            state = f"state-{uuid.uuid4().hex}"
            await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=state,
                expires_at=utc_now_naive() - timedelta(seconds=1),
            )

            async def fake_exchange(platform, credentials, code, redirect_uri):
                raise AssertionError("过期 state 不应触达平台换取")

            monkeypatch.setattr(im_binding_service, "_exchange_code", fake_exchange)

            with pytest.raises(HTTPException) as exc_info:
                await complete_binding(session, platform="dingtalk", code="code", state=state)
            assert exc_info.value.status_code == 400
            assert exc_info.value.detail["code"] == "invalid_binding_state"

            binding = (
                await session.execute(select(IMAccountBinding).where(IMAccountBinding.user_id == user.id))
            ).scalar_one_or_none()
            assert binding is None
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])


async def test_complete_binding_rejects_platform_mismatch(binding_engine, monkeypatch):
    """state 平台与回调平台不一致时须拒绝，且不触碰平台换取。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            state = f"state-{uuid.uuid4().hex}"
            await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=state,
                expires_at=utc_now_naive() + timedelta(seconds=600),
            )

            async def no_exchange(platform, credentials, code, redirect_uri):
                raise AssertionError("平台不匹配时不应触达换取")

            monkeypatch.setattr(im_binding_service, "_exchange_code", no_exchange)

            with pytest.raises(HTTPException) as exc_info:
                await complete_binding(session, platform="feishu", code="code", state=state)
            assert exc_info.value.status_code == 400
            assert exc_info.value.detail["code"] == "invalid_binding_state"
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])


async def test_complete_binding_rejects_rebinding_same_platform(binding_engine, monkeypatch):
    """同一用户同平台已有绑定时返回 409，不触发唯一约束 IntegrityError。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            session.add(IMAccountBinding(user_id=user.id, platform="dingtalk", platform_user_id="already-bound"))
            await session.commit()
            state = f"state-{uuid.uuid4().hex}"
            await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=state,
                expires_at=utc_now_naive() + timedelta(seconds=600),
            )

            async def no_exchange(platform, credentials, code, redirect_uri):
                raise AssertionError("已绑定同平台时不应触达换取")

            monkeypatch.setattr(im_binding_service, "_exchange_code", no_exchange)

            with pytest.raises(HTTPException) as exc_info:
                await complete_binding(session, platform="dingtalk", code="code", state=state)
            assert exc_info.value.status_code == 409
            assert exc_info.value.detail["code"] == "im_account_already_bound"
            assert exc_info.value.detail["message"] == "该平台已绑定，请先解绑"

            bindings = (
                await session.execute(select(IMAccountBinding).where(IMAccountBinding.user_id == user.id))
            ).scalars().all()
            assert len(bindings) == 1
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])


async def test_complete_binding_commits_used_state_before_exchange(binding_engine, monkeypatch):
    """换取失败也必须把 state 置 used 并提交，使重放不能复用。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            state = f"state-{uuid.uuid4().hex}"
            await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=state,
                expires_at=utc_now_naive() + timedelta(seconds=600),
            )

            async def failing_exchange(platform, credentials, code, redirect_uri):
                raise ValueError("平台换取失败")

            monkeypatch.setattr(im_binding_service, "_exchange_code", failing_exchange)

            with pytest.raises(ValueError, match="平台换取失败"):
                await complete_binding(session, platform="dingtalk", code="code", state=state)

            # 用独立会话确认 used_at 已提交，排除未提交的脏读。
            async with binding_engine() as verify:
                row = (
                    await verify.execute(
                        select(IMBindingSession).where(IMBindingSession.state_hash == _hash_state(state))
                    )
                ).scalar_one()
                assert row.status == "used"
                assert row.used_at is not None
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])


async def test_dingtalk_token_exchange_error_redacts_query_secrets(monkeypatch):
    """钉钉换取失败时异常消息不得包含 appsecret / access_token 等 query 凭据。"""
    secret = "super-secret-appsecret-value"
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(401, json={"errcode": 40001, "errmsg": "invalid appsecret"}, request=request)

    class _MockClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(im_binding_service.httpx, "AsyncClient", _MockClient)

    with pytest.raises(ValueError) as exc_info:
        await im_binding_service._dingtalk_enterprise_access_token(
            {"client_id": "appkey-1", "client_secret": secret}
        )

    assert captured, "请求应已发出"
    assert secret in captured[0], "换取应把 client_secret 作为 appsecret query 参数发出"
    message = str(exc_info.value)
    assert secret not in message
    assert "appsecret" not in message
    assert "access_token" not in message


async def test_resolve_channel_identity_falls_back_to_owner_when_not_required(binding_engine):
    """未绑定时按 require_binding 分流：关闭回退归属人，打开抛 409。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            transport_user = await _create_user(session, user_uid)
            optional_bot = await _create_bot(
                session, platform="dingtalk", owner_uid=transport_user.uid, require_binding=False
            )
            identity = await resolve_channel_identity(
                session, transport_user=transport_user, account_id=str(optional_bot.id), sender_id="unknown-sender"
            )
            assert identity.user.id == transport_user.id
            assert identity.owner_fallback is True

            required_bot = await _create_bot(
                session, platform="dingtalk", owner_uid=transport_user.uid, require_binding=True
            )
            with pytest.raises(HTTPException) as exc_info:
                await resolve_channel_identity(
                    session, transport_user=transport_user, account_id=str(required_bot.id), sender_id="unknown-sender"
                )
            assert exc_info.value.status_code == 409
            assert exc_info.value.detail["code"] == "im_account_not_bound"
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
