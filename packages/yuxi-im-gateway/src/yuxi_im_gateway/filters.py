"""入站消息的白名单、@ 判定与会话键计算。"""

from __future__ import annotations

from yuxi_im_gateway.models import BotConfig, InboundMessage


def is_allowed(bot: BotConfig, sender_id: str) -> bool:
    """白名单为空表示允许所有人；否则发送者必须命中。"""
    if not bot.allowed_users:
        return True
    return sender_id in bot.allowed_users


def should_handle(bot: BotConfig, msg: InboundMessage) -> bool:
    """判断是否应处理该消息：私聊直接处理，群聊按 @ 规则。"""
    if not msg.is_group:
        return True
    if not bot.require_mention:
        return True
    return msg.mentions_bot


def session_chat_id(bot: BotConfig, msg: InboundMessage) -> str:
    """计算传给 Channel 的 chat_id，群内按用户隔离时拼接发送者。"""
    if msg.is_group and bot.group_sessions_per_user:
        return f"{msg.chat_id}:{msg.sender_id}"
    return msg.chat_id
