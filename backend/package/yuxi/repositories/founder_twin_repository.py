"""老板分身经验、咨询与转交的持久化查询。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import ExperienceConsultation, ExperienceEntry, HandoffRequest


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

    async def entry_status_counts(self, twin_slug: str) -> dict[str, int]:
        """按状态统计分身经验条目。"""
        stmt = (
            select(ExperienceEntry.status, func.count())
            .where(ExperienceEntry.twin_slug == twin_slug)
            .group_by(ExperienceEntry.status)
        )
        return {str(status): int(count) for status, count in (await self.db.execute(stmt)).all()}

    async def get_consultation_by_run(self, run_id: str) -> ExperienceConsultation | None:
        """按 Run 读取唯一咨询记录。"""
        stmt = select(ExperienceConsultation).where(ExperienceConsultation.run_id == run_id)
        return (await self.db.scalars(stmt)).one_or_none()

    async def list_consultations(
        self, twin_slug: str, *, uid: str | None = None, limit: int = 100
    ) -> list[ExperienceConsultation]:
        """列出咨询记录；uid 为空时返回分身全部记录。"""
        stmt = select(ExperienceConsultation).where(ExperienceConsultation.twin_slug == twin_slug)
        if uid is not None:
            stmt = stmt.where(ExperienceConsultation.uid == uid)
        stmt = stmt.order_by(ExperienceConsultation.created_at.desc(), ExperienceConsultation.id.desc()).limit(limit)
        return list((await self.db.scalars(stmt)).all())

    async def list_handoffs(self, twin_slug: str, *, uid: str | None = None) -> list[HandoffRequest]:
        """列出转交单；uid 为空时返回分身全部记录。"""
        stmt = select(HandoffRequest).where(HandoffRequest.twin_slug == twin_slug)
        if uid is not None:
            stmt = stmt.where(HandoffRequest.uid == uid)
        stmt = stmt.order_by(HandoffRequest.created_at.desc(), HandoffRequest.id.desc())
        return list((await self.db.scalars(stmt)).all())

    async def get_handoff(self, handoff_id: int) -> HandoffRequest | None:
        """按主键读取转交单。"""
        return await self.db.get(HandoffRequest, handoff_id)

    async def handoff_status_counts(self, twin_slug: str) -> dict[str, int]:
        """按状态统计分身转交单。"""
        stmt = (
            select(HandoffRequest.status, func.count())
            .where(HandoffRequest.twin_slug == twin_slug)
            .group_by(HandoffRequest.status)
        )
        return {str(status): int(count) for status, count in (await self.db.execute(stmt)).all()}
