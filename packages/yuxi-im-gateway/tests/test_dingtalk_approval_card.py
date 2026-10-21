import json

import pytest

from yuxi_im_gateway.adapters.dingtalk import DingTalkAdapter, _CardCallbackHandler
from yuxi_im_gateway.models import BotConfig

_FALLBACK_TEXT = "审批请求：请回复 /approve 或 /reject 进行审批"


async def _noop() -> None:
    """占位协程，让被替换的 _resolve 返回值仍可被 close/await。"""




def _adapter(*, template_id: str | None = "tpl-1") -> DingTalkAdapter:
    """构造带可选审批模板 ID 的钉钉适配器。"""
    credentials = {"client_id": "1", "client_secret": "s"}
    if template_id:
        credentials["approval_card_template_id"] = template_id
    bot = BotConfig(1, "dingtalk", "b", credentials, "k", "a")
    return DingTalkAdapter(bot, on_inbound=lambda m: None)


def _callback(content: dict, out_track_id: str = "card-1"):
    """构造钉钉卡片回调消息，content 按 SDK 约定序列化为字符串。"""

    class _Callback:
        data = {
            "content": json.dumps(content),
            "outTrackId": out_track_id,
            "userId": "u9",
            "corpId": "c1",
            "type": "actionCallback",
        }

    return _Callback()


def _stub_delivery(adapter, calls: list[dict], *, result: str = "card-1", error: Exception | None = None) -> None:
    """替换卡片投递方法，记录参数并返回预设实例 ID 或抛出异常。"""

    async def fake_deliver_card(chat_id: str, template_id: str, card_data: dict) -> str:
        calls.append({"chat_id": chat_id, "template_id": template_id, "card_data": card_data})
        if error is not None:
            raise error
        return result

    adapter._deliver_card = fake_deliver_card


def _capture_text(adapter) -> list[tuple[str, str]]:
    """替换 send_text，返回记录列表。"""
    texts: list[tuple[str, str]] = []

    async def fake_send_text(chat_id: str, text: str) -> None:
        texts.append((chat_id, text))

    adapter.send_text = fake_send_text
    return texts


@pytest.mark.asyncio
async def test_send_approval_card_delivers_template_and_tracks_instance():
    adapter = _adapter()
    adapter._recent_messages["c1"] = object()
    calls: list[dict] = []
    _stub_delivery(adapter, calls)

    await adapter.send_approval_card("c1", thread_id="t1", run_id="r1", requester_id="u1")

    assert len(calls) == 1
    assert calls[0]["template_id"] == "tpl-1"
    assert "工具审批" in calls[0]["card_data"]["markdown"]
    assert adapter._approval_cards["card-1"] == ("t1", "r1", "u1", "c1")


@pytest.mark.asyncio
async def test_send_approval_card_falls_back_without_template():
    adapter = _adapter(template_id=None)
    adapter._recent_messages["c1"] = object()
    calls: list[dict] = []
    _stub_delivery(adapter, calls)
    texts = _capture_text(adapter)

    await adapter.send_approval_card("c1", thread_id="t1", run_id="r1", requester_id="u1")

    assert calls == []
    assert texts == [("c1", _FALLBACK_TEXT)]


@pytest.mark.asyncio
async def test_send_approval_card_falls_back_when_delivery_fails():
    adapter = _adapter()
    adapter._recent_messages["c1"] = object()
    calls: list[dict] = []
    _stub_delivery(adapter, calls, error=RuntimeError("400"))
    texts = _capture_text(adapter)

    await adapter.send_approval_card("c1", thread_id="t1", run_id="r1", requester_id="u1")

    assert texts == [("c1", _FALLBACK_TEXT)]
    assert adapter._approval_cards == {}


@pytest.mark.asyncio
async def test_card_callback_resolves_context_by_instance_id():
    adapter = _adapter()
    adapter._approval_cards["card-1"] = ("t1", "r1", "u1", "c1")
    calls: list[tuple] = []

    def fake_resolve(*args) -> object:
        calls.append(args)
        return _noop()

    adapter._resolve = fake_resolve
    adapter._dispatch = lambda coro: coro.close()

    await _CardCallbackHandler(adapter).process(
        _callback({"cardPrivateData": {"params": {"decision": "approve"}}})
    )

    assert calls == [("approve", "t1", "r1", "u9", "u1", "c1")]
    assert "card-1" not in adapter._approval_cards


@pytest.mark.asyncio
async def test_card_callback_ignores_unknown_instance():
    adapter = _adapter()
    calls: list[tuple] = []

    def fake_resolve(*args) -> object:
        calls.append(args)
        return _noop()

    adapter._resolve = fake_resolve
    adapter._dispatch = lambda coro: coro.close()

    await _CardCallbackHandler(adapter).process(
        _callback({"cardPrivateData": {"params": {"decision": "approve"}}}, out_track_id="unknown")
    )

    assert calls == []
