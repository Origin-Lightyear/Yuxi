"""Channel 入口按 IM 绑定解析真实执行身份的路由级集成测试。

进程内直接调用 ``receive_channel_message``，用真实 PostgreSQL 与 Redis 验证
``resolve_channel_identity`` 的缓存命中、Tenant 冷启动回查、归属人回退、强绑
拦截与机器人归属校验，并断言每次成功响应都带 ``identity_source``。Tenant
客户端以替身注入，下游 ``submit_agent_request`` 以 monkeypatch 捕获。
"""

from __future__ import annotations

import importlib
import os
import time
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.utils.auth_middleware import AuthPrincipal
from yuxi.services.im_binding_cache import bind_uid, forget_user_bindings, remember_binding
from yuxi.services.saas_client import SaasAPIError, _parse_employee_auth
from yuxi.storage.postgres.models_business import APIKey, IMBotConfig, User
from yuxi.storage.redis import close_async_redis_client, get_async_redis_client

channel_router = importlib.import_module("server.routers.agent_invocation_channel_router")

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture()
async def binding_engine():
    """自建真实 PostgreSQL 会话工厂，测试结束后释放连接池。"""
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _isolated_redis_client():
    """把共享 Redis 客户端绑定到当前用例的事件循环。"""
    await close_async_redis_client()
    await get_async_redis_client()
    yield
    await close_async_redis_client()


@pytest.fixture(autouse=True)
def _saas_binding_enabled(monkeypatch):
    """默认按 SaaS 部署运行；非 SaaS 用例自行覆盖开关。"""
    from yuxi.config import config as app_config

    monkeypatch.setattr(app_config, "saas_enabled", True)
    monkeypatch.setattr(app_config, "platform_service_url", "https://tenant.example.com")


class _FakeTenant:
    """记录平台登录调用次数，按用例返回员工身份或异常。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.auth_result = None
        self.error: Exception | None = None

    async def authenticate_channel(self, channel_type: str, channel_user_id: str):
        self.calls.append((channel_type, channel_user_id))
        if self.error is not None:
            raise self.error
        return self.auth_result


@pytest.fixture()
def fake_tenant(monkeypatch) -> _FakeTenant:
    tenant = _FakeTenant()
    import yuxi.services.im_binding_service as im_binding_service

    monkeypatch.setattr(im_binding_service, "get_saas_client", lambda: tenant)
    return tenant


def _employee_auth(employee_code: str):
    """构造 Tenant auth/channel 的成功响应。"""
    return _parse_employee_auth(
        {
            "mobile": "13800000001",
            "tenants": [
                {
                    "tenantId": 1001,
                    "tenantName": "示例公司",
                    "employeeId": 2001,
                    "employeeCode": employee_code,
                    "employeeName": "张三",
                    "departmentName": "财务部",
                    "enabled": 1,
                    "llmKey": "",
                    "llmUrl": "",
                    "agentSessionToken": "session-from-channel",
                    "agentSessionExpiresAt": int(time.time()) + 600,
                }
            ],
        }
    )


async def _create_user(session, uid: str) -> User:
    """创建临时用户并返回。"""
    user = User(username=uid, uid=uid, password_hash="pytest")
    session.add(user)
    await session.commit()
    return user


async def _create_bot(
    session, *, platform: str, owner_uid: str, require_binding: bool = False, enabled: bool = True
) -> IMBotConfig:
    """创建临时机器人配置及其 API Key，返回可供身份解析校验的机器人。"""
    owner = (await session.execute(select(User).where(User.uid == owner_uid))).scalar_one()
    api_key = APIKey(
        key_hash=uuid.uuid4().hex,
        key_prefix="pytest",
        name=f"pytest-bot-key-{uuid.uuid4().hex[:8]}",
        user_id=owner.id,
        created_by=owner.uid,
    )
    session.add(api_key)
    await session.flush()
    bot = IMBotConfig(
        platform=platform,
        name=f"bot-{uuid.uuid4().hex[:8]}",
        enabled=enabled,
        credentials_encrypted="pytest-placeholder",
        owner_uid=owner_uid,
        agent_slug="default-chatbot",
        require_binding=require_binding,
        api_key_id=api_key.id,
    )
    session.add(bot)
    await session.commit()
    return bot


async def _cleanup(session, user_uids: list[str]) -> None:
    """删除测试机器人、API Key 与用户，并清理绑定缓存。"""
    user_ids = list(
        (await session.execute(select(User.id).where(User.uid.in_(user_uids)))).scalars().all()
    )
    await session.execute(delete(IMBotConfig).where(IMBotConfig.owner_uid.in_(user_uids)))
    if user_ids:
        await session.execute(delete(APIKey).where(APIKey.user_id.in_(user_ids)))
    await session.execute(delete(User).where(User.uid.in_(user_uids)))
    await session.commit()
    for uid in user_uids:
        await forget_user_bindings(uid)


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


def _capture_submit(monkeypatch) -> dict[str, str]:
    """替换提交服务并捕获最终执行身份。"""
    captured: dict[str, str] = {}

    async def fake_submit_agent_request(*, request_input, current_user, **_kwargs):
        captured["current_uid"] = current_user.uid
        return {"run_id": "run-1", "thread_id": request_input.thread_id, "status": "dispatched"}

    monkeypatch.setattr(channel_router, "submit_agent_request", fake_submit_agent_request)
    return captured


async def test_cached_sender_resolves_without_tenant_call(binding_engine, monkeypatch, fake_tenant):
    """缓存命中直接解析绑定用户，0 次 Tenant 调用。"""
    uid = uuid.uuid4().hex[:10]
    owner_uid = f"owner-{uid}"
    bound_uid = f"bound-{uid}"
    captured = _capture_submit(monkeypatch)

    try:
        async with binding_engine() as session:
            owner = await _create_user(session, owner_uid)
            bound = await _create_user(session, bound_uid)
            bot = await _create_bot(session, platform="feishu", owner_uid=owner.uid)
            await remember_binding(uid=bound.uid, platform="feishu", channel_user_id="sender-1", ttl_seconds=600)

            result = await channel_router.receive_channel_message(
                _payload(str(bot.id), "sender-1"),
                principal=AuthPrincipal(user=owner, api_key_id=bot.api_key_id),
                db=session,
            )
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [owner_uid, bound_uid])

    assert result["identity_source"] == "binding"
    assert captured["current_uid"] == bound_uid
    assert fake_tenant.calls == []


async def test_cache_miss_authenticates_once_and_backfills(binding_engine, monkeypatch, fake_tenant):
    """缓存未命中时调用一次 auth/channel，并把 uid 写回缓存供后续消息复用。"""
    uid = uuid.uuid4().hex[:10]
    owner_uid = f"owner-{uid}"
    bound_uid = f"bound-{uid}"
    captured = _capture_submit(monkeypatch)
    fake_tenant.auth_result = _employee_auth(bound_uid)

    try:
        async with binding_engine() as session:
            owner = await _create_user(session, owner_uid)
            await _create_user(session, bound_uid)
            bot = await _create_bot(session, platform="feishu", owner_uid=owner.uid)

            first = await channel_router.receive_channel_message(
                _payload(str(bot.id), "sender-1"),
                principal=AuthPrincipal(user=owner, api_key_id=bot.api_key_id),
                db=session,
            )
            second = await channel_router.receive_channel_message(
                _payload(str(bot.id), "sender-1"),
                principal=AuthPrincipal(user=owner, api_key_id=bot.api_key_id),
                db=session,
            )
            cached_uid = await bind_uid("feishu", "sender-1")
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [owner_uid, bound_uid])

    assert first["identity_source"] == "binding"
    assert second["identity_source"] == "binding"
    assert captured["current_uid"] == bound_uid
    assert fake_tenant.calls == [("FEISHU", "sender-1")]
    assert cached_uid == bound_uid


async def test_stale_cache_reauthenticates(binding_engine, monkeypatch, fake_tenant):
    """缓存中的 uid 在本地不存在时重新认证，不把消息归到不存在的用户。"""
    uid = uuid.uuid4().hex[:10]
    owner_uid = f"owner-{uid}"
    bound_uid = f"bound-{uid}"
    captured = _capture_submit(monkeypatch)
    fake_tenant.auth_result = _employee_auth(bound_uid)

    try:
        async with binding_engine() as session:
            owner = await _create_user(session, owner_uid)
            bot = await _create_bot(session, platform="feishu", owner_uid=owner.uid)
            await remember_binding(uid="missing-user", platform="feishu", channel_user_id="sender-1", ttl_seconds=600)

            result = await channel_router.receive_channel_message(
                _payload(str(bot.id), "sender-1"),
                principal=AuthPrincipal(user=owner, api_key_id=bot.api_key_id),
                db=session,
            )
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [owner_uid, bound_uid, "missing-user"])

    assert result["identity_source"] == "binding"
    assert captured["current_uid"] == bound_uid
    assert fake_tenant.calls == [("FEISHU", "sender-1")]


async def test_cache_read_failure_falls_back_to_tenant(binding_engine, monkeypatch, fake_tenant):
    """Redis 读失败时回退到 Tenant 冷启动回查，不把 IM 消息变成 500。"""
    import yuxi.services.im_binding_service as im_binding_service

    uid = uuid.uuid4().hex[:10]
    owner_uid = f"owner-{uid}"
    bound_uid = f"bound-{uid}"
    captured = _capture_submit(monkeypatch)
    fake_tenant.auth_result = _employee_auth(bound_uid)

    async def failing_bind_uid(platform, channel_user_id):
        del platform, channel_user_id
        raise RuntimeError("redis down")

    monkeypatch.setattr(im_binding_service.im_binding_cache, "bind_uid", failing_bind_uid)

    try:
        async with binding_engine() as session:
            owner = await _create_user(session, owner_uid)
            bot = await _create_bot(session, platform="feishu", owner_uid=owner.uid)

            result = await channel_router.receive_channel_message(
                _payload(str(bot.id), "sender-1"),
                principal=AuthPrincipal(user=owner, api_key_id=bot.api_key_id),
                db=session,
            )
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [owner_uid, bound_uid])

    assert result["identity_source"] == "binding"
    assert captured["current_uid"] == bound_uid
    assert fake_tenant.calls == [("FEISHU", "sender-1")]


async def test_unbound_sender_falls_back_to_owner(binding_engine, monkeypatch, fake_tenant):
    """Tenant 返回未绑定且 require_binding=false 时回退到归属人。"""
    uid = uuid.uuid4().hex[:10]
    owner_uid = f"owner-{uid}"
    captured = _capture_submit(monkeypatch)
    fake_tenant.error = SaasAPIError(code=20206, msg="账号或密码错误")

    try:
        async with binding_engine() as session:
            owner = await _create_user(session, owner_uid)
            bot = await _create_bot(session, platform="feishu", owner_uid=owner.uid, require_binding=False)

            result = await channel_router.receive_channel_message(
                _payload(str(bot.id), "unknown-sender"),
                principal=AuthPrincipal(user=owner, api_key_id=bot.api_key_id),
                db=session,
            )
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [owner_uid])

    assert result["identity_source"] == "owner_fallback"
    assert captured["current_uid"] == owner_uid


async def test_unbound_sender_required_binding_rejects(binding_engine, monkeypatch, fake_tenant):
    """Tenant 返回未绑定且 require_binding=true 时返回 409，且不提交任何 run。"""
    uid = uuid.uuid4().hex[:10]
    owner_uid = f"owner-{uid}"
    calls: list[dict] = []
    fake_tenant.error = SaasAPIError(code=20206, msg="账号或密码错误")

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
                    principal=AuthPrincipal(user=owner, api_key_id=bot.api_key_id),
                    db=session,
                )
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [owner_uid])

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "im_account_not_bound"
    assert calls == []


async def test_tenant_unavailable_fails_closed(binding_engine, monkeypatch, fake_tenant):
    """Tenant 不可达时返回 502，不静默回退到机器人归属人。"""
    uid = uuid.uuid4().hex[:10]
    owner_uid = f"owner-{uid}"
    calls: list[dict] = []
    fake_tenant.error = httpx.ConnectError("tenant down")

    async def fake_submit_agent_request(**kwargs):
        calls.append(kwargs)
        raise AssertionError("身份服务不可用时不应提交 Run")

    monkeypatch.setattr(channel_router, "submit_agent_request", fake_submit_agent_request)

    try:
        async with binding_engine() as session:
            owner = await _create_user(session, owner_uid)
            bot = await _create_bot(session, platform="feishu", owner_uid=owner.uid, require_binding=False)

            with pytest.raises(HTTPException) as exc_info:
                await channel_router.receive_channel_message(
                    _payload(str(bot.id), "sender-1"),
                    principal=AuthPrincipal(user=owner, api_key_id=bot.api_key_id),
                    db=session,
                )
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [owner_uid])

    assert exc_info.value.status_code == 502
    assert exc_info.value.detail["code"] == "im_binding_unavailable"
    assert calls == []


async def test_non_saas_keeps_owner_identity(binding_engine, monkeypatch, fake_tenant):
    """非 SaaS 部署即使 require_binding=true 也按归属人执行，不调用 Tenant。"""
    from yuxi.config import config as app_config

    monkeypatch.setattr(app_config, "saas_enabled", False)
    uid = uuid.uuid4().hex[:10]
    owner_uid = f"owner-{uid}"
    captured = _capture_submit(monkeypatch)

    try:
        async with binding_engine() as session:
            owner = await _create_user(session, owner_uid)
            bot = await _create_bot(session, platform="feishu", owner_uid=owner.uid, require_binding=True)

            result = await channel_router.receive_channel_message(
                _payload(str(bot.id), "sender-1"),
                principal=AuthPrincipal(user=owner, api_key_id=bot.api_key_id),
                db=session,
            )
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [owner_uid])

    assert result["identity_source"] == "owner"
    assert captured["current_uid"] == owner_uid
    assert fake_tenant.calls == []


async def test_bot_not_owned_by_transport_user_rejects(binding_engine, fake_tenant):
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
                    principal=AuthPrincipal(user=owner_a, api_key_id=bot_b.api_key_id),
                    db=session,
                )
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [owner_a_uid, owner_b_uid])

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "im_bot_not_owned"


async def test_disabled_bot_rejects_before_identity_resolution(binding_engine, fake_tenant):
    """机器人停用时返回 403 ``im_bot_disabled``，不解析身份也不调用 Tenant。"""
    uid = uuid.uuid4().hex[:10]
    owner_uid = f"owner-{uid}"

    try:
        async with binding_engine() as session:
            owner = await _create_user(session, owner_uid)
            bot = await _create_bot(session, platform="feishu", owner_uid=owner.uid, enabled=False)

            with pytest.raises(HTTPException) as exc_info:
                await channel_router.receive_channel_message(
                    _payload(str(bot.id), "sender-1"),
                    principal=AuthPrincipal(user=owner, api_key_id=bot.api_key_id),
                    db=session,
                )
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [owner_uid])

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "im_bot_disabled"
    assert fake_tenant.calls == []


async def test_state_command_returns_identity_source(binding_engine, monkeypatch, fake_tenant):
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
            await remember_binding(uid=bound.uid, platform="feishu", channel_user_id="sender-1", ttl_seconds=600)

            result = await channel_router.receive_channel_message(
                _payload(str(bot.id), "sender-1", text="/state"),
                principal=AuthPrincipal(user=owner, api_key_id=bot.api_key_id),
                db=session,
            )
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [owner_uid, bound_uid])

    assert result["command"] == "state"
    assert result["identity_source"] == "binding"
    assert captured["state_uid"] == bound_uid


async def test_state_command_without_sender_falls_back_to_owner(binding_engine, monkeypatch, fake_tenant):
    """无 sender 的 /state 命令走归属人回退，响应 identity_source=owner_fallback。"""
    uid = uuid.uuid4().hex[:10]
    owner_uid = f"owner-{uid}"
    captured: dict[str, str] = {}

    async def fake_state(**kwargs):
        captured["state_uid"] = kwargs["current_user"].uid
        return {"agent_state": {"todos": []}}

    monkeypatch.setattr(channel_router, "get_agent_state_view", fake_state)

    try:
        async with binding_engine() as session:
            owner = await _create_user(session, owner_uid)
            bot = await _create_bot(session, platform="feishu", owner_uid=owner.uid, require_binding=False)

            result = await channel_router.receive_channel_message(
                _payload(str(bot.id), None, text="/state"),
                principal=AuthPrincipal(user=owner, api_key_id=bot.api_key_id),
                db=session,
            )
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [owner_uid])

    assert result["command"] == "state"
    assert result["identity_source"] == "owner_fallback"
    assert captured["state_uid"] == owner_uid


async def test_reject_without_pending_approval_returns_conflict(binding_engine, fake_tenant):
    """无待审批运行时的 /reject 返回 409 no_pending_approval，身份先解析成功。"""
    uid = uuid.uuid4().hex[:10]
    owner_uid = f"owner-{uid}"

    try:
        async with binding_engine() as session:
            owner = await _create_user(session, owner_uid)
            bot = await _create_bot(session, platform="feishu", owner_uid=owner.uid, require_binding=False)

            with pytest.raises(HTTPException) as exc_info:
                await channel_router.receive_channel_message(
                    _payload(str(bot.id), None, text="/reject"),
                    principal=AuthPrincipal(user=owner, api_key_id=bot.api_key_id),
                    db=session,
                )
    finally:
        async with binding_engine() as session:
            await _cleanup(session, [owner_uid])

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "no_pending_approval"
