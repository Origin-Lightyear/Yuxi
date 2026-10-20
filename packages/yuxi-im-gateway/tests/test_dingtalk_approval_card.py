import pytest

from yuxi_im_gateway.adapters.dingtalk import DingTalkAdapter
from yuxi_im_gateway.models import BotConfig


class FakeChatbotHandler:
    """记录 reply_markdown_button 调用的 ChatbotHandler 替身。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def reply_markdown_button(self, incoming, markdown, button_list, tips="", title=""):
        self.calls.append(
            {"incoming": incoming, "markdown": markdown, "buttons": button_list, "tips": tips, "title": title}
        )


def _adapter() -> DingTalkAdapter:
    bot = BotConfig(1, "dingtalk", "b", {"client_id": "1", "client_secret": "s"}, "k", "a")
    return DingTalkAdapter(bot, on_inbound=lambda m: None)


@pytest.mark.asyncio
async def test_send_approval_card_uses_sdk_card_instance_with_action_value():
    adapter = _adapter()
    handler = FakeChatbotHandler()
    adapter._chatbot_handler = handler
    incoming = object()
    adapter._recent_messages["c1"] = incoming

    await adapter.send_approval_card("c1", thread_id="t1", run_id="r1", requester_id="u1")

    assert len(handler.calls) == 1
    call = handler.calls[0]
    assert call["incoming"] is incoming
    assert call["title"] == "工具审批"
    assert call["buttons"][0]["value"] == {
        "thread_id": "t1",
        "run_id": "r1",
        "requester_id": "u1",
        "chat_id": "c1",
        "decision": "approve",
    }
    assert call["buttons"][1]["value"]["decision"] == "reject"


@pytest.mark.asyncio
async def test_send_approval_card_skips_without_cached_inbound_message():
    adapter = _adapter()
    handler = FakeChatbotHandler()
    adapter._chatbot_handler = handler
    texts: list[tuple[str, str]] = []

    async def fake_send_text(chat_id: str, text: str) -> None:
        texts.append((chat_id, text))

    adapter.send_text = fake_send_text

    await adapter.send_approval_card("c1", thread_id="t1", run_id="r1", requester_id="u1")

    assert handler.calls == []
    assert texts == [("c1", "审批卡片发送失败，请直接回复 /approve 或 /reject 进行审批")]
