"""飞书适配器：基于 lark-oapi WebSocket 长连接收发消息。"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import threading
from collections.abc import Awaitable, Callable

from yuxi_im_gateway.adapters.base import Adapter
from yuxi_im_gateway.models import BotConfig, InboundMessage

logger = logging.getLogger(__name__)

OnInbound = Callable[[InboundMessage], Awaitable[None]]


class FeishuAdapter(Adapter):
    """单个飞书机器人的长连接适配器。"""

    RESTART_ON_STOP = True

    def __init__(self, bot: BotConfig, *, on_inbound: OnInbound) -> None:
        import lark_oapi as lark

        self._bot = bot
        self._on_inbound = on_inbound
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stopped = False

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
                self._dispatch(self._on_inbound(message))

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(on_message)
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
        """标记停止并丢弃线程引用；SDK 无干净关闭接口，守护线程由进程退出兜底。"""
        self._stopped = True
        self._thread = None

    def _dispatch(self, coro) -> None:
        """把协程调度回主事件循环，并记录后台任务的未处理异常；停止后丢弃迟到回调。"""
        if self._stopped:
            coro.close()
            return
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.add_done_callback(self._log_task_error)

    @staticmethod
    def _log_task_error(future) -> None:
        """记录后台任务异常，避免回复失败被静默丢弃。"""
        exc = future.exception()
        if exc is not None:
            logger.warning("飞书回调处理失败: %s", exc)

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

    def _normalize(self, data) -> InboundMessage | None:
        """把飞书消息事件归一化为 InboundMessage；非文本消息回复提示并忽略。

        前置条件：群聊接收依赖飞书「群聊中仅 @ 机器人」的事件订阅范围
        （``im:message.group_at_msg``），该范围下收到的每条群消息都必然 @ 了本机器人，
        故此处以 mentions 非空判定。若将来授予更宽的群消息范围，必须改为把 mention 的
        ``id.open_id`` 与本机器人自身 open_id 比对——该值无法从事件或 lark-oapi 1.7.5
        直接获得，需额外调用原始 API。
        """
        event = data.event
        message = event.message
        message_type = getattr(message, "message_type", "")
        if message_type != "text":
            self._dispatch(self.send_text(message.chat_id, "暂不支持非文本消息"))
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
