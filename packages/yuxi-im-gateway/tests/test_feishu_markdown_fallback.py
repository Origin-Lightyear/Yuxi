import pytest

from yuxi_im_gateway.adapters.feishu import FeishuAdapter
from yuxi_im_gateway.models import BotConfig


def _adapter() -> FeishuAdapter:
    """构造不发起网络 IO 的适配器替身。"""
    bot = BotConfig(1, "feishu", "b", {"app_id": "1", "app_secret": "s"}, "k", "a")
    return FeishuAdapter(bot, on_inbound=lambda m: None)


@pytest.mark.asyncio
async def test_send_markdown_falls_back_to_text_when_send_fails():
    adapter = _adapter()
    sent: list[tuple[str, str]] = []

    def fail(*args) -> None:
        raise RuntimeError("post 不支持")

    async def fake_send_text(chat_id: str, text: str) -> None:
        sent.append((chat_id, text))

    adapter._send_message = fail
    adapter.send_text = fake_send_text

    await adapter.send_markdown("c1", "hi")

    assert sent == [("c1", "hi")]


@pytest.mark.asyncio
async def test_send_markdown_does_not_fall_back_when_send_succeeds():
    adapter = _adapter()
    calls: list[tuple[str, str]] = []

    def ok(chat_id: str, msg_type: str, content: str) -> None:
        calls.append((chat_id, msg_type))

    async def fake_send_text(chat_id: str, text: str) -> None:
        raise AssertionError("成功路径不应降级")

    adapter._send_message = ok
    adapter.send_text = fake_send_text

    await adapter.send_markdown("c1", "hi")

    assert calls == [("c1", "post")]
