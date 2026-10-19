"""入站处理与运行编排：去重、白名单、提交、消费、出站、审批。"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from yuxi_im_gateway.dedupe import DedupeCache
from yuxi_im_gateway.events import reduce_run_events
from yuxi_im_gateway.filters import is_allowed, session_chat_id, should_handle
from yuxi_im_gateway.models import BotConfig, InboundMessage

logger = logging.getLogger(__name__)


class SessionQueue:
    """保证同一会话键上的任务串行执行，不同会话键并行。"""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def run(self, key: str, factory: Callable[[], Awaitable[None]]) -> None:
        """在 key 对应的锁内执行工厂返回的协程。"""
        async with self._locks[key]:
            await factory()


def approval_request_id(decision: str, run_id: str) -> str:
    """按 decision 与父 run_id 生成确定性 request_id，用于幂等去重。"""
    return f"{decision}:{run_id}"


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
        return
    if dedupe.seen(f"{bot.id}:{msg.message_id}"):
        return

    chat_id = session_chat_id(bot, msg)

    async def job() -> None:
        try:
            await _run_and_reply(client=client, adapter=adapter, bot=bot, chat_id=chat_id, msg=msg)
        except Exception as exc:  # noqa: BLE001 - 单个会话失败不应中断网关
            logger.warning("处理入站消息失败 bot=%s message=%s: %s", bot.id, msg.message_id, exc)

    await sessions.run(chat_id, job)


async def _run_and_reply(*, client, adapter, bot: BotConfig, chat_id: str, msg: InboundMessage) -> None:
    """提交运行、消费事件并把结果或审批卡片发回平台。"""
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
        await adapter.send_text(chat_id, f"调用失败：{exc}")
        return

    stream_url = _resolve_stream_url(client, submitted)
    if not stream_url:
        await adapter.send_text(chat_id, "运行未启动，请稍后重试")
        return

    outcome = await _consume(client, stream_url, bot.api_key, submitted.get("thread_id"))
    if outcome.status == "approval_required":
        await adapter.send_approval_card(
            chat_id,
            thread_id=str(submitted.get("thread_id") or ""),
            run_id=str(submitted.get("run_id") or ""),
            requester_id=msg.sender_id,
        )
        return
    if outcome.status == "error":
        await adapter.send_text(chat_id, f"运行失败：{outcome.error_message}")
        return

    if outcome.text.strip():
        await adapter.send_markdown(chat_id, outcome.text)
    for path in outcome.artifact_paths:
        await _send_artifact(client, adapter, bot, chat_id, str(submitted.get("thread_id") or ""), path)


def _resolve_stream_url(client, submitted: dict) -> str | None:
    """从提交响应解析实际可订阅的 run stream_url。"""
    return submitted.get("stream_url")


async def _consume(client, stream_url: str, api_key: str, thread_id: str | None):
    """订阅 SSE 并归并结果。"""
    collected: list[dict] = []
    async for batch in client.stream_run(stream_url, api_key):
        collected.extend(batch)
    return reduce_run_events(collected, thread_id=thread_id)


async def _send_artifact(client, adapter, bot: BotConfig, chat_id: str, thread_id: str, path: str) -> None:
    """下载交付物并作为附件发送，失败只记录日志。"""
    try:
        content = await client.download_artifact(thread_id, path, bot.api_key)
        filename = path.rstrip("/").split("/")[-1] or "artifact"
        await adapter.send_file(chat_id, filename, content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("发送交付物失败 bot=%s path=%s: %s", bot.id, path, exc)
        await adapter.send_text(chat_id, f"交付物 {path} 发送失败")


async def resolve_approval(
    *,
    client,
    adapter,
    bot: BotConfig,
    thread_id: str,
    run_id: str,
    decision: str,
    operator_id: str,
    requester_id: str,
) -> None:
    """处理卡片审批回调：仅原发送者有效，提交 decision 并回发最终结果。"""
    try:
        if operator_id != requester_id:
            return
        if decision not in {"approve", "reject"}:
            return

        request_id = approval_request_id(decision, run_id)
        try:
            result = await client.submit_message(
                bot=bot,
                thread_id=thread_id,
                chat_id="",
                sender_id=operator_id,
                message_id=request_id,
                text=f"/{decision}",
                request_id=request_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("提交审批失败 bot=%s run=%s: %s", bot.id, run_id, exc)
            await adapter.send_text(thread_id, f"审批提交失败：{exc}")
            return

        run = result.get("run") if isinstance(result.get("run"), dict) else result
        stream_url = run.get("stream_url") if isinstance(run, dict) else None
        if not stream_url:
            await adapter.send_text(thread_id, "运行未启动，请稍后重试")
            return
        outcome = await _consume(client, stream_url, bot.api_key, thread_id)
        if outcome.status == "completed" and outcome.text.strip():
            await adapter.send_markdown(thread_id, outcome.text)
        elif outcome.status == "error":
            await adapter.send_text(thread_id, f"运行失败：{outcome.error_message}")
    except Exception as exc:  # noqa: BLE001 - 单个会话失败不应中断网关
        logger.warning("处理审批回调失败 bot=%s run=%s: %s", bot.id, run_id, exc)
