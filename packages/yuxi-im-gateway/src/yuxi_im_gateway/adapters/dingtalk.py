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
    ChatbotHandler,
    ChatbotMessage,
)

from yuxi_im_gateway.adapters.base import Adapter
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

    RESTART_ON_STOP = True

    def __init__(self, bot: BotConfig, *, on_inbound: OnInbound) -> None:
        self._bot = bot
        self._on_inbound = on_inbound
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: dingtalk_stream.DingTalkStreamClient | None = None
        self._stopped = False

        # 出站走网关主 loop 上的 httpx 客户端；token 缓存到过期时间。
        self._http: httpx.AsyncClient | None = None
        self._token_lock: asyncio.Lock | None = None
        self._token: str | None = None
        self._token_expire = 0.0

        # 会话级投递信息：session webhook 兜底地址，单聊还需记住对方 staff id。
        self._webhooks: dict[str, str] = {}
        self._single_users: dict[str, str] = {}
        self._chatbot_handler: _ChatbotHandler | None = None

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
        self._chatbot_handler = _ChatbotHandler(self)
        self._client.register_callback_handler(ChatbotMessage.TOPIC, self._chatbot_handler)
        threading.Thread(target=self._client.start_forever, daemon=True).start()

    async def stop(self) -> None:
        """丢弃 HTTP 客户端与 Stream 引用；dingtalk-stream 无干净关闭接口，守护线程由进程退出兜底。"""
        self._stopped = True
        if self._http is not None:
            await self._http.aclose()
        self._http = None
        self._client = None

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
            text=_strip_at(text, message.at_users, bool(message.is_in_at_list)),
        )

    def _remember(self, message: ChatbotMessage) -> None:
        """记录会话投递信息：session webhook 与单聊 staff id。"""
        chat_id = str(message.conversation_id)
        webhook = getattr(message, "session_webhook", None)
        if webhook:
            self._webhooks[chat_id] = str(webhook)
        if str(message.conversation_type) == "1" and message.sender_staff_id:
            self._single_users[chat_id] = str(message.sender_staff_id)

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
        """把消息体 POST 到会话 session webhook，并按响应 errcode 判定成败。

        钉钉 session webhook 过期 / 限流时仍返回 HTTP 200，只靠状态码会把丢包当成功，
        因此必须解析响应体 errcode。

        webhook URL 的查询串带有可向该会话发消息的 session 密钥，httpx 的异常文本会带出
        完整 URL，因此传输层错误只保留状态码、异常类型与响应片段。
        """
        try:
            response = await self._http.post(webhook, json=body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            response = exc.response
            status = response.status_code if response is not None else None
            excerpt = response.text[:200] if response is not None else ""
            raise RuntimeError(
                f"钉钉 session webhook 请求失败 status={status} error={type(exc).__name__} body={excerpt}"
            ) from None
        try:
            data = response.json()
        except ValueError:
            logger.warning("钉钉 session webhook 返回非 JSON 响应：%s", response.text[:200])
            raise RuntimeError("钉钉 session webhook 返回非 JSON 响应") from None
        errcode = data.get("errcode") if isinstance(data, dict) else None
        if errcode:
            errmsg = data.get("errmsg") if isinstance(data, dict) else ""
            logger.warning("钉钉 session webhook 发送失败 errcode=%s errmsg=%s", errcode, errmsg)
            raise RuntimeError(f"钉钉 session webhook 发送失败 errcode={errcode} errmsg={errmsg}")

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
        """上传文件到钉钉临时素材库并返回 media_id。

        access_token 位于查询串，httpx 的状态错误会带出完整 URL，因此这里只把状态码与
        响应片段包装成 RuntimeError，避免调用方记录异常时泄露企业 token。
        """
        token = await self._get_token()
        try:
            response = await self._http.post(
                _MEDIA_UPLOAD_URL,
                params={"access_token": token, "type": "file"},
                files={"media": (filename, content)},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            body = getattr(getattr(exc, "response", None), "text", "") or ""
            raise RuntimeError(f"上传钉钉素材失败 status={status} body={body[:200]}") from None
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


def _strip_at(text: str, at_users, is_in_at_list: bool) -> str:
    """去掉钉钉消息开头的 @机器人 文本；仅在确认 @ 了本机器人时剥离。

    SDK 0.24.3 的 ``AtUser`` 没有 ``is_bot`` 标记，只能退化为按 at_users 里出现的
    ``dingtalk_id`` / ``staff_id`` 前缀匹配，再兜底剥离一个开头的 ``@<token>``。
    """
    text = text.strip()
    if not is_in_at_list:
        return text
    names = [
        str(value)
        for user in (at_users or [])
        for value in (getattr(user, "dingtalk_id", None), getattr(user, "staff_id", None))
        if value
    ]
    for name in names:
        prefix = f"@{name}"
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    if text.startswith("@"):
        parts = text.split(None, 1)
        if len(parts) == 2:
            return parts[1].strip()
    return text


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
        logger.info(
            "钉钉入站消息 chat=%s group=%s mentioned=%s sender=%s text_len=%d",
            inbound.chat_id,
            inbound.is_group,
            inbound.mentions_bot,
            inbound.sender_id,
            len(inbound.text),
        )
        # 群里只 @ 机器人不带正文时也要进入处理，由网关回一条引导提示
        if inbound.text or inbound.mentions_bot:
            adapter._dispatch(adapter._on_inbound(inbound))
        return AckMessage.STATUS_OK, "OK"


