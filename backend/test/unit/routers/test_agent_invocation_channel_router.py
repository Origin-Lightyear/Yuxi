from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

router = importlib.import_module("server.routers.agent_invocation_channel_router")


@pytest.fixture(autouse=True)
def _resolve_identity(monkeypatch: pytest.MonkeyPatch):
    """把身份解析替换为透传 transport_user，使单元测试聚焦消息信封与命令分支。"""

    async def fake_resolve_channel_identity(db, *, transport_user, account_id, sender_id):
        del db, account_id, sender_id
        return SimpleNamespace(user=transport_user, owner_fallback=False)

    monkeypatch.setattr(router, "resolve_channel_identity", fake_resolve_channel_identity)


def _payload(text: str, **kwargs):
    values = {
        "agent_slug": "default-chatbot",
        "thread_id": "thread-1",
        "message": {"type": "text", "text": text},
        "message_id": "message-1",
    }
    values.update(kwargs)
    return router.ChannelMessageRequest(
        **values,
    )


def test_channel_rejects_overlong_message_id_at_schema_boundary():
    with pytest.raises(ValidationError):
        _payload("你好", message_id="x" * 129)


def test_channel_rejects_overlong_channel_at_schema_boundary():
    with pytest.raises(ValidationError):
        _payload("你好", channel="x" * 33)


@pytest.mark.asyncio
async def test_plain_channel_text_uses_shared_submission(monkeypatch: pytest.MonkeyPatch):
    calls: dict[str, object] = {}

    async def fake_submit_agent_request(*, request_input, **_kwargs):
        calls["request_input"] = request_input
        return {"run_id": "run-1", "thread_id": request_input.thread_id, "status": "dispatched"}

    class EmptyRunRepo:
        def __init__(self, db):
            del db

        async def get_run_by_request_id(self, request_id: str):
            del request_id
            return None

        async def get_latest_chat_or_resume_run(self, **_kwargs):
            return None

    monkeypatch.setattr(router, "submit_agent_request", fake_submit_agent_request)
    monkeypatch.setattr(router, "AgentRunRepository", EmptyRunRepo)
    result = await router.receive_channel_message(
        _payload("你好", channel="cli", account_id="local", chat_id="chat-1"),
        current_user=SimpleNamespace(uid="user-1"),
        db=object(),
    )

    assert result["kind"] == "run"
    assert calls["request_input"].origin.source == "channel"
    assert calls["request_input"].origin.channel == "cli"
    assert calls["request_input"].origin.external_id == "message-1"
    assert calls["request_input"].queue_policy == "steer"


@pytest.mark.asyncio
async def test_channel_rejects_whitespace_text_before_submission(monkeypatch: pytest.MonkeyPatch):
    async def fail_submit(**_kwargs):
        raise AssertionError("空白消息不应进入提交服务")

    monkeypatch.setattr(router, "submit_agent_request", fail_submit)

    with pytest.raises(HTTPException) as exc:
        await router.receive_channel_message(
            _payload("   "),
            current_user=SimpleNamespace(uid="user-1"),
            db=object(),
        )

    assert exc.value.status_code == 422
    assert exc.value.detail == "text 不能为空"


@pytest.mark.asyncio
async def test_channel_uses_request_id_as_external_id_when_message_id_missing(monkeypatch: pytest.MonkeyPatch):
    calls: dict[str, object] = {}

    async def fake_submit_agent_request(*, request_input, **_kwargs):
        calls["request_input"] = request_input
        return {"run_id": "run-1", "thread_id": request_input.thread_id, "status": "dispatched"}

    class EmptyRunRepo:
        def __init__(self, db):
            del db

        async def get_run_by_request_id(self, request_id: str):
            del request_id
            return None

        async def get_latest_chat_or_resume_run(self, **_kwargs):
            return None

    monkeypatch.setattr(router, "submit_agent_request", fake_submit_agent_request)
    monkeypatch.setattr(router, "AgentRunRepository", EmptyRunRepo)
    await router.receive_channel_message(
        router.ChannelMessageRequest(
            agent_slug="default-chatbot",
            thread_id="thread-1",
            request_id="request-1",
            message={"type": "text", "text": "你好"},
        ),
        current_user=SimpleNamespace(uid="user-1"),
        db=object(),
    )

    assert calls["request_input"].request_id == "request-1"
    assert calls["request_input"].origin.external_id == "request-1"


@pytest.mark.asyncio
async def test_state_command_does_not_submit(monkeypatch: pytest.MonkeyPatch):
    async def fail_submit(**_kwargs):
        raise AssertionError("/state must not create a Request")

    async def fake_state(**_kwargs):
        return {"agent_state": {"todos": []}}

    monkeypatch.setattr(router, "submit_agent_request", fail_submit)
    monkeypatch.setattr(router, "get_agent_state_view", fake_state)
    result = await router.receive_channel_message(
        _payload("/state", channel="dingtalk", account_id="1"),
        current_user=SimpleNamespace(uid="user-1"),
        db=object(),
    )
    assert result == {
        "kind": "command",
        "command": "state",
        "thread_id": "thread-1",
        "state": {"agent_state": {"todos": []}},
        "identity_source": "binding",
    }


@pytest.mark.asyncio
async def test_approve_command_creates_resume_without_submit(monkeypatch: pytest.MonkeyPatch):
    class RunRepo:
        def __init__(self, db):
            del db

        async def get_run_by_request_id(self, request_id: str):
            del request_id
            return None

        async def get_latest_chat_or_resume_run(self, **_kwargs):
            return SimpleNamespace(id="run-parent", status="interrupted", error_type="human_approval_required")

    calls: dict[str, object] = {}

    async def fake_create_resume_run_view(**kwargs):
        calls["kwargs"] = kwargs
        return {"run_id": "run-resume", "status": "pending", "thread_id": kwargs["thread_id"]}

    async def fail_submit(**_kwargs):
        raise AssertionError("/approve must not create a Request")

    monkeypatch.setattr(router, "AgentRunRepository", RunRepo)
    monkeypatch.setattr(router, "create_resume_run_view", fake_create_resume_run_view)
    monkeypatch.setattr(router, "submit_agent_request", fail_submit)
    result = await router.receive_channel_message(
        _payload("/approve"),
        current_user=SimpleNamespace(uid="user-1"),
        db=object(),
    )

    assert result["command"] == "approve"
    assert result["run"]["run_id"] == "run-resume"
    assert calls["kwargs"]["created_by_run_id"] == "run-parent"
    assert calls["kwargs"]["resume"] == {"decisions": [{"type": "approve"}]}


@pytest.mark.asyncio
async def test_approve_command_reuses_existing_resume_when_it_is_latest(monkeypatch: pytest.MonkeyPatch):
    existing_run = SimpleNamespace(
        id="resume-run",
        uid="user-1",
        agent_slug="default-chatbot",
        conversation_thread_id="thread-1",
        run_type="resume",
        status="pending",
        request_id="request-1",
        created_by_run_id="parent-run",
    )

    class RunRepo:
        def __init__(self, db):
            del db

        async def get_run_by_request_id(self, request_id: str):
            assert request_id == "request-1"
            return existing_run

        async def get_latest_chat_or_resume_run(self, **_kwargs):
            return existing_run

    async def fake_create_resume_run_view(**kwargs):
        assert kwargs["created_by_run_id"] == "parent-run"
        return {
            "run_id": existing_run.id,
            "thread_id": existing_run.conversation_thread_id,
            "status": existing_run.status,
            "request_id": existing_run.request_id,
            "stream_url": f"/api/agent/runs/{existing_run.id}/events",
        }

    monkeypatch.setattr(router, "AgentRunRepository", RunRepo)
    monkeypatch.setattr(router, "create_resume_run_view", fake_create_resume_run_view)

    result = await router.receive_channel_message(
        _payload("/approve", request_id="request-1"),
        current_user=SimpleNamespace(uid="user-1"),
        db=object(),
    )

    assert result["command"] == "approve"
    assert result["run"]["run_id"] == "resume-run"


@pytest.mark.asyncio
async def test_approve_command_rejects_request_id_from_older_resume(monkeypatch: pytest.MonkeyPatch):
    existing_run = SimpleNamespace(
        id="old-resume",
        uid="user-1",
        agent_slug="default-chatbot",
        conversation_thread_id="thread-1",
        run_type="resume",
        status="completed",
        request_id="request-1",
        created_by_run_id="old-parent",
    )
    latest_run = SimpleNamespace(
        id="new-parent",
        status="interrupted",
        error_type="human_approval_required",
    )

    class RunRepo:
        def __init__(self, db):
            del db

        async def get_run_by_request_id(self, request_id: str):
            assert request_id == "request-1"
            return existing_run

        async def get_latest_chat_or_resume_run(self, **_kwargs):
            return latest_run

    monkeypatch.setattr(router, "AgentRunRepository", RunRepo)

    with pytest.raises(HTTPException) as exc:
        await router.receive_channel_message(
            _payload("/approve", request_id="request-1"),
            current_user=SimpleNamespace(uid="user-1"),
            db=object(),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == "request_id 冲突"


@pytest.mark.asyncio
async def test_channel_does_not_treat_question_interrupt_as_approval(monkeypatch: pytest.MonkeyPatch):
    class RunRepo:
        def __init__(self, db):
            del db

        async def get_run_by_request_id(self, request_id: str):
            del request_id
            return None

        async def get_latest_chat_or_resume_run(self, **_kwargs):
            return SimpleNamespace(id="run-parent", status="interrupted", error_type="ask_user_question_required")

    monkeypatch.setattr(router, "AgentRunRepository", RunRepo)
    with pytest.raises(HTTPException) as exc:
        await router.receive_channel_message(
            _payload("继续"),
            current_user=SimpleNamespace(uid="user-1"),
            db=object(),
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "ask_user_question_unsupported"


@pytest.mark.asyncio
async def test_cli_channel_skips_im_binding_resolution(monkeypatch: pytest.MonkeyPatch):
    """非 IM 渠道（yuxi-cli）不得进入机器人绑定解析，也不返回 identity_source。"""
    calls: dict[str, object] = {}

    async def fail_resolve_channel_identity(*_args, **_kwargs):
        raise AssertionError("非 IM 渠道不应解析 IM 绑定身份")

    async def fake_submit_agent_request(*, request_input, **_kwargs):
        calls["request_input"] = request_input
        return {"run_id": "run-1", "thread_id": request_input.thread_id, "status": "dispatched"}

    class EmptyRunRepo:
        def __init__(self, db):
            del db

        async def get_run_by_request_id(self, request_id: str):
            del request_id
            return None

        async def get_latest_chat_or_resume_run(self, **_kwargs):
            return None

    monkeypatch.setattr(router, "resolve_channel_identity", fail_resolve_channel_identity)
    monkeypatch.setattr(router, "submit_agent_request", fake_submit_agent_request)
    monkeypatch.setattr(router, "AgentRunRepository", EmptyRunRepo)
    result = await router.receive_channel_message(
        _payload("你好", channel="cli", account_id="local", chat_id="chat-1"),
        current_user=SimpleNamespace(uid="user-1"),
        db=object(),
    )

    assert result["kind"] == "run"
    assert calls["request_input"].origin.channel == "cli"
    assert "identity_source" not in result
