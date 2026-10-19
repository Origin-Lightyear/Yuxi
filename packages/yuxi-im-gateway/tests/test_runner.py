import asyncio

import pytest

from yuxi_im_gateway.dedupe import DedupeCache
from yuxi_im_gateway.models import BotConfig, InboundMessage
from yuxi_im_gateway.runner import (
    SessionQueue,
    approval_request_id,
    handle_inbound,
    resolve_approval,
)


class FakeClient:
    """记录提交参数并按预置结果返回的客户端替身。"""

    def __init__(self, submitted: dict | None = None) -> None:
        self.submitted = submitted if submitted is not None else {"thread_id": "t1", "run_id": "r1"}
        self.calls: list[dict] = []

    async def submit_message(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return self.submitted

    async def stream_run(self, stream_url: str, api_key: str):
        if False:
            yield []

    async def download_artifact(self, thread_id: str, path: str, api_key: str) -> bytes:
        return b"artifact"


class FakeAdapter:
    """记录出站调用的适配器替身。"""

    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.markdowns: list[tuple[str, str]] = []
        self.cards: list[dict] = []
        self.files: list[tuple[str, str, bytes]] = []

    async def send_text(self, chat_id: str, text: str) -> None:
        self.texts.append((chat_id, text))

    async def send_markdown(self, chat_id: str, text: str) -> None:
        self.markdowns.append((chat_id, text))

    async def send_approval_card(self, chat_id: str, **kwargs) -> None:
        self.cards.append({"chat_id": chat_id, **kwargs})

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


def test_approval_request_id_is_deterministic():
    assert approval_request_id("approve", "run-1") == approval_request_id("approve", "run-1")
    assert approval_request_id("approve", "run-1") != approval_request_id("reject", "run-1")


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
async def test_resolve_approval_ignores_non_requester_and_unknown_decision():
    client = FakeClient()

    await resolve_approval(
        client=client,
        adapter=FakeAdapter(),
        bot=_bot(),
        thread_id="t1",
        run_id="r1",
        decision="approve",
        operator_id="u2",
        requester_id="u1",
    )
    await resolve_approval(
        client=client,
        adapter=FakeAdapter(),
        bot=_bot(),
        thread_id="t1",
        run_id="r1",
        decision="maybe",
        operator_id="u1",
        requester_id="u1",
    )

    assert client.calls == []


@pytest.mark.asyncio
async def test_resolve_approval_submits_decision_with_thread_and_request_id():
    client = FakeClient(submitted={"thread_id": "t1", "run_id": "r2"})
    adapter = FakeAdapter()

    await resolve_approval(
        client=client,
        adapter=adapter,
        bot=_bot(),
        thread_id="t1",
        run_id="r1",
        decision="approve",
        operator_id="u1",
        requester_id="u1",
    )

    call = client.calls[0]
    assert call["text"] == "/approve"
    assert call["thread_id"] == "t1"
    assert call["chat_id"] == ""
    assert call["request_id"] == approval_request_id("approve", "r1")


@pytest.mark.asyncio
async def test_resolve_approval_reports_missing_stream_url():
    client = FakeClient(submitted={"kind": "command"})
    adapter = FakeAdapter()

    await resolve_approval(
        client=client,
        adapter=adapter,
        bot=_bot(),
        thread_id="t1",
        run_id="r1",
        decision="approve",
        operator_id="u1",
        requester_id="u1",
    )

    assert adapter.texts == [("t1", "运行未启动，请稍后重试")]
