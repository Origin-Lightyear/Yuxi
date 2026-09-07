"""SaaS 员工身份与双写同步 fail-soft 单元测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import yuxi.services.agent_data_sync as agent_data_sync
from yuxi.services.saas_client import SaasAPIError


def _conversation(uid="u1", extra_metadata=None):
    return SimpleNamespace(
        thread_id="t-1",
        uid=uid,
        title="会话标题",
        extra_metadata=dict(extra_metadata or {}),
    )


def _message(role="user", request_id="req-1", extra_metadata=None):
    return SimpleNamespace(
        role=role, request_id=request_id, content="消息内容", extra_metadata=dict(extra_metadata or {})
    )


@pytest.fixture
def sync_enabled(monkeypatch):
    """pydantic 模型的 property 不能实例 setattr，需 patch 到类上。"""
    monkeypatch.setattr(type(agent_data_sync.app_config), "is_agent_data_sync_enabled", property(lambda self: True))


@pytest.mark.asyncio
async def test_sync_skipped_when_disabled(monkeypatch):
    monkeypatch.setattr(type(agent_data_sync.app_config), "is_agent_data_sync_enabled", property(lambda self: False))

    async def unexpected(*args, **kwargs):
        raise AssertionError("开关关闭时不应触发任何 Tenant 调用")

    monkeypatch.setattr(agent_data_sync, "get_saas_employee_context", unexpected)

    await agent_data_sync.sync_thread_created(None, _conversation())
    await agent_data_sync.sync_user_message(None, _conversation(), _message())
    await agent_data_sync.sync_assistant_message(None, _conversation(), _message("assistant"), None)


@pytest.mark.asyncio
async def test_sync_swallows_tenant_failure(monkeypatch, sync_enabled):
    """Tenant 不可用时只记 warning，不向本地链路抛异常。"""

    async def fake_context(db, uid):
        return SimpleNamespace(uid=uid, tenant_id=100, employee_id=200)

    monkeypatch.setattr(agent_data_sync, "get_saas_employee_context", fake_context)

    class _FailingClient:
        def __init__(self, *args, **kwargs):
            pass

        async def create_conversation(self, title=None, directory_id=None):
            raise SaasAPIError(code=50000, msg="系统异常")

    monkeypatch.setattr(agent_data_sync, "AgentDataClient", _FailingClient)

    await agent_data_sync.sync_thread_created(None, _conversation())


@pytest.mark.asyncio
async def test_sync_user_message_stores_tenant_message_id(monkeypatch, sync_enabled):

    async def fake_context(db, uid):
        return SimpleNamespace(uid=uid, tenant_id=100, employee_id=200)

    monkeypatch.setattr(agent_data_sync, "get_saas_employee_context", fake_context)

    conversation = _conversation(extra_metadata={"agent_data_sync": {"conversation_id": 7, "version": 2}})
    message = _message(request_id="req-42")

    class _OkClient:
        def __init__(self, *args, **kwargs):
            pass

        async def append_message(self, **kwargs):
            return {"message": {"id": 88}, "conversationVersion": 3}

    monkeypatch.setattr(agent_data_sync, "AgentDataClient", _OkClient)

    class _DB:
        async def commit(self):
            return None

    await agent_data_sync.sync_user_message(_DB(), conversation, message)

    assert message.extra_metadata["tenant_message_id"] == 88
    assert conversation.extra_metadata["agent_data_sync"]["version"] == 3
    # clientMessageId 由 request_id + role 稳定派生
    derived_again = agent_data_sync.derive_client_message_id("req-42", "USER")
    assert agent_data_sync.derive_client_message_id("req-42", "USER") == derived_again


def test_derive_client_message_id_is_uuid_and_role_scoped():
    user_id = agent_data_sync.derive_client_message_id("req-1", "USER")
    assistant_id = agent_data_sync.derive_client_message_id("req-1", "ASSISTANT")
    assert user_id != assistant_id
    import uuid as uuid_module

    uuid_module.UUID(user_id)  # 合法 UUID，不抛异常
    uuid_module.UUID(assistant_id)


def test_consumed_score_sums_usage_metadata():
    message = _message(
        "assistant",
        extra_metadata={"usage_metadata": {"input_tokens": 150, "output_tokens": 320}},
    )
    assert agent_data_sync._consumed_score(message) == 470

    empty = _message("assistant")
    assert agent_data_sync._consumed_score(empty) == 0
