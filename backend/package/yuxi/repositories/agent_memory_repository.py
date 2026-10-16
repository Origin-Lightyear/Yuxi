"""跨会话 Agent 记忆的 PostgreSQL 访问边界。"""

from __future__ import annotations

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import AgentMemory


class AgentMemoryRepository:
    """读写用户×Agent 作用域内的记忆，所有方法强制带 uid 过滤。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def list_by_agent(self, *, uid: str, agent_slug: str) -> list[dict]:
        """按时间倒序返回该用户该 Agent 的全部记忆。"""
        result = await self.db.execute(
            select(AgentMemory)
            .where(AgentMemory.uid == uid, AgentMemory.agent_slug == agent_slug)
            .order_by(AgentMemory.created_at.desc(), AgentMemory.id.desc())
        )
        return [row.to_dict() for row in result.scalars().all()]

    async def add(self, *, uid: str, agent_slug: str, content: str, source_run_id: str) -> dict:
        """写入一条记忆；source_run_id 冲突时上抛唯一约束异常，由调用方跳过该条。"""
        memory = AgentMemory(uid=uid, agent_slug=agent_slug, content=content, source_run_id=source_run_id)
        self.db.add(memory)
        await self.db.flush()
        return memory.to_dict()

    async def count_by_agent(self, *, uid: str, agent_slug: str) -> int:
        """统计该用户该 Agent 的记忆条数。"""
        count = await self.db.scalar(
            select(func.count())
            .select_from(AgentMemory)
            .where(AgentMemory.uid == uid, AgentMemory.agent_slug == agent_slug)
        )
        return int(count or 0)

    async def delete_oldest_over_limit(self, *, uid: str, agent_slug: str, limit: int = 50) -> int:
        """删除超出容量上限的最旧记忆，返回删除条数。"""
        excess_ids = (
            select(AgentMemory.id)
            .where(and_(AgentMemory.uid == uid, AgentMemory.agent_slug == agent_slug))
            .order_by(AgentMemory.created_at.desc(), AgentMemory.id.desc())
            .offset(limit)
            .scalar_subquery()
        )
        result = await self.db.execute(delete(AgentMemory).where(AgentMemory.id.in_(excess_ids)))
        return int(result.rowcount or 0)

    async def delete(self, *, uid: str, agent_slug: str, memory_id: int) -> bool:
        """按 uid、agent_slug、id 三重过滤硬删，返回是否命中。"""
        result = await self.db.execute(
            delete(AgentMemory).where(
                and_(
                    AgentMemory.id == memory_id,
                    AgentMemory.uid == uid,
                    AgentMemory.agent_slug == agent_slug,
                )
            )
        )
        return (result.rowcount or 0) > 0
