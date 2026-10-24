import asyncio

from yuxi_im_gateway.adapters.dingtalk import DingTalkAdapter
from yuxi_im_gateway.adapters.feishu import FeishuAdapter
from yuxi_im_gateway.models import BotConfig


def _bot(platform: str) -> BotConfig:
    """构造一个未连接平台的最小机器人配置。"""
    credentials = {"client_id": "c", "client_secret": "s"} if platform == "dingtalk" else {
        "app_id": "a",
        "app_secret": "s",
    }
    return BotConfig(1, platform, "b", credentials, "k", "a")


async def _noop() -> None:
    """空回调，用于验证停止后不再调度。"""


def test_platform_adapters_require_process_restart() -> None:
    """长连接 SDK 无法停止线程，两个平台都必须声明需要重启进程。"""
    assert DingTalkAdapter.RESTART_ON_STOP is True
    assert FeishuAdapter.RESTART_ON_STOP is True


def test_dingtalk_dispatch_ignored_after_stop(monkeypatch) -> None:
    """停止后迟到的回调不应再调度到事件循环。"""
    adapter = DingTalkAdapter(_bot("dingtalk"), on_inbound=lambda msg: _noop())
    asyncio.run(adapter.stop())

    scheduled: list[object] = []
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", lambda coro, loop: scheduled.append(coro))
    coro = _noop()
    try:
        adapter._dispatch(coro)
    finally:
        coro.close()

    assert scheduled == []


def test_feishu_dispatch_ignored_after_stop(monkeypatch) -> None:
    """飞书适配器停止后同样丢弃迟到回调。"""
    adapter = FeishuAdapter(_bot("feishu"), on_inbound=lambda msg: _noop())
    asyncio.run(adapter.stop())

    scheduled: list[object] = []
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", lambda coro, loop: scheduled.append(coro))
    coro = _noop()
    try:
        adapter._dispatch(coro)
    finally:
        coro.close()

    assert scheduled == []
