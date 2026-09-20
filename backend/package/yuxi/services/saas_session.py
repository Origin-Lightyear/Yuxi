"""SaaS AgentSession 的跨进程短期存储。"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field

from yuxi.storage.redis import get_async_redis_client

SESSION_KEY_PREFIX = "saas:agent-session:"


@dataclass(frozen=True, slots=True)
class SaasAgentSession:
    """当前用户选定租户的 Tenant 会话。"""

    token: str = field(repr=False)
    expires_at: str
    tenant_id: int
    employee_id: int
    mcp_instance_id: str


def _session_key(uid: str) -> str:
    return f"{SESSION_KEY_PREFIX}{uid}"


def get_saas_session_ttl_seconds(expires_at: str) -> int:
    """计算 Tenant AgentSession 的剩余有效秒数。"""
    from datetime import datetime

    try:
        return max(1, int(float(expires_at) - time.time()))
    except ValueError:
        pass

    normalized = expires_at.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        expires_epoch = parsed.timestamp()
    else:
        expires_epoch = parsed.timestamp()
    return max(1, int(expires_epoch - time.time()))


async def save_saas_agent_session(uid: str, session: SaasAgentSession) -> None:
    """保存所选租户 Session，并让 Redis TTL 与 Tenant 过期时间一致。"""
    redis = await get_async_redis_client()
    await redis.set(
        _session_key(uid),
        json.dumps(asdict(session), ensure_ascii=False),
        ex=get_saas_session_ttl_seconds(session.expires_at),
    )


async def get_saas_agent_session(uid: str) -> SaasAgentSession | None:
    """读取当前用户的有效 Tenant Session。"""
    redis = await get_async_redis_client()
    raw = await redis.get(_session_key(uid))
    if not raw:
        return None
    data = json.loads(raw)
    return SaasAgentSession(
        token=str(data["token"]),
        expires_at=str(data["expires_at"]),
        tenant_id=int(data["tenant_id"]),
        employee_id=int(data["employee_id"]),
        mcp_instance_id=str(data["mcp_instance_id"]),
    )


async def clear_saas_agent_session(uid: str) -> None:
    """删除当前用户的 Tenant Session。"""
    redis = await get_async_redis_client()
    await redis.delete(_session_key(uid))
