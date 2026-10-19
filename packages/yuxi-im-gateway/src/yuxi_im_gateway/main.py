"""网关主程序：加载机器人、维护连接、周期性同步配置。"""

from __future__ import annotations

import asyncio
import logging
import signal

from yuxi_im_gateway.adapters.base import Adapter
from yuxi_im_gateway.adapters.registry import adapter_fingerprint, build_adapter
from yuxi_im_gateway.config import GatewaySettings
from yuxi_im_gateway.dedupe import DedupeCache
from yuxi_im_gateway.models import BotConfig, InboundMessage
from yuxi_im_gateway.runner import SessionQueue, handle_inbound, resolve_approval
from yuxi_im_gateway.yuxi_client import YuxiClient

logger = logging.getLogger(__name__)


class GatewayRunner:
    """持有全部机器人适配器，负责配置同步与热更新。"""

    def __init__(self, client: YuxiClient, settings: GatewaySettings) -> None:
        self._client = client
        self._settings = settings
        self._adapters: dict[int, tuple[str, Adapter]] = {}
        self._bots: dict[int, BotConfig] = {}
        self._dedupe = DedupeCache()
        self._sessions = SessionQueue()
        self._stopping = asyncio.Event()

    async def reconcile(self, configs: list[BotConfig]) -> None:
        """按最新配置增删改机器人连接，未变化的保持不动。"""
        desired = {bot.id: bot for bot in configs}
        for bot_id in list(self._adapters.keys()):
            bot = desired.get(bot_id)
            if bot is None or adapter_fingerprint(bot) != self._adapters[bot_id][0]:
                await self._stop_bot(bot_id)
        for bot_id, bot in desired.items():
            if bot_id not in self._adapters:
                await self._start_bot(bot)
        self._bots = desired

    async def _start_bot(self, bot: BotConfig) -> None:
        """启动单个机器人适配器，失败只记录日志。"""
        try:
            adapter = build_adapter(bot, on_inbound=lambda msg, b=bot: self._on_inbound(b, msg))
            self._attach_approval(adapter, bot)
            await adapter.start()
            self._adapters[bot.id] = (adapter_fingerprint(bot), adapter)
            logger.info("机器人已启动 id=%s platform=%s", bot.id, bot.platform)
        except Exception as exc:
            logger.error("机器人启动失败 id=%s: %s", bot.id, exc)

    async def _stop_bot(self, bot_id: int) -> None:
        """停止并移除单个机器人适配器。"""
        entry = self._adapters.pop(bot_id, None)
        if entry is None:
            return
        try:
            await entry[1].stop()
        except Exception as exc:
            logger.warning("机器人停止失败 id=%s: %s", bot_id, exc)

    def _attach_approval(self, adapter: Adapter, bot: BotConfig) -> None:
        """给适配器注入审批回调，把卡片操作转交编排层。"""

        async def on_approval(*, decision, thread_id, run_id, operator_id, requester_id, chat_id):
            await resolve_approval(
                client=self._client,
                adapter=adapter,
                bot=bot,
                thread_id=thread_id,
                run_id=run_id,
                decision=decision,
                operator_id=operator_id,
                requester_id=requester_id,
                chat_id=chat_id,
            )

        adapter.approval_handler = on_approval

    async def _on_inbound(self, bot: BotConfig, msg: InboundMessage) -> None:
        """入站消息入口，交给编排层；适配器已被配置轮询移除时直接丢弃。"""
        entry = self._adapters.get(bot.id)
        if entry is None:
            logger.debug("机器人已移除，丢弃入站消息 bot=%s message=%s", bot.id, msg.message_id)
            return
        await handle_inbound(
            client=self._client,
            adapter=entry[1],
            bot=bot,
            dedupe=self._dedupe,
            sessions=self._sessions,
            msg=msg,
        )

    async def run(self) -> None:
        """主循环：启动后周期拉取配置并同步。"""
        while not self._stopping.is_set():
            try:
                configs = await self._client.fetch_bot_configs()
                await self.reconcile(configs)
            except Exception as exc:
                logger.error("同步机器人配置失败: %s", exc)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._settings.poll_interval)
            except TimeoutError:
                continue

    def request_stop(self) -> None:
        """请求优雅退出。"""
        self._stopping.set()


async def _amain() -> None:
    """阻塞运行网关，收到退出信号后关闭客户端。"""
    logging.basicConfig(level=logging.INFO)
    settings = GatewaySettings.from_env()
    if not settings.gateway_token:
        raise SystemExit("IM_GATEWAY_TOKEN 未配置")
    client = YuxiClient(settings)
    runner = GatewayRunner(client, settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, runner.request_stop)
    try:
        await runner.run()
    finally:
        await client.aclose()


def main() -> None:
    """同步入口（脚本与 `python -m`）。"""
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
