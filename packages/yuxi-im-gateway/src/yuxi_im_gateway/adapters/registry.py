"""按平台构建适配器，并提供配置指纹用于热更新比对。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from yuxi_im_gateway.adapters.base import Adapter
from yuxi_im_gateway.models import BotConfig, InboundMessage

OnInbound = Callable[[InboundMessage], Awaitable[None]]


def adapter_fingerprint(bot: BotConfig) -> str:
    """生成能反映连接关键配置的指纹，用于判断是否需要重建连接。"""
    return json.dumps(
        {
            "platform": bot.platform,
            "credentials": bot.credentials,
            "agent_slug": bot.agent_slug,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def build_adapter(bot: BotConfig, *, on_inbound: OnInbound) -> Adapter:
    """按平台构建适配器实例。"""
    if bot.platform == "feishu":
        from yuxi_im_gateway.adapters.feishu import FeishuAdapter

        return FeishuAdapter(bot, on_inbound=on_inbound)
    if bot.platform == "dingtalk":
        from yuxi_im_gateway.adapters.dingtalk import DingTalkAdapter

        return DingTalkAdapter(bot, on_inbound=on_inbound)
    raise ValueError(f"不支持的平台：{bot.platform}")
