import asyncio
import json

import httpx
import pytest

from yuxi_im_gateway.dedupe import DedupeCache
from yuxi_im_gateway.models import BotConfig, InboundMessage
from yuxi_im_gateway.runner import SessionQueue, handle_inbound


class FakeClient:
    """记录提交参数并按预置结果返回的客户端替身。"""

    def __init__(self, submitted: dict | None = None, events: list[dict] | None = None, fail_paths=()) -> None:
        self.submitted = submitted if submitted is not None else {"thread_id": "t1", "run_id": "r1"}
        self.events = events or []
        self.fail_paths = set(fail_paths)
        self.calls: list[dict] = []

    async def submit_message(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return self.submitted

    async def stream_run(self, stream_url: str, api_key: str):
        if self.events:
            yield self.events

    async def download_artifact(self, thread_id: str, path: str, api_key: str) -> bytes:
        if path in self.fail_paths:
            raise RuntimeError("下载失败")
        return b"artifact"


class FakeAdapter:
    """记录出站调用的适配器替身。"""

    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.markdowns: list[tuple[str, str]] = []
        self.files: list[tuple[str, str, bytes]] = []

    async def send_text(self, chat_id: str, text: str) -> None:
        self.texts.append((chat_id, text))

    async def send_markdown(self, chat_id: str, text: str) -> None:
        self.markdowns.append((chat_id, text))

    async def send_file(self, chat_id: str, filename: str, content: bytes) -> None:
        self.files.append((chat_id, filename, content))


class RecordingSessions:
    """记录会话键后立即执行的 SessionQueue 替身。"""

    def __init__(self) -> None:
        self.keys: list[str] = []

    async def run(self, key: str, factory) -> None:
        self.keys.append(key)
        await factory()


def _bot(**overrides) -> BotConfig:
    values = dict(
        id=1,
        platform="feishu",
        name="bot",
        credentials={},
        api_key="yxkey",
        agent_slug="agent",
    )
    values.update(overrides)
    return BotConfig(**values)


def _msg(**overrides) -> InboundMessage:
    values = dict(
        message_id="m1",
        chat_id="c1",
        sender_id="u1",
        is_group=False,
        mentions_bot=False,
        text="hello",
    )
    values.update(overrides)
    return InboundMessage(**values)


def _sse(event: str, payload: dict, thread_id: str = "t1") -> dict:
    """构造一条带 thread_id 的 SSE 事件，供运行结果归并使用。"""
    return {"event": event, "data": json.dumps({"thread_id": thread_id, "payload": payload})}


@pytest.mark.asyncio
async def test_session_queue_serializes_same_key():
    queue = SessionQueue()
    order: list[str] = []

    async def job(tag: str, delay: float) -> None:
        order.append(f"start-{tag}")
        await asyncio.sleep(delay)
        order.append(f"end-{tag}")

    await asyncio.gather(
        queue.run("k", lambda: job("a", 0.05)),
        queue.run("k", lambda: job("b", 0.0)),
    )
    assert order == ["start-a", "end-a", "start-b", "end-b"]


@pytest.mark.asyncio
async def test_session_queue_runs_different_keys_concurrently():
    queue = SessionQueue()
    order: list[str] = []

    async def job(tag: str, delay: float) -> None:
        order.append(f"start-{tag}")
        await asyncio.sleep(delay)
        order.append(f"end-{tag}")

    await asyncio.gather(
        queue.run("k1", lambda: job("a", 0.05)),
        queue.run("k2", lambda: job("b", 0.0)),
    )
    assert order.index("start-b") < order.index("end-a")


@pytest.mark.asyncio
async def test_handle_inbound_skips_filtered_messages():
    client = FakeClient()
    adapter = FakeAdapter()
    dedupe = DedupeCache()
    sessions = SessionQueue()

    await handle_inbound(
        client=client,
        adapter=adapter,
        bot=_bot(require_mention=True),
        dedupe=dedupe,
        sessions=sessions,
        msg=_msg(is_group=True, mentions_bot=False),
    )
    await handle_inbound(
        client=client,
        adapter=adapter,
        bot=_bot(allowed_users=["u2"]),
        dedupe=dedupe,
        sessions=sessions,
        msg=_msg(message_id="m2"),
    )

    assert client.calls == []


@pytest.mark.asyncio
async def test_handle_inbound_prompts_when_mention_has_no_text():
    client = FakeClient()
    adapter = FakeAdapter()

    await handle_inbound(
        client=client,
        adapter=adapter,
        bot=_bot(require_mention=True),
        dedupe=DedupeCache(),
        sessions=SessionQueue(),
        msg=_msg(is_group=True, mentions_bot=True, text=""),
    )

    assert client.calls == []
    assert adapter.texts and "请直接描述" in adapter.texts[0][1]


@pytest.mark.asyncio
async def test_handle_inbound_dedupes_by_bot_and_message_id():
    dedupe = DedupeCache()
    sessions = SessionQueue()
    msg = _msg()
    first = FakeClient()
    second = FakeClient()

    await handle_inbound(
        client=first, adapter=FakeAdapter(), bot=_bot(id=1), dedupe=dedupe, sessions=sessions, msg=msg
    )
    await handle_inbound(
        client=second, adapter=FakeAdapter(), bot=_bot(id=1), dedupe=dedupe, sessions=sessions, msg=msg
    )
    assert len(first.calls) == 1
    assert second.calls == []

    other_bot = FakeClient()
    await handle_inbound(
        client=other_bot, adapter=FakeAdapter(), bot=_bot(id=2), dedupe=dedupe, sessions=sessions, msg=msg
    )
    assert len(other_bot.calls) == 1


@pytest.mark.asyncio
async def test_handle_inbound_uses_session_chat_id_and_queue_key():
    client = FakeClient()
    sessions = RecordingSessions()
    msg = _msg(is_group=True, mentions_bot=True, chat_id="g1", sender_id="u1")

    await handle_inbound(
        client=client,
        adapter=FakeAdapter(),
        bot=_bot(group_sessions_per_user=True),
        dedupe=DedupeCache(),
        sessions=sessions,
        msg=msg,
    )

    assert client.calls[0]["chat_id"] == "g1:u1"
    assert sessions.keys == ["g1:u1"]


@pytest.mark.asyncio
async def test_handle_inbound_replies_to_platform_chat_id_not_session_key():
    client = FakeClient()
    adapter = FakeAdapter()
    sessions = RecordingSessions()
    msg = _msg(is_group=True, mentions_bot=True, chat_id="g1", sender_id="u1")

    await handle_inbound(
        client=client,
        adapter=adapter,
        bot=_bot(group_sessions_per_user=True),
        dedupe=DedupeCache(),
        sessions=sessions,
        msg=msg,
    )

    assert client.calls[0]["chat_id"] == "g1:u1"
    assert sessions.keys == ["g1:u1"]
    assert adapter.texts == [("g1", "运行未启动，请稍后重试")]


@pytest.mark.asyncio
async def test_handle_inbound_delivers_markdown_and_all_artifacts():
    events = [
        _sse("messages", {"items": [{"stream_event": {"type": "message_delta", "content": "hello"}}]}),
        _sse("custom", {"agent_state": {"artifacts": ["/out/a.xlsx", "/out/b.xlsx"]}}),
        _sse("end", {"status": "completed"}),
    ]
    client = FakeClient(submitted={"thread_id": "t1", "run_id": "r1", "stream_url": "u1"}, events=events)
    adapter = FakeAdapter()

    await handle_inbound(
        client=client,
        adapter=adapter,
        bot=_bot(),
        dedupe=DedupeCache(),
        sessions=RecordingSessions(),
        msg=_msg(),
    )

    assert adapter.markdowns == [("c1", "hello")]
    assert adapter.files == [("c1", "a.xlsx", b"artifact"), ("c1", "b.xlsx", b"artifact")]


@pytest.mark.asyncio
async def test_handle_inbound_appends_owner_fallback_hint_to_markdown():
    events = [
        _sse("messages", {"items": [{"stream_event": {"type": "message_delta", "content": "hello"}}]}),
        _sse("end", {"status": "completed"}),
    ]
    client = FakeClient(
        submitted={"thread_id": "t1", "run_id": "r1", "stream_url": "u1", "identity_source": "owner_fallback"},
        events=events,
    )
    adapter = FakeAdapter()

    await handle_inbound(
        client=client,
        adapter=adapter,
        bot=_bot(),
        dedupe=DedupeCache(),
        sessions=RecordingSessions(),
        msg=_msg(),
    )

    assert len(adapter.markdowns) == 1
    assert adapter.texts == []
    assert adapter.markdowns[0][1] == (
        "hello\n\n（提示：你还没绑定 Yuxi 账号，当前按机器人归属人身份执行，绑定后改为按本人权限执行）"
    )


@pytest.mark.asyncio
async def test_handle_inbound_bound_identity_has_no_fallback_hint():
    events = [
        _sse("messages", {"items": [{"stream_event": {"type": "message_delta", "content": "hello"}}]}),
        _sse("end", {"status": "completed"}),
    ]
    client = FakeClient(
        submitted={"thread_id": "t1", "run_id": "r1", "stream_url": "u1", "identity_source": "binding"},
        events=events,
    )
    adapter = FakeAdapter()

    await handle_inbound(
        client=client,
        adapter=adapter,
        bot=_bot(),
        dedupe=DedupeCache(),
        sessions=RecordingSessions(),
        msg=_msg(),
    )

    assert adapter.markdowns == [("c1", "hello")]


@pytest.mark.asyncio
async def test_handle_inbound_owner_fallback_hint_when_run_has_only_artifacts():
    events = [
        _sse("custom", {"agent_state": {"artifacts": ["/out/a.xlsx"]}}),
        _sse("end", {"status": "completed"}),
    ]
    client = FakeClient(
        submitted={"thread_id": "t1", "run_id": "r1", "stream_url": "u1", "identity_source": "owner_fallback"},
        events=events,
    )
    adapter = FakeAdapter()

    await handle_inbound(
        client=client,
        adapter=adapter,
        bot=_bot(),
        dedupe=DedupeCache(),
        sessions=RecordingSessions(),
        msg=_msg(),
    )

    assert adapter.markdowns == [
        ("c1", "（提示：你还没绑定 Yuxi 账号，当前按机器人归属人身份执行，绑定后改为按本人权限执行）")
    ]
    assert adapter.files == [("c1", "a.xlsx", b"artifact")]


@pytest.mark.asyncio
async def test_artifact_failure_does_not_block_remaining_artifacts():
    events = [
        _sse("custom", {"agent_state": {"artifacts": ["/out/a.xlsx", "/out/b.xlsx"]}}),
        _sse("end", {"status": "completed"}),
    ]
    client = FakeClient(
        submitted={"thread_id": "t1", "run_id": "r1", "stream_url": "u1"},
        events=events,
        fail_paths={"/out/a.xlsx"},
    )
    adapter = FakeAdapter()

    await handle_inbound(
        client=client,
        adapter=adapter,
        bot=_bot(),
        dedupe=DedupeCache(),
        sessions=RecordingSessions(),
        msg=_msg(),
    )

    assert adapter.files == [("c1", "b.xlsx", b"artifact")]
    assert adapter.texts == [("c1", "交付物 /out/a.xlsx 发送失败")]


@pytest.mark.asyncio
async def test_handle_inbound_reports_approval_as_unsupported():
    events = [
        _sse("end", {"status": "interrupted", "chunk": {"status": "human_approval_required"}}),
    ]
    client = FakeClient(submitted={"thread_id": "t1", "run_id": "r1", "stream_url": "u1"}, events=events)
    adapter = FakeAdapter()

    await handle_inbound(
        client=client,
        adapter=adapter,
        bot=_bot(),
        dedupe=DedupeCache(),
        sessions=RecordingSessions(),
        msg=_msg(),
    )

    assert adapter.texts == [("c1", "运行需要工具审批，IM 会话不支持审批，请改用平台内对话")]
    assert adapter.markdowns == []


class ConflictClient(FakeClient):
    """提交时返回 409 的客户端替身，用于验证提交冲突的提示文案。"""

    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code

    async def submit_message(self, **kwargs) -> dict:
        request = httpx.Request("POST", "http://api/api/agent-invocation/channel/messages")
        response = httpx.Response(
            409,
            json={"detail": {"code": self.code, "message": "线程已有正在执行的运行"}},
            request=request,
        )
        raise httpx.HTTPStatusError("Client error '409 Conflict'", request=request, response=response)


@pytest.mark.asyncio
async def test_handle_inbound_reports_busy_thread_without_leaking_http_error():
    adapter = FakeAdapter()

    await handle_inbound(
        client=ConflictClient("run_active"),
        adapter=adapter,
        bot=_bot(),
        dedupe=DedupeCache(),
        sessions=RecordingSessions(),
        msg=_msg(),
    )

    assert adapter.texts == [("c1", "上一条消息还在执行中，请等它结束后再发。")]


@pytest.mark.asyncio
async def test_handle_inbound_reports_unbound_account():
    adapter = FakeAdapter()

    await handle_inbound(
        client=ConflictClient("im_account_not_bound"),
        adapter=adapter,
        bot=_bot(),
        dedupe=DedupeCache(),
        sessions=RecordingSessions(),
        msg=_msg(),
    )

    assert adapter.texts == [("c1", "请先在 Yuxi 个人设置里完成飞书/钉钉账号绑定后再试。")]
    assert adapter.markdowns == []
