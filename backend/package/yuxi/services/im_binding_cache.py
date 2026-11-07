"""IM 平台身份到 Yuxi 用户的短期映射缓存。

Tenant 是绑定关系的唯一保存方；Yuxi 只在 Redis 缓存一份 TTL 与 Tenant
AgentSession 对齐的映射，使 IM 消息在 TTL 内不必每条都调用 Tenant。
缓存只保存 uid 与平台用户 ID，不保存任何 Token 或凭据。
"""

from __future__ import annotations

from yuxi.services.im_bot_service import PLATFORMS
from yuxi.storage.redis import get_async_redis_client

KEY_PREFIX = "saas:im-binding"


def _text(value: object) -> str:
    """把 Redis 返回值统一成字符串。"""
    return value.decode() if isinstance(value, bytes) else str(value)


def _forward_key(platform: str, channel_user_id: str) -> str:
    """平台身份 → uid。"""
    return f"{KEY_PREFIX}:{platform}:{channel_user_id}"


def _reverse_key(uid: str, platform: str) -> str:
    """uid → 平台身份，供解绑与登出时清理正向缓存。"""
    return f"{KEY_PREFIX}:uid:{uid}:{platform}"


async def bind_uid(platform: str, channel_user_id: str) -> str | None:
    """读取平台身份对应的 Yuxi uid；未命中返回 None。"""
    redis = await get_async_redis_client()
    raw = await redis.get(_forward_key(platform, channel_user_id))
    return _text(raw) if raw else None


async def remember_binding(*, uid: str, platform: str, channel_user_id: str, ttl_seconds: int) -> None:
    """写入正向与反向映射，TTL 取 Tenant AgentSession 剩余有效秒数。"""
    redis = await get_async_redis_client()
    ttl = max(1, int(ttl_seconds))
    await redis.set(_forward_key(platform, channel_user_id), uid, ex=ttl)
    await redis.set(_reverse_key(uid, platform), channel_user_id, ex=ttl)


async def forget_binding(*, uid: str, platform: str) -> None:
    """删除某用户在某平台的映射；没有反向键时只清理可能残留的反向键。"""
    redis = await get_async_redis_client()
    reverse = _reverse_key(uid, platform)
    channel_user_id = await redis.get(reverse)
    if channel_user_id:
        await redis.delete(_forward_key(platform, _text(channel_user_id)))
    await redis.delete(reverse)


async def forget_user_bindings(uid: str) -> None:
    """删除某用户全部支持平台的映射。"""
    for platform in sorted(PLATFORMS):
        await forget_binding(uid=uid, platform=platform)
