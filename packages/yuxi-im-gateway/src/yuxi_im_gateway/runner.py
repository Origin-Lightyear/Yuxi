"""入站处理与运行编排：去重、白名单、提交、消费、出站。"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

import httpx

from yuxi_im_gateway.dedupe import DedupeCache
from yuxi_im_gateway.events import reduce_run_events
from yuxi_im_gateway.filters import is_allowed, session_chat_id, should_handle
from yuxi_im_gateway.models import BotConfig, InboundMessage

logger = logging.getLogger(__name__)

# IM 会话不提供审批交互，运行仍需审批时只能明确告知用户改走平台内对话。
_APPROVAL_UNSUPPORTED_TEXT = "运行需要工具审批，IM 会话不支持审批，请改用平台内对话"
_BUSY_THREAD_TEXT = "上一条消息还在执行中，请等它结束后再发。"


class SessionQueue:
    """保证同一会话键上的任务串行执行，不同会话键并行。"""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def run(self, key: str, factory: Callable[[], Awaitable[None]]) -> None:
        """在 key 对应的锁内执行工厂返回的协程。"""
        async with self._locks[key]:
            await factory()


async def handle_inbound(
    *,
    client,
    adapter,
    bot: BotConfig,
    dedupe: DedupeCache,
    sessions: SessionQueue,
    msg: InboundMessage,
) -> None:
    """处理一条归一化入站消息：过滤 → 去重 → 提交 → 消费 → 出站。"""
    if not should_handle(bot, msg) or not is_allowed(bot, msg.sender_id):
        logger.info(
            "跳过入站消息 bot=%s message=%s group=%s mentioned=%s",
            bot.id,
            msg.message_id,
            msg.is_group,
            msg.mentions_bot,
        )
        return
    if dedupe.seen(f"{bot.id}:{msg.message_id}"):
        return
    if not msg.text.strip():
        logger.info("入站消息无正文 bot=%s message=%s group=%s", bot.id, msg.message_id, msg.is_group)
        await adapter.send_text(msg.chat_id, "目前只支持文字提问，请直接描述你要查询的问题，例如「查一下大力水手 11 月的应收账款」。")
        return

    chat_id = session_chat_id(bot, msg)
    # 会话键可能带发送者后缀（群内按用户隔离），而平台出站必须用原始会话 id。
    reply_chat_id = msg.chat_id

    async def job() -> None:
        try:
            await _run_and_reply(
                client=client, adapter=adapter, bot=bot, chat_id=chat_id, reply_chat_id=reply_chat_id, msg=msg
            )
        except Exception as exc:  # noqa: BLE001 - 单个会话失败不应中断网关
            logger.warning("处理入站消息失败 bot=%s message=%s: %s", bot.id, msg.message_id, exc)

    await sessions.run(chat_id, job)


async def _run_and_reply(
    *, client, adapter, bot: BotConfig, chat_id: str, reply_chat_id: str, msg: InboundMessage
) -> None:
    """提交运行、消费事件并把结果发回平台会话。"""
    try:
        submitted = await client.submit_message(
            bot=bot,
            thread_id=None,
            chat_id=chat_id,
            sender_id=msg.sender_id,
            message_id=msg.message_id,
            text=msg.text,
            request_id=msg.message_id,
        )
    except Exception as exc:  # noqa: BLE001 - 单个会话失败不应中断网关
        logger.warning("提交消息失败 bot=%s: %s", bot.id, exc)
        await adapter.send_text(reply_chat_id, _submit_error_text(exc))
        return

    stream_url = _resolve_stream_url(submitted)
    if not stream_url:
        await adapter.send_text(reply_chat_id, "运行未启动，请稍后重试")
        return

    outcome = await _consume(client, stream_url, bot.api_key, submitted.get("thread_id"))
    if outcome.status == "approval_required":
        await adapter.send_text(reply_chat_id, _APPROVAL_UNSUPPORTED_TEXT)
        return
    if outcome.status == "error":
        await adapter.send_text(reply_chat_id, f"运行失败：{outcome.error_message}")
        return

    await _deliver(client, adapter, bot, reply_chat_id, str(submitted.get("thread_id") or ""), outcome)


def _submit_error_text(exc: Exception) -> str:
    """把提交阶段的 HTTP 错误转成用户可读提示，避免把 httpx 异常原文发到会话。"""
    response = getattr(exc, "response", None)
    if isinstance(response, httpx.Response) and response.status_code == 409:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = None
        if isinstance(detail, dict) and detail.get("code") == "run_active":
            return _BUSY_THREAD_TEXT
    return f"调用失败：{exc}"


def _resolve_stream_url(submitted: dict) -> str | None:
    """从提交响应解析实际可订阅的 run stream_url。"""
    return submitted.get("stream_url")


async def _consume(client, stream_url: str, api_key: str, thread_id: str | None):
    """订阅 SSE 并归并结果。"""
    collected: list[dict] = []
    async for batch in client.stream_run(stream_url, api_key):
        collected.extend(batch)
    return reduce_run_events(collected, thread_id=thread_id)


async def _deliver(client, adapter, bot: BotConfig, chat_id: str, thread_id: str, outcome) -> None:
    """发送运行正文与全部交付物；单个交付物失败不阻断其余交付物。"""
    if outcome.text.strip():
        await adapter.send_markdown(chat_id, outcome.text)
    for path in outcome.artifact_paths:
        await _send_artifact(client, adapter, bot, chat_id, thread_id, path)


async def _send_artifact(client, adapter, bot: BotConfig, chat_id: str, thread_id: str, path: str) -> None:
    """下载交付物并作为附件发送，失败只记录日志。"""
    try:
        content = await client.download_artifact(thread_id, path, bot.api_key)
        filename = path.rstrip("/").split("/")[-1] or "artifact"
        await adapter.send_file(chat_id, filename, content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("发送交付物失败 bot=%s path=%s: %s", bot.id, path, exc)
        await adapter.send_text(chat_id, f"交付物 {path} 发送失败")
