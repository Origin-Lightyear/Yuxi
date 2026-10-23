"""语音转写客户端：调用自托管 ASR 服务的 HTTP 接口。"""

from __future__ import annotations

import re
import time

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.config.options import asr_host_opts
from yuxi.utils.logging_config import logger

ASR_TIMEOUT_SECONDS = 30
ASR_HEALTH_TIMEOUT_SECONDS = 3

_RICH_TAG_PATTERN = re.compile(r"<\|[^|]*\|>")


def strip_rich_tags(text: str) -> str:
    """去掉 SenseVoice 等模型附加的富文本标签，返回纯文本。"""
    return _RICH_TAG_PATTERN.sub("", text).strip()


class SpeechError(RuntimeError):
    """语音转写失败；reason 取值 unavailable / timeout / empty。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def build_asr_client(server_url: str) -> httpx.AsyncClient:
    """创建指向 ASR 服务的 HTTP 客户端。"""
    return httpx.AsyncClient(base_url=server_url, timeout=ASR_TIMEOUT_SECONDS)


async def _resolve_server_url(server_url: str | None, db: AsyncSession | None) -> str:
    if server_url is None:
        options = await asr_host_opts.get(db)
        server_url = options.get("server_url", "")
    return (server_url or "").strip()


async def transcribe_speech(
    audio: bytes,
    *,
    server_url: str | None = None,
    db: AsyncSession | None = None,
) -> str:
    """把 WAV 音频转写为纯文本，失败时抛出 SpeechError。"""
    resolved_url = await _resolve_server_url(server_url, db)
    if not resolved_url:
        logger.warning("ASR 服务地址未配置，语音转写不可用")
        raise SpeechError("unavailable")

    started = time.perf_counter()
    try:
        async with build_asr_client(resolved_url) as client:
            response = await client.post(
                "/transcribe",
                content=audio,
                headers={"content-type": "audio/wav"},
                timeout=ASR_TIMEOUT_SECONDS,
            )
    except httpx.TimeoutException:
        logger.warning("ASR 转写超时：audio_bytes=%d elapsed_ms=%d", len(audio), _elapsed_ms(started))
        raise SpeechError("timeout") from None
    except httpx.HTTPError as exc:
        logger.warning(
            "ASR 转写请求失败：audio_bytes=%d elapsed_ms=%d error=%s",
            len(audio),
            _elapsed_ms(started),
            type(exc).__name__,
        )
        raise SpeechError("unavailable") from None

    if response.status_code != 200:
        logger.warning(
            "ASR 转写返回非 200：status=%d audio_bytes=%d elapsed_ms=%d",
            response.status_code,
            len(audio),
            _elapsed_ms(started),
        )
        raise SpeechError("unavailable")

    try:
        payload = response.json()
    except ValueError:
        logger.warning("ASR 转写响应不是合法 JSON：audio_bytes=%d", len(audio))
        raise SpeechError("unavailable") from None

    text = strip_rich_tags(str(payload.get("text") or "")) if isinstance(payload, dict) else ""
    if not text:
        logger.warning("ASR 转写结果为空：audio_bytes=%d elapsed_ms=%d", len(audio), _elapsed_ms(started))
        raise SpeechError("empty")

    logger.info("ASR 转写完成：audio_bytes=%d elapsed_ms=%d", len(audio), _elapsed_ms(started))
    return text


async def check_speech_health(db: AsyncSession | None = None) -> bool:
    """探测 ASR 服务健康状态；任何异常都视为不可用。"""
    try:
        resolved_url = await _resolve_server_url(None, db)
        if not resolved_url:
            return False
        async with build_asr_client(resolved_url) as client:
            response = await client.get("/health", timeout=ASR_HEALTH_TIMEOUT_SECONDS)
        return response.status_code == 200
    except Exception as exc:
        logger.debug("ASR 健康检查失败：error=%s", type(exc).__name__)
        return False


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
