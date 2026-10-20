"""与 Yuxi HTTP API 的交互：拉取配置、提交消息、消费 SSE、下载附件。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from yuxi_im_gateway.config import GatewaySettings
from yuxi_im_gateway.events import iter_sse_events
from yuxi_im_gateway.models import BotConfig


class YuxiClient:
    """基于 httpx 的 Yuxi 客户端，业务调用使用机器人 API Key。"""

    def __init__(self, settings: GatewaySettings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(base_url=settings.api_base, timeout=httpx.Timeout(30.0, read=None))

    async def aclose(self) -> None:
        """关闭底层连接。"""
        await self._client.aclose()

    async def fetch_bot_configs(self) -> list[BotConfig]:
        """拉取启用中的机器人配置（含解密凭证与 API Key）。"""
        response = await self._client.get(
            "/api/internal/im-gateway/config",
            headers={"X-IM-Gateway-Token": self._settings.gateway_token},
        )
        response.raise_for_status()
        data = response.json().get("data") or []
        return [BotConfig.from_payload(item) for item in data]

    async def submit_message(
        self,
        *,
        bot: BotConfig,
        thread_id: str | None,
        chat_id: str,
        sender_id: str,
        message_id: str,
        text: str,
        request_id: str,
    ) -> dict:
        """向 Channel 入口提交一条文本消息或 slash command。"""
        payload = {
            "channel": bot.platform,
            "account_id": str(bot.id),
            "chat_id": chat_id,
            "thread_id": thread_id,
            "sender_id": sender_id,
            "message_id": message_id,
            "request_id": request_id,
            "agent_slug": bot.agent_slug,
            "message": {"type": "text", "text": text},
        }
        response = await self._client.post(
            "/api/agent-invocation/channel/messages",
            json=payload,
            headers={"Authorization": f"Bearer {bot.api_key}"},
        )
        response.raise_for_status()
        return response.json()

    async def stream_run(self, stream_url: str, api_key: str) -> AsyncIterator[list[dict]]:
        """连接 Run SSE，逐帧产出事件；解析统一复用 iter_sse_events。"""
        headers = {"Authorization": f"Bearer {api_key}"}
        async with self._client.stream(
            "GET", stream_url, headers=headers, params={"verbose": "false"}
        ) as response:
            response.raise_for_status()
            buffer: list[str] = []
            async for line in response.aiter_lines():
                if line:
                    buffer.append(line)
                    continue
                parsed = list(iter_sse_events(buffer))
                buffer = []
                if parsed:
                    yield parsed
            parsed = list(iter_sse_events(buffer))
            if parsed:
                yield parsed

    async def download_artifact(self, thread_id: str, path: str, api_key: str) -> bytes:
        """下载线程交付物原始字节。"""
        virtual = path.lstrip("/")
        response = await self._client.get(
            f"/api/chat/thread/{thread_id}/artifacts/{virtual}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        return response.content
