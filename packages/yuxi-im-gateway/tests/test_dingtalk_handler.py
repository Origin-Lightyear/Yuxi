from types import SimpleNamespace

import pytest

from yuxi_im_gateway.adapters import dingtalk
from yuxi_im_gateway.models import InboundMessage


def _handler(monkeypatch, inbound: InboundMessage):
    """构造只依赖归一化结果的 handler 替身，记录是否触发入站分发。"""
    dispatched: list[InboundMessage] = []

    async def fake_on_inbound(msg: InboundMessage):
        return None

    def fake_dispatch(awaitable):
        awaitable.close()
        dispatched.append(inbound)

    adapter = SimpleNamespace(
        _remember=lambda message: None,
        _normalize=lambda message: inbound,
        _on_inbound=fake_on_inbound,
        _dispatch=fake_dispatch,
    )
    monkeypatch.setattr(
        dingtalk, "ChatbotMessage", SimpleNamespace(from_dict=lambda data: SimpleNamespace(message_type="text"))
    )
    return dingtalk._ChatbotHandler(adapter), dispatched


@pytest.mark.asyncio
async def test_handler_dispatches_when_group_mention_has_no_text(monkeypatch):
    inbound = InboundMessage("m1", "c1", "u1", is_group=True, mentions_bot=True, text="")
    handler, dispatched = _handler(monkeypatch, inbound)

    await handler.process(SimpleNamespace(data={}))

    assert dispatched == [inbound]


@pytest.mark.asyncio
async def test_handler_dispatches_empty_text_in_private_chat(monkeypatch):
    """单聊发图片或语音时正文为空，也要进入处理以回一条文字引导。"""
    inbound = InboundMessage("m1", "c1", "u1", is_group=False, mentions_bot=False, text="")
    handler, dispatched = _handler(monkeypatch, inbound)

    await handler.process(SimpleNamespace(data={}))

    assert dispatched == [inbound]


def test_normalize_reads_rich_text_when_text_missing():
    message = SimpleNamespace(
        message_type="richText",
        text=None,
        conversation_type="1",
        message_id="m1",
        conversation_id="c1",
        sender_staff_id="u1",
        is_in_at_list=False,
        at_users=[],
        get_text_list=lambda: ["查一下大力水手", "11 月应收账款"],
    )

    inbound = dingtalk.DingTalkAdapter._normalize(object(), message)

    assert inbound.text == "查一下大力水手\n11 月应收账款"
