"""老板分身经验、咨询与转交的持久化查询。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import ExperienceEntry


class FounderTwinRepository:
    """集中分身相关表的读取，避免 service 直接拼 SQL。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_entry(self, entry_id: int) -> ExperienceEntry | None:
        """按主键读取经验条目。"""
        return await self.db.get(ExperienceEntry, entry_id)

    async def list_entries(self, twin_slug: str, *, status: str | None = None) -> list[ExperienceEntry]:
        """按分身和可选状态列出经验，新的在前。"""
        stmt = select(ExperienceEntry).where(ExperienceEntry.twin_slug == twin_slug)
        if status:
            stmt = stmt.where(ExperienceEntry.status == status)
        stmt = stmt.order_by(ExperienceEntry.created_at.desc(), ExperienceEntry.id.desc())
        return list((await self.db.scalars(stmt)).all())

    async def list_retrievable(self, twin_slug: str, *, limit: int) -> list[ExperienceEntry]:
        """只取已确认且有 embedding 的条目。"""
        stmt = (
            select(ExperienceEntry)
            .where(
                ExperienceEntry.twin_slug == twin_slug,
                ExperienceEntry.status == "confirmed",
                ExperienceEntry.embedding.is_not(None),
            )
            .order_by(ExperienceEntry.id.asc())
            .limit(limit)
        )
        return list((await self.db.scalars(stmt)).all())

    async def get_entries_by_ids(self, entry_ids: list[int]) -> list[ExperienceEntry]:
        """一次读取候选引用条目。"""
        if not entry_ids:
            return []
        stmt = select(ExperienceEntry).where(ExperienceEntry.id.in_(entry_ids))
        return list((await self.db.scalars(stmt)).all())

    async def count_entries(self, twin_slug: str) -> int:
        """统计分身全部经验条目，用于 2,000 条上限。"""
        stmt = select(func.count()).select_from(ExperienceEntry).where(ExperienceEntry.twin_slug == twin_slug)
        return int((await self.db.scalar(stmt)) or 0)
