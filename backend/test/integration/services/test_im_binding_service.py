"""真实 PostgreSQL + Redis 上的 IM 绑定服务测试（Tenant 唯一保存方）。"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import yuxi.services.im_binding_service as im_binding_service
from yuxi.config import config as app_config
from yuxi.services.im_binding_cache import bind_uid, forget_user_bindings, remember_binding
from yuxi.services.im_binding_service import (
    _hash_state,
    complete_binding,
    create_binding_session,
    delete_binding,
    list_bindings,
)
from yuxi.services.im_bot_service import encrypt_credentials
from yuxi.services.saas_client import SaasAPIError
from yuxi.services.saas_session import (
    SaasAgentSession,
    clear_saas_agent_session,
    get_saas_agent_session,
    save_saas_agent_session,
)
from yuxi.storage.postgres.models_business import Base, IMBindingSession, IMBotConfig, User
from yuxi.storage.redis import close_async_redis_client, get_async_redis_client
from yuxi.utils.datetime_utils import utc_now_naive

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture(autouse=True)
async def _isolated_redis_client():
    """把共享 Redis 客户端绑定到当前用例的事件循环。"""
    await close_async_redis_client()
    await get_async_redis_client()
    yield
    await close_async_redis_client()


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


@pytest.fixture(autouse=True)
def _enable_binding(monkeypatch):
    """绑定接口只在 SaaS 部署开放，测试显式打开开关。"""
    monkeypatch.setattr(app_config, "saas_enabled", True)
    monkeypatch.setattr(app_config, "platform_service_url", "https://tenant.example.com")


class _FakeSaasClient:
    """记录绑定服务对 Tenant 的调用，按用例注入返回值或异常。"""

    def __init__(self) -> None:
        self.bound: list[tuple[str, str, str]] = []
        self.unbound: list[tuple[str, str]] = []
        self.bind_error: Exception | None = None
        self.unbind_error: Exception | None = None
        self.list_error: Exception | None = None
        self.bindings: list[dict] = []

    async def bind_channel_account(self, token: str, channel_type: str, channel_user_id: str) -> dict:
        self.bound.append((token, channel_type, channel_user_id))
        if self.bind_error is not None:
            raise self.bind_error
        return {"channelType": channel_type, "bound": True, "channelUserId": channel_user_id}

    async def unbind_channel_account(self, token: str, channel_type: str) -> None:
        self.unbound.append((token, channel_type))
        if self.unbind_error is not None:
            raise self.unbind_error

    async def list_channel_bindings(self, token: str) -> list[dict]:
        del token
        if self.list_error is not None:
            raise self.list_error
        return self.bindings


@pytest.fixture()
def fake_saas(monkeypatch) -> _FakeSaasClient:
    client = _FakeSaasClient()
    monkeypatch.setattr(im_binding_service, "get_saas_client", lambda: client)
    return client


def _credentials(platform: str) -> dict:
    if platform == "dingtalk":
        return {"client_id": "ding-client", "client_secret": "ding-secret"}
    return {"app_id": "fei-app", "app_secret": "fei-secret"}


async def _create_user(session, uid: str) -> User:
    """创建临时用户并返回；手机号用于平台账号归属核验，按 uid 独立生成。"""
    user = User(username=uid, uid=uid, password_hash="pytest", phone_number=f"138{uuid.uuid4().int % 100000000:08d}")
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
    """插入一个 pending 会话并提交，返回其 state。"""
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


async def _seed_agent_session(uid: str, mobile: str | None = "13800138000") -> None:
    """写入与 Tenant AgentSession 对齐的 Redis 会话，``mobile`` 是归属核验基准。"""
    await save_saas_agent_session(
        uid,
        SaasAgentSession(
            token="tenant-session-1",
            expires_at=str(int(time.time()) + 600),
            tenant_id=1,
            employee_id=2,
            mcp_instance_id="runtime-1",
            mobile=mobile,
        ),
    )


async def _clear_user_state(uid: str) -> None:
    """清理 Redis 会话与绑定缓存。"""
    await clear_saas_agent_session(uid)
    await forget_user_bindings(uid)


async def test_create_binding_session_rejects_when_binding_disabled(binding_engine, monkeypatch):
    """非 SaaS 部署不启用绑定，发起会话直接返回 400。"""
    monkeypatch.setattr(app_config, "saas_enabled", False)
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            with pytest.raises(HTTPException) as exc_info:
                await create_binding_session(session, user=user, platform="dingtalk", base_url="https://yuxi.example")
            assert exc_info.value.status_code == 400
            assert exc_info.value.detail["code"] == "im_binding_not_enabled"
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])


async def test_create_binding_session_requires_login_session(binding_engine):
    """没有 Tenant AgentSession 时返回 401，且不写入 pending state。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            with pytest.raises(HTTPException) as exc_info:
                await create_binding_session(session, user=user, platform="dingtalk", base_url="https://yuxi.example")
            assert exc_info.value.status_code == 401
            assert exc_info.value.detail["code"] == "im_binding_login_required"
            rows = await session.execute(select(IMBindingSession).where(IMBindingSession.user_id == user.id))
            assert rows.scalars().all() == []
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])


async def test_create_binding_session_requires_session_mobile(binding_engine):
    """旧会话没有 Tenant 手机号时提前 401，不生成必然失败的二维码与 pending state。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
        await _seed_agent_session(user_uid, None)

        async with binding_engine() as session:
            with pytest.raises(HTTPException) as exc_info:
                await create_binding_session(session, user=user, platform="dingtalk", base_url="https://yuxi.example")
            assert exc_info.value.status_code == 401
            assert exc_info.value.detail["code"] == "im_binding_login_required"
            rows = await session.execute(select(IMBindingSession).where(IMBindingSession.user_id == user.id))
            assert rows.scalars().all() == []
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)


async def test_create_binding_session_rejects_ambiguous_platform(binding_engine):
    """同一平台存在多个启用机器人时必须拒绝，不能静默挑选凭据。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            await _seed_agent_session(user.uid, user.phone_number)
            with pytest.raises(ValueError, match="同一平台只能有一个启用的机器人"):
                await create_binding_session(session, user=user, platform="dingtalk", base_url="https://yuxi.example")
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)


async def test_complete_binding_saves_to_tenant_and_caches(binding_engine, monkeypatch, fake_saas):
    """绑定成功后 Tenant 收到大写平台类型，Redis 写入可复用的身份映射。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            state = await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=f"state-{uuid.uuid4().hex}",
                expires_at=utc_now_naive() + timedelta(seconds=600),
            )
            await _seed_agent_session(user.uid, user.phone_number)

            async def fake_exchange(platform, credentials, code, redirect_uri):
                return "dingtalk-userid-1", "union-1", user.phone_number

            monkeypatch.setattr(im_binding_service, "_exchange_code", fake_exchange)

            result_path = await complete_binding(session, platform="dingtalk", code="code", state=state)

            assert result_path == "/"
            assert fake_saas.bound == [("tenant-session-1", "DINGTALK", "dingtalk-userid-1")]
            assert await bind_uid("dingtalk", "dingtalk-userid-1") == user.uid
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)


async def test_complete_binding_consumes_state_once(binding_engine, monkeypatch, fake_saas):
    """state 只能消费一次：首次成功，重放抛 400。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            state = await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=f"state-{uuid.uuid4().hex}",
                expires_at=utc_now_naive() + timedelta(seconds=600),
            )
            await _seed_agent_session(user.uid, user.phone_number)

            async def fake_exchange(platform, credentials, code, redirect_uri):
                return "dingtalk-userid-1", "union-1", user.phone_number

            monkeypatch.setattr(im_binding_service, "_exchange_code", fake_exchange)

            await complete_binding(session, platform="dingtalk", code="code", state=state)

            with pytest.raises(HTTPException) as exc_info:
                await complete_binding(session, platform="dingtalk", code="code", state=state)
            assert exc_info.value.status_code == 400
            assert exc_info.value.detail["code"] == "invalid_binding_state"
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)


async def test_complete_binding_concurrent_callbacks_only_one_succeeds(binding_engine, monkeypatch, fake_saas):
    """同一 state 的并发回调最多一个成功，Tenant 只收到一次绑定。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            state = await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=f"state-{uuid.uuid4().hex}",
                expires_at=utc_now_naive() + timedelta(seconds=600),
            )
        await _seed_agent_session(user_uid, user.phone_number)

        exchanged_codes: list[str] = []

        async def fake_exchange(platform, credentials, code, redirect_uri):
            exchanged_codes.append(code)
            return "dingtalk-userid-1", "union-1", user.phone_number

        monkeypatch.setattr(im_binding_service, "_exchange_code", fake_exchange)

        async def attempt(code: str):
            async with binding_engine() as session:
                return await complete_binding(session, platform="dingtalk", code=code, state=state)

        results = await asyncio.gather(
            attempt("code-a"),
            attempt("code-b"),
            return_exceptions=True,
        )

        assert [result for result in results if result == "/"] == ["/"]
        failures = [result for result in results if isinstance(result, HTTPException)]
        assert len(failures) == 1
        assert failures[0].status_code == 400
        assert failures[0].detail["code"] == "invalid_binding_state"
        assert len(exchanged_codes) == 1
        assert fake_saas.bound == [("tenant-session-1", "DINGTALK", "dingtalk-userid-1")]
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)


async def test_complete_binding_rejects_other_persons_platform_account(binding_engine, monkeypatch, fake_saas):
    """他人平台账号扫码授权不能绑到当前 Yuxi 账号，绑定前必须核验手机号。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            state = await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=f"state-{uuid.uuid4().hex}",
                expires_at=utc_now_naive() + timedelta(seconds=600),
            )
            await _seed_agent_session(user.uid, user.phone_number)

            async def fake_exchange(platform, credentials, code, redirect_uri):
                return "dingtalk-userid-1", "union-1", "13900000000"

            monkeypatch.setattr(im_binding_service, "_exchange_code", fake_exchange)

            with pytest.raises(HTTPException) as exc_info:
                await complete_binding(session, platform="dingtalk", code="code", state=state)
            assert exc_info.value.status_code == 403
            assert exc_info.value.detail["code"] == "im_binding_identity_mismatch"
            assert fake_saas.bound == []
            assert await bind_uid("dingtalk", "dingtalk-userid-1") is None
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)


async def test_complete_binding_ignores_user_editable_phone(binding_engine, monkeypatch, fake_saas):
    """本地 phone_number 可被用户改写，归属核验必须以 Tenant 会话手机号为准。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    victim_mobile = f"139{int(uid, 16) % 100000000:08d}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            state = await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=f"state-{uuid.uuid4().hex}",
                expires_at=utc_now_naive() + timedelta(seconds=600),
            )
            session_mobile = user.phone_number
        await _seed_agent_session(user_uid, session_mobile)

        # 用户把自己的本地手机号改成受害者号码，但 Tenant 会话里的手机号不变。
        async with binding_engine() as session:
            stored = (await session.execute(select(User).where(User.uid == user_uid))).scalar_one()
            stored.phone_number = victim_mobile
            await session.commit()

        async def fake_exchange(platform, credentials, code, redirect_uri):
            return "dingtalk-userid-1", "union-1", victim_mobile

        monkeypatch.setattr(im_binding_service, "_exchange_code", fake_exchange)

        async with binding_engine() as session:
            with pytest.raises(HTTPException) as exc_info:
                await complete_binding(session, platform="dingtalk", code="code", state=state)

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["code"] == "im_binding_identity_mismatch"
        assert fake_saas.bound == []
        assert await bind_uid("dingtalk", "dingtalk-userid-1") is None
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)


async def test_complete_binding_requires_session_mobile(binding_engine, monkeypatch, fake_saas):
    """旧会话没有 Tenant 下发手机号时要求重新登录，不能跳过归属核验。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            state = await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=f"state-{uuid.uuid4().hex}",
                expires_at=utc_now_naive() + timedelta(seconds=600),
            )
        await _seed_agent_session(user_uid, None)

        async def no_exchange(*_args, **_kwargs):
            raise AssertionError("缺少会话手机号时不应触达平台换取")

        monkeypatch.setattr(im_binding_service, "_exchange_code", no_exchange)

        async with binding_engine() as session:
            with pytest.raises(HTTPException) as exc_info:
                await complete_binding(session, platform="dingtalk", code="code", state=state)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["code"] == "im_binding_login_required"
        assert fake_saas.bound == []
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)


async def test_complete_binding_ttl_failure_happens_before_tenant(binding_engine, monkeypatch, fake_saas):
    """缓存 TTL 解析失败必须在调用 Tenant 前暴露，避免已绑定却显示失败。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            state = await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=f"state-{uuid.uuid4().hex}",
                expires_at=utc_now_naive() + timedelta(seconds=600),
            )
            await _seed_agent_session(user.uid, user.phone_number)

            async def fake_exchange(platform, credentials, code, redirect_uri):
                return "dingtalk-userid-1", "union-1", user.phone_number

            def failing_ttl(expires_at):
                raise ValueError(f"bad expiry: {expires_at}")

            monkeypatch.setattr(im_binding_service, "_exchange_code", fake_exchange)
            monkeypatch.setattr(im_binding_service, "get_saas_session_ttl_seconds", failing_ttl)

            with pytest.raises(ValueError, match="bad expiry"):
                await complete_binding(session, platform="dingtalk", code="code", state=state)
            assert fake_saas.bound == []
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)


async def test_complete_binding_requires_login_session(binding_engine, monkeypatch, fake_saas):
    """没有 Tenant AgentSession 时拒绝绑定，且不触达平台换取。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            state = await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=f"state-{uuid.uuid4().hex}",
                expires_at=utc_now_naive() + timedelta(seconds=600),
            )

            async def no_exchange(platform, credentials, code, redirect_uri):
                raise AssertionError("缺少登录会话时不应触达平台换取")

            monkeypatch.setattr(im_binding_service, "_exchange_code", no_exchange)

            with pytest.raises(HTTPException) as exc_info:
                await complete_binding(session, platform="dingtalk", code="code", state=state)
            assert exc_info.value.status_code == 401
            assert exc_info.value.detail["code"] == "im_binding_login_required"
            assert fake_saas.bound == []
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)


async def test_complete_binding_rejects_expired_state(binding_engine, monkeypatch):
    """过期 state 必须拒绝且不写绑定。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            state = await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=f"state-{uuid.uuid4().hex}",
                expires_at=utc_now_naive() - timedelta(seconds=1),
            )

            async def no_exchange(platform, credentials, code, redirect_uri):
                raise AssertionError("过期 state 不应触达平台换取")

            monkeypatch.setattr(im_binding_service, "_exchange_code", no_exchange)

            with pytest.raises(HTTPException) as exc_info:
                await complete_binding(session, platform="dingtalk", code="code", state=state)
            assert exc_info.value.status_code == 400
            assert exc_info.value.detail["code"] == "invalid_binding_state"

            row = (
                await session.execute(select(IMBindingSession).where(IMBindingSession.state_hash == _hash_state(state)))
            ).scalar_one()
            assert row.status == "pending"
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
            state = await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=f"state-{uuid.uuid4().hex}",
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


async def test_complete_binding_commits_used_state_before_exchange(binding_engine, monkeypatch, fake_saas):
    """换取失败也必须把 state 置 used 并提交，使重放不能复用。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            state = await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=f"state-{uuid.uuid4().hex}",
                expires_at=utc_now_naive() + timedelta(seconds=600),
            )
            await _seed_agent_session(user.uid, user.phone_number)

            async def failing_exchange(platform, credentials, code, redirect_uri):
                raise ValueError("平台换取失败")

            monkeypatch.setattr(im_binding_service, "_exchange_code", failing_exchange)

            with pytest.raises(ValueError, match="平台换取失败"):
                await complete_binding(session, platform="dingtalk", code="code", state=state)

            async with binding_engine() as verify:
                row = (
                    await verify.execute(
                        select(IMBindingSession).where(IMBindingSession.state_hash == _hash_state(state))
                    )
                ).scalar_one()
                assert row.status == "used"
                assert row.used_at is not None
            assert fake_saas.bound == []
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)


async def test_complete_binding_maps_tenant_conflict(binding_engine, monkeypatch, fake_saas):
    """Tenant 20316 映射为 409，用户需要先解绑占用账号。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            state = await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=f"state-{uuid.uuid4().hex}",
                expires_at=utc_now_naive() + timedelta(seconds=600),
            )
            await _seed_agent_session(user.uid, user.phone_number)
            fake_saas.bind_error = SaasAPIError(code=20316, msg="该平台账号已绑定其他员工，请先解绑")

            async def fake_exchange(platform, credentials, code, redirect_uri):
                return "dingtalk-userid-1", "union-1", user.phone_number

            monkeypatch.setattr(im_binding_service, "_exchange_code", fake_exchange)

            with pytest.raises(HTTPException) as exc_info:
                await complete_binding(session, platform="dingtalk", code="code", state=state)
            assert exc_info.value.status_code == 409
            assert exc_info.value.detail["code"] == "im_account_already_bound"
            assert await bind_uid("dingtalk", "dingtalk-userid-1") is None
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)


async def test_complete_binding_clears_expired_tenant_session(binding_engine, monkeypatch, fake_saas):
    """Tenant 20314 表示会话失效：清理 Redis 会话与绑定缓存并要求重新登录。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            state = await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=f"state-{uuid.uuid4().hex}",
                expires_at=utc_now_naive() + timedelta(seconds=600),
            )
            await _seed_agent_session(user.uid, user.phone_number)
            await remember_binding(uid=user.uid, platform="dingtalk", channel_user_id="old-id", ttl_seconds=600)
            fake_saas.bind_error = SaasAPIError(code=20314, msg="Agent登录状态已失效，请重新登录")

            async def fake_exchange(platform, credentials, code, redirect_uri):
                return "dingtalk-userid-1", "union-1", user.phone_number

            monkeypatch.setattr(im_binding_service, "_exchange_code", fake_exchange)

            with pytest.raises(HTTPException) as exc_info:
                await complete_binding(session, platform="dingtalk", code="code", state=state)
            assert exc_info.value.status_code == 401
            assert exc_info.value.detail["code"] == "im_binding_login_required"
            assert await get_saas_agent_session(user.uid) is None
            assert await bind_uid("dingtalk", "old-id") is None
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)


async def test_20314_cache_clear_failure_still_returns_401(binding_engine, monkeypatch, fake_saas):
    """20314 清理缓存失败时仍返回 401，不能把重新登录要求变成 500。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            state = await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=f"state-{uuid.uuid4().hex}",
                expires_at=utc_now_naive() + timedelta(seconds=600),
            )
            await _seed_agent_session(user.uid, user.phone_number)
            fake_saas.bind_error = SaasAPIError(code=20314, msg="Agent登录状态已失效，请重新登录")

            async def fake_exchange(platform, credentials, code, redirect_uri):
                return "dingtalk-userid-1", "union-1", user.phone_number

            async def failing_forget(uid):
                del uid
                raise RuntimeError("redis down")

            monkeypatch.setattr(im_binding_service, "_exchange_code", fake_exchange)
            monkeypatch.setattr(im_binding_service.im_binding_cache, "forget_user_bindings", failing_forget)

            with pytest.raises(HTTPException) as exc_info:
                await complete_binding(session, platform="dingtalk", code="code", state=state)
            assert exc_info.value.status_code == 401
            assert exc_info.value.detail["code"] == "im_binding_login_required"
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)


async def test_complete_binding_maps_tenant_network_failure(binding_engine, monkeypatch, fake_saas):
    """Tenant 不可达时绑定失败关闭，不返回成功。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            bot = await _create_bot(session, platform="dingtalk", owner_uid=user.uid)
            state = await _add_session(
                session,
                user_id=user.id,
                bot_id=bot.id,
                state=f"state-{uuid.uuid4().hex}",
                expires_at=utc_now_naive() + timedelta(seconds=600),
            )
            await _seed_agent_session(user.uid, user.phone_number)
            fake_saas.bind_error = httpx.ConnectError("tenant down")

            async def fake_exchange(platform, credentials, code, redirect_uri):
                return "dingtalk-userid-1", "union-1", user.phone_number

            monkeypatch.setattr(im_binding_service, "_exchange_code", fake_exchange)

            with pytest.raises(HTTPException) as exc_info:
                await complete_binding(session, platform="dingtalk", code="code", state=state)
            assert exc_info.value.status_code == 502
            assert exc_info.value.detail["code"] == "im_binding_tenant_unavailable"
            assert await bind_uid("dingtalk", "dingtalk-userid-1") is None
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)


async def test_list_bindings_maps_tenant_payload(binding_engine, fake_saas):
    """列表只映射 Tenant 已绑定的飞书/钉钉，未绑定与企业微信不出现。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            await _seed_agent_session(user.uid, user.phone_number)
            fake_saas.bindings = [
                {"channelType": "FEISHU", "bound": True, "channelUserId": "ou-1", "boundAt": "2026-11-06T10:20:30"},
                {"channelType": "DINGTALK", "bound": False, "channelUserId": None, "boundAt": None},
                {"channelType": "WECOM", "bound": True, "channelUserId": "wc-1", "boundAt": "2026-11-05T09:08:07"},
            ]

            rows = await list_bindings(session, user=user)

            assert rows == [
                {"platform": "feishu", "platform_user_id": "ou-1", "created_at": "2026-11-06T10:20:30"}
            ]
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)


async def test_delete_binding_calls_tenant_and_clears_cache(binding_engine, fake_saas):
    """解绑由 Tenant 执行，本地只清理缓存。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            await _seed_agent_session(user.uid, user.phone_number)
            await remember_binding(uid=user.uid, platform="feishu", channel_user_id="ou-1", ttl_seconds=600)

            await delete_binding(session, user=user, platform="feishu")

            assert fake_saas.unbound == [("tenant-session-1", "FEISHU")]
            assert await bind_uid("feishu", "ou-1") is None
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)


async def test_list_bindings_requires_login_session(binding_engine, fake_saas):
    """没有 Tenant AgentSession 时查询直接 401，不调用 Tenant。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user = await _create_user(session, user_uid)
            with pytest.raises(HTTPException) as exc_info:
                await list_bindings(session, user=user)
            assert exc_info.value.status_code == 401
            assert exc_info.value.detail["code"] == "im_binding_login_required"
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)


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


async def test_list_bindings_maps_employee_unavailable(binding_engine, fake_saas):
    """Tenant 20302 映射为 403，不把员工停用伪装成临时故障。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            await _create_user(session, user_uid)
        await _seed_agent_session(user_uid)
        fake_saas.list_error = SaasAPIError(code=20302, msg="员工不存在或已停用")

        async with binding_engine() as session:
            user = (await session.execute(select(User).where(User.uid == user_uid))).scalar_one()
            with pytest.raises(HTTPException) as exc_info:
                await list_bindings(session, user=user)
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "im_employee_unavailable"


async def test_list_bindings_maps_channel_key_failure(binding_engine, fake_saas):
    """Tenant 20317 映射为 502，管理密钥问题必须显式失败。"""
    uid = uuid.uuid4().hex[:10]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            await _create_user(session, user_uid)
        await _seed_agent_session(user_uid)
        fake_saas.list_error = SaasAPIError(code=20317, msg="管理密钥错误")

        async with binding_engine() as session:
            user = (await session.execute(select(User).where(User.uid == user_uid))).scalar_one()
            with pytest.raises(HTTPException) as exc_info:
                await list_bindings(session, user=user)
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [user_uid])
        await _clear_user_state(user_uid)

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "im_binding_tenant_unavailable"
