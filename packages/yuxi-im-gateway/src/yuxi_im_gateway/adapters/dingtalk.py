"""钉钉适配器：基于 dingtalk-stream Stream Mode 收发消息。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable

import httpx
import dingtalk_stream
from dingtalk_stream import (
    AckMessage,
    CallbackHandler,
    CardCallbackMessage,
    Card_Callback_Router_Topic,
    ChatbotHandler,
    ChatbotMessage,
)

from yuxi_im_gateway.adapters.base import Adapter
from yuxi_im_gateway.adapters.cards import approval_card, parse_card_action
from yuxi_im_gateway.models import BotConfig, InboundMessage

logger = logging.getLogger(__name__)

OnInbound = Callable[[InboundMessage], Awaitable[None]]

_OPENAPI_BASE = "https://api.dingtalk.com"
_ACCESS_TOKEN_URL = f"{_OPENAPI_BASE}/v1.0/oauth2/accessToken"
_SINGLE_SEND_URL = f"{_OPENAPI_BASE}/v1.0/robot/oToMessages/batchSend"
_GROUP_SEND_URL = f"{_OPENAPI_BASE}/v1.0/robot/groupMessages/send"
_MEDIA_UPLOAD_URL = "https://oapi.dingtalk.com/media/upload"


class DingTalkAdapter(Adapter):
    """单个钉钉机器人的 Stream Mode 适配器。"""

    def __init__(self, bot: BotConfig, *, on_inbound: OnInbound) -> None:
        self._bot = bot
        self._on_inbound = on_inbound
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: dingtalk_stream.DingTalkStreamClient | None = None

        # 出站走网关主 loop 上的 httpx 客户端；token 缓存到过期时间。
        self._http: httpx.AsyncClient | None = None
        self._token_lock: asyncio.Lock | None = None
        self._token: str | None = None
        self._token_expire = 0.0

        # 会话级投递信息：session webhook 兜底地址，单聊还需记住对方 staff id。
        self._webhooks: dict[str, str] = {}
        self._single_users: dict[str, str] = {}

    async def start(self) -> None:
        """启动 Stream 客户端线程，出站资源建在主 loop 上。"""
        # 回调运行在 SDK 线程，必须持有主 loop 才能把协程安全地投递回来。
        self._loop = asyncio.get_running_loop()
        self._http = httpx.AsyncClient()
        self._token_lock = asyncio.Lock()

        credential = dingtalk_stream.Credential(
            self._bot.credentials["client_id"], self._bot.credentials["client_secret"]
        )
        self._client = dingtalk_stream.DingTalkStreamClient(credential)
        self._client.register_callback_handler(ChatbotMessage.TOPIC, _ChatbotHandler(self))
        self._client.register_callback_handler(Card_Callback_Router_Topic, _CardCallbackHandler(self))
        threading.Thread(target=self._client.start_forever, daemon=True).start()

    async def stop(self) -> None:
        """丢弃 HTTP 客户端与 Stream 引用；dingtalk-stream 无干净关闭接口，守护线程由进程退出兜底。"""
        if self._http is not None:
            await self._http.aclose()
        self._http = None
        self._client = None

    def _dispatch(self, coro) -> None:
        """把协程调度回主事件循环，并记录后台任务的未处理异常。"""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        future.add_done_callback(self._log_task_error)

    @staticmethod
    def _log_task_error(future) -> None:
        """记录后台任务异常，避免审批 / 回复失败被静默丢弃。"""
        exc = future.exception()
        if exc is not None:
            logger.warning("钉钉回调处理失败: %s", exc)

    async def send_text(self, chat_id: str, text: str) -> None:
        """优先用 robotCode 主动发送，session webhook 回退。"""
        await self._send(chat_id, {"msgtype": "text", "text": {"content": text}})

    async def send_markdown(self, chat_id: str, text: str) -> None:
        """发送 markdown 消息，失败降级为纯文本。"""
        try:
            await self._send(chat_id, {"msgtype": "markdown", "markdown": {"title": "结果", "text": text}})
        except Exception:  # noqa: BLE001
            await self.send_text(chat_id, text)

    async def send_file(self, chat_id: str, filename: str, content: bytes) -> None:
        """上传临时素材并发送文件消息，发送失败由 _send 回退 session webhook。"""
        media_id = await self._upload_media(filename, content)
        await self._send(chat_id, {"msgtype": "file", "file": {"media_id": media_id, "file_name": filename}})

    async def send_approval_card(self, chat_id: str, *, thread_id: str, run_id: str, requester_id: str) -> None:
        """发送 actionCard 审批卡片。"""
        card = approval_card(thread_id, run_id, requester_id)
        buttons = [
            {"title": action["label"], "actionURL": "", "value": json.dumps(action["value"], ensure_ascii=False)}
            for action in card["actions"]
        ]
        body = {
            "msgtype": "actionCard",
            "actionCard": {
                "title": card["title"],
                "text": card["text"],
                "btnOrientation": "0",
                "btns": buttons,
            },
        }
        await self._send(chat_id, body)

    def _normalize(self, message: ChatbotMessage) -> InboundMessage:
        """把钉钉回调消息归一化为 InboundMessage。"""
        is_group = bool(message.conversation_type == "2")
        text = str((message.text and message.text.content) or "").strip()
        return InboundMessage(
            message_id=str(message.message_id),
            chat_id=str(message.conversation_id),
            sender_id=str(message.sender_staff_id),
            is_group=is_group,
            mentions_bot=bool(message.is_in_at_list) if is_group else False,
            text=_strip_at(text, message),
        )

    def _remember(self, message: ChatbotMessage) -> None:
        """记录会话投递信息：session webhook 兜底地址与单聊对方 staff id。"""
        chat_id = str(message.conversation_id)
        webhook = getattr(message, "session_webhook", None)
        if webhook:
            self._webhooks[chat_id] = str(webhook)
        if str(message.conversation_type) == "1" and message.sender_staff_id:
            self._single_users[chat_id] = str(message.sender_staff_id)

    async def _resolve(self, decision, thread_id, run_id, operator_id, requester_id) -> None:
        """从主 loop 调用编排层处理审批（client/bot 由 main 注入）。"""
        callback = getattr(self, "approval_handler", None)
        if callback is not None:
            await callback(
                decision=decision, thread_id=thread_id, run_id=run_id,
                operator_id=operator_id, requester_id=requester_id,
            )

    async def _send(self, chat_id: str, body: dict) -> None:
        """通过 robotCode 主动发送，失败回退该会话的 session webhook。"""
        try:
            await self._send_active(chat_id, body)
        except Exception as exc:  # noqa: BLE001
            webhook = self._webhooks.get(chat_id)
            if not webhook:
                logger.warning("钉钉会话 %s 主动发送失败且无 session webhook 可回退：%s", chat_id, exc)
                raise
            logger.warning("钉钉会话 %s 主动发送失败，改用 session webhook：%s", chat_id, exc)
            await self._post_webhook(webhook, body)

    async def _send_active(self, chat_id: str, body: dict) -> None:
        """用 access token 调机器人 OpenAPI 主动发送消息。"""
        token = await self._get_token()
        msg_key, msg_param = _robot_payload(body)
        robot_code = self._bot.credentials["client_id"]
        if chat_id in self._single_users:
            url = _SINGLE_SEND_URL
            payload = {"robotCode": robot_code, "userIds": [self._single_users[chat_id]]}
        else:
            url = _GROUP_SEND_URL
            payload = {"robotCode": robot_code, "openConversationId": chat_id}
        payload.update({"msgKey": msg_key, "msgParam": msg_param})
        response = await self._http.post(url, headers={"x-acs-dingtalk-access-token": token}, json=payload)
        response.raise_for_status()

    async def _post_webhook(self, webhook: str, body: dict) -> None:
        """把消息体 POST 到会话 session webhook。"""
        response = await self._http.post(webhook, json=body)
        response.raise_for_status()

    async def _get_token(self) -> str:
        """获取并缓存企业 access token，过期前 5 分钟刷新。"""
        now = time.monotonic()
        if self._token is not None and now < self._token_expire:
            return self._token
        async with self._token_lock:
            now = time.monotonic()
            if self._token is not None and now < self._token_expire:
                return self._token
            response = await self._http.post(
                _ACCESS_TOKEN_URL,
                json={
                    "appKey": self._bot.credentials["client_id"],
                    "appSecret": self._bot.credentials["client_secret"],
                },
            )
            response.raise_for_status()
            data = response.json()
            self._token = str(data["accessToken"])
            self._token_expire = now + float(data.get("expireIn", 7200)) - 300
            return self._token

    async def _upload_media(self, filename: str, content: bytes) -> str:
        """上传文件到钉钉临时素材库并返回 media_id。"""
        token = await self._get_token()
        response = await self._http.post(
            _MEDIA_UPLOAD_URL,
            params={"access_token": token, "type": "file"},
            files={"media": (filename, content)},
        )
        response.raise_for_status()
        data = response.json()
        if data.get("errcode"):
            raise RuntimeError(f"上传钉钉素材失败 errcode={data.get('errcode')} errmsg={data.get('errmsg')}")
        return str(data["media_id"])


def _robot_payload(body: dict) -> tuple[str, str]:
    """把 webhook 风格消息体转换为机器人主动发送的 msgKey / msgParam。

    审批卡片（actionCard）带两个回调按钮，OpenAPI 的 sampleActionCard 模板无法表达，
    这里直接抛错交由 _send 回退 session webhook。
    """
    msgtype = str(body.get("msgtype") or "")
    if msgtype == "text":
        return "sampleText", json.dumps({"content": body["text"]["content"]}, ensure_ascii=False)
    if msgtype == "markdown":
        markdown = body["markdown"]
        return "sampleMarkdown", json.dumps(
            {"title": markdown.get("title", ""), "text": markdown["text"]}, ensure_ascii=False
        )
    if msgtype == "file":
        file_info = body["file"]
        return "sampleFile", json.dumps(
            {"mediaId": file_info["media_id"], "fileName": file_info["file_name"], "fileType": "file"},
            ensure_ascii=False,
        )
    raise ValueError(f"主动发送不支持的消息类型：{msgtype}")


def _strip_at(text: str, message: ChatbotMessage) -> str:
    """去掉钉钉消息前缀中的 @机器人 文本。"""
    at_bot = None
    if isinstance(message.at_users, list):
        for user in message.at_users:
            if getattr(user, "is_bot", False):
                at_bot = getattr(user, "dingtalk_id", None)
    if at_bot:
        return text.replace(f"@{at_bot}", "").strip()
    return text.strip()


def _card_value(content) -> dict:
    """从卡片回调 content 中提取按钮携带的 value。"""
    if isinstance(content, dict) and isinstance(content.get("cardPrivateData"), dict):
        params = content["cardPrivateData"].get("params")
        if isinstance(params, dict):
            return params
    return content if isinstance(content, dict) else {}


class _ChatbotHandler(ChatbotHandler):
    """把 dingtalk-stream 入站消息桥接到 asyncio 主循环。"""

    def __init__(self, adapter: DingTalkAdapter) -> None:
        super().__init__()
        self._adapter = adapter

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        """处理入站消息并返回 ACK。"""
        message = ChatbotMessage.from_dict(callback.data)
        adapter = self._adapter
        adapter._remember(message)
        inbound = adapter._normalize(message)
        if inbound.text:
            adapter._dispatch(adapter._on_inbound(inbound))
        return AckMessage.STATUS_OK, "OK"


class _CardCallbackHandler(CallbackHandler):
    """把钉钉卡片按钮回调桥接到 asyncio 主循环。"""

    def __init__(self, adapter: DingTalkAdapter) -> None:
        super().__init__()
        self._adapter = adapter

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        """解析卡片回调 value 并把审批调度回主 loop。"""
        message = CardCallbackMessage.from_dict(callback.data)
        adapter = self._adapter
        try:
            decision, thread_id, run_id, requester_id = parse_card_action(_card_value(message.content))
        except (ValueError, TypeError, json.JSONDecodeError):
            return AckMessage.STATUS_OK, "OK"
        adapter._dispatch(adapter._resolve(decision, thread_id, run_id, message.user_id, requester_id))
        return AckMessage.STATUS_OK, "OK"
