"""跨会话用户×Agent 记忆用例：条目查看、删除、开关判定与注入读取。"""

from __future__ import annotations

from yuxi.config.user import UserConfig
from yuxi.repositories.agent_memory_repository import AgentMemoryRepository
from yuxi.storage.postgres.manager import pg_manager


async def list_memories(*, uid: str, agent_slug: str) -> list[dict]:
    """按倒序返回该用户该智能体的记忆条目（倒序由 repository 保证）。"""
    async with pg_manager.get_async_session_context() as db:
        return await AgentMemoryRepository(db).list_by_agent(uid=uid, agent_slug=agent_slug)


async def delete_memory(*, uid: str, agent_slug: str, memory_id: int) -> None:
    """删除单条记忆；未命中抛 ValueError("记忆不存在")，由适配层映射 404。"""
    async with pg_manager.get_async_session_context() as db:
        deleted = await AgentMemoryRepository(db).delete(uid=uid, agent_slug=agent_slug, memory_id=memory_id)
    if not deleted:
        raise ValueError("记忆不存在")


async def is_memory_enabled(uid: str) -> bool:
    """读取用户记忆开关；uid 为空直接视为关闭，不落库。"""
    normalized_uid = str(uid or "").strip()
    if not normalized_uid:
        return False
    async with pg_manager.get_async_session_context() as db:
        config = await UserConfig.load(db, normalized_uid)
    return bool(config.schema.enable_memory)


async def load_injection_entries(*, uid: str, agent_slug: str, limit: int = 20) -> list[str]:
    """读取注入上下文用的记忆内容；开关关闭或 uid 为空时返回空。"""
    if not await is_memory_enabled(uid):
        return []
    items = await list_memories(uid=uid, agent_slug=agent_slug)
    return [item["content"] for item in items[:limit]]
