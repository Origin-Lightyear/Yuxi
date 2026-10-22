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
    monkeypatch.setattr(dingtalk, "ChatbotMessage", SimpleNamespace(from_dict=lambda data: object()))
    return dingtalk._ChatbotHandler(adapter), dispatched


@pytest.mark.asyncio
async def test_handler_dispatches_when_group_mention_has_no_text(monkeypatch):
    inbound = InboundMessage("m1", "c1", "u1", is_group=True, mentions_bot=True, text="")
    handler, dispatched = _handler(monkeypatch, inbound)

    await handler.process(SimpleNamespace(data={}))

    assert dispatched == [inbound]


@pytest.mark.asyncio
async def test_handler_ignores_empty_text_without_mention(monkeypatch):
    inbound = InboundMessage("m1", "c1", "u1", is_group=True, mentions_bot=False, text="")
    handler, dispatched = _handler(monkeypatch, inbound)

    await handler.process(SimpleNamespace(data={}))

    assert dispatched == []
