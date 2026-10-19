"""飞书适配器：基于 lark-oapi WebSocket 长连接收发消息与卡片。"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import threading
from collections.abc import Awaitable, Callable

from yuxi_im_gateway.adapters.base import Adapter
from yuxi_im_gateway.adapters.cards import approval_card, parse_card_action
from yuxi_im_gateway.models import BotConfig, InboundMessage

logger = logging.getLogger(__name__)

OnInbound = Callable[[InboundMessage], Awaitable[None]]


class FeishuAdapter(Adapter):
    """单个飞书机器人的长连接适配器。"""

    def __init__(self, bot: BotConfig, *, on_inbound: OnInbound) -> None:
        import lark_oapi as lark

        self._bot = bot
        self._on_inbound = on_inbound
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

        credentials = bot.credentials
        domain = credentials.get("domain") or "feishu"
        self._lark_domain = lark.LARK_DOMAIN if domain == "lark" else lark.FEISHU_DOMAIN
        self._client = (
            lark.Client.builder()
            .app_id(credentials["app_id"])
            .app_secret(credentials["app_secret"])
            .domain(self._lark_domain)
            .build()
        )

    async def start(self) -> None:
        """在后台线程启动 lark-oapi WS 客户端，事件回调经主 loop 调度。"""
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

        # 事件回调运行在 SDK 线程，需持有主 loop 才能把协程安全地投递回去。
        self._loop = asyncio.get_running_loop()

        def on_message(data: P2ImMessageReceiveV1) -> None:
            message = self._normalize(data)
            if message is not None:
                asyncio.run_coroutine_threadsafe(self._on_inbound(message), self._loop)

        def on_card(data) -> None:
            self._handle_card(data)

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_message)
            .register_p2_card_action_trigger(on_card)
            .build()
        )
        ws_client = lark.ws.Client(
            self._bot.credentials["app_id"],
            self._bot.credentials["app_secret"],
            event_handler=handler,
            domain=self._lark_domain,
        )
        self._thread = _run_in_thread(ws_client.start)

    async def stop(self) -> None:
        """仅丢弃线程引用；SDK 无干净关闭接口，守护线程由进程退出兜底。"""
        self._thread = None

    async def send_text(self, chat_id: str, text: str) -> None:
        """发送纯文本消息。"""
        content = json.dumps({"text": text}, ensure_ascii=False)
        await asyncio.to_thread(self._send_message, chat_id, "text", content)

    async def send_markdown(self, chat_id: str, text: str) -> None:
        """用 post 消息内嵌 md 标签发送，失败降级为纯文本。"""
        content = json.dumps({"zh_cn": {"content": [[{"tag": "md", "text": text}]]}}, ensure_ascii=False)
        try:
            await asyncio.to_thread(self._send_message, chat_id, "post", content)
        except Exception:  # noqa: BLE001
            await self.send_text(chat_id, text)

    async def send_file(self, chat_id: str, filename: str, content: bytes) -> None:
        """上传并发送文件附件。"""
        file_key = await asyncio.to_thread(self._upload_file, filename, content)
        await asyncio.to_thread(self._send_message, chat_id, "file", json.dumps({"file_key": file_key}))

    async def send_approval_card(self, chat_id: str, *, thread_id: str, run_id: str, requester_id: str) -> None:
        """发送审批交互卡片。"""
        card = _feishu_card(approval_card(thread_id, run_id, requester_id))
        await asyncio.to_thread(self._send_message, chat_id, "interactive", json.dumps(card, ensure_ascii=False))

    def _normalize(self, data) -> InboundMessage | None:
        """把飞书消息事件归一化为 InboundMessage；非文本消息回复提示并忽略。"""
        event = data.event
        message = event.message
        message_type = getattr(message, "message_type", "")
        if message_type != "text":
            asyncio.run_coroutine_threadsafe(
                self.send_text(message.chat_id, "暂不支持非文本消息"),
                self._loop,
            )
            return None
        content = json.loads(message.content or "{}")
        text = str(content.get("text") or "")
        chat_type = getattr(message, "chat_type", "p2p")
        is_group = chat_type == "group"
        mentions = getattr(message, "mentions", None) or []
        mentions_bot = bool(mentions) if is_group else False
        sender_id = event.sender.sender_id.open_id
        return InboundMessage(
            message_id=message.message_id,
            chat_id=message.chat_id,
            sender_id=sender_id,
            is_group=is_group,
            mentions_bot=mentions_bot,
            text=text.strip(),
        )

    def _handle_card(self, data) -> None:
        """处理卡片回调：解析 value 并把审批调度回主 loop。"""
        action = getattr(getattr(data, "event", None), "action", None)
        value = getattr(action, "value", None)
        operator_id = str(getattr(getattr(data.event, "operator", None), "open_id", ""))
        try:
            decision, thread_id, run_id, requester_id = parse_card_action(value)
        except (ValueError, json.JSONDecodeError):
            return
        asyncio.run_coroutine_threadsafe(
            self._resolve(decision, thread_id, run_id, operator_id, requester_id),
            self._loop,
        )

    async def _resolve(self, decision, thread_id, run_id, operator_id, requester_id) -> None:
        """从主 loop 调用编排层处理审批（client/bot 由 main 注入）。"""
        callback = getattr(self, "approval_handler", None)
        if callback is not None:
            await callback(
                decision=decision, thread_id=thread_id, run_id=run_id,
                operator_id=operator_id, requester_id=requester_id,
            )

    def _send_message(self, chat_id: str, msg_type: str, content: str) -> None:
        """用 lark OpenAPI 主动发送消息（同步调用，由 to_thread 承载）。"""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success():
            raise RuntimeError(f"发送飞书消息失败 code={response.code} msg={response.msg}")

    def _upload_file(self, filename: str, content: bytes) -> str:
        """上传文件并返回 file_key（同步调用，由 to_thread 承载）。"""
        from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

        request = (
            CreateFileRequest.builder()
            .request_body(
                CreateFileRequestBody.builder()
                .file_type("stream")
                .file_name(filename)
                .file(io.BytesIO(content))
                .build()
            )
            .build()
        )
        response = self._client.im.v1.file.create(request)
        if not response.success():
            raise RuntimeError(f"上传飞书文件失败 code={response.code} msg={response.msg}")
        return response.data.file_key


def _run_in_thread(target) -> threading.Thread:
    """在守护线程中运行阻塞的 WS 客户端并返回线程句柄。"""
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def _feishu_card(card: dict) -> dict:
    """把平台无关卡片转换为飞书 interactive schema 2.0。"""
    buttons = [
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": action["label"]},
            "type": "primary" if action["decision"] == "approve" else "danger",
            "behaviors": [{"type": "callback", "value": action["value"]}],
        }
        for action in card["actions"]
    ]
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": card["title"]}},
        "body": {
            "elements": [
                {"tag": "markdown", "content": card["text"]},
                {"tag": "action", "actions": buttons},
            ]
        },
    }
