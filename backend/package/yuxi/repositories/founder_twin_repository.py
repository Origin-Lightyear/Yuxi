"""老板分身经验、咨询与转交的持久化查询。"""

from __future__ import annotations

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import (
    Conversation,
    ExperienceConsultation,
    ExperienceEntry,
    HandoffRequest,
    IMBotConfig,
    Meeting,
    MeetingDecision,
    ScheduledAgentJob,
    User,
)


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

    async def list_retrievable(
        self, twin_slug: str, *, limit: int, embedding_model: str
    ) -> list[ExperienceEntry]:
        """只取已确认、有 embedding 且与当前查询模型同空间的条目。"""
        stmt = (
            select(ExperienceEntry)
            .where(
                ExperienceEntry.twin_slug == twin_slug,
                ExperienceEntry.status == "confirmed",
                ExperienceEntry.embedding.is_not(None),
                ExperienceEntry.embedding_model == embedding_model,
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
        stmt = (
            select(func.count())
            .select_from(ExperienceEntry)
            .where(
                ExperienceEntry.twin_slug == twin_slug,
                ExperienceEntry.status != "invalid",
            )
        )
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
            stmt = stmt.where(
                or_(HandoffRequest.uid == uid, HandoffRequest.assigned_to_uid == uid)
            )
        stmt = stmt.order_by(HandoffRequest.created_at.desc(), HandoffRequest.id.desc())
        return list((await self.db.scalars(stmt)).all())

    async def get_handoff(self, handoff_id: int) -> HandoffRequest | None:
        """按主键读取转交单。"""
        return await self.db.get(HandoffRequest, handoff_id)

    async def get_user_names(self, uids: list[str]) -> dict[str, str]:
        """批量读取转交发起人的展示名。"""
        if not uids:
            return {}
        stmt = select(User.uid, User.username).where(User.uid.in_(uids))
        return {str(uid): str(username) for uid, username in (await self.db.execute(stmt)).all()}

    async def handoff_status_counts(self, twin_slug: str) -> dict[str, int]:
        """按状态统计分身转交单。"""
        stmt = (
            select(HandoffRequest.status, func.count())
            .where(HandoffRequest.twin_slug == twin_slug)
            .group_by(HandoffRequest.status)
        )
        return {str(status): int(count) for status, count in (await self.db.execute(stmt)).all()}

    async def count_references(self, twin_slug: str) -> dict[str, int]:
        """统计阻止删除分身的持久引用。"""
        counts: dict[str, int] = {}
        for name, model, column in (
            ("experiences", ExperienceEntry, ExperienceEntry.twin_slug),
            ("consultations", ExperienceConsultation, ExperienceConsultation.twin_slug),
            ("handoffs", HandoffRequest, HandoffRequest.twin_slug),
            ("conversations", Conversation, Conversation.agent_id),
            ("scheduled_jobs", ScheduledAgentJob, ScheduledAgentJob.agent_slug),
            ("im_bots", IMBotConfig, IMBotConfig.agent_slug),
        ):
            result = await self.db.execute(select(func.count()).select_from(model).where(column == twin_slug))
            counts[name] = int(result.scalar_one() or 0)

        escaped_twin_slug = twin_slug.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        meeting_result = await self.db.execute(
            select(func.count())
            .select_from(Meeting)
            .where(
                cast(Meeting.participant_slugs, String).like(
                    f'%"{escaped_twin_slug}"%',
                    escape="\\",
                )
            )
        )
        counts["meetings"] = int(meeting_result.scalar_one() or 0)
        decision_result = await self.db.execute(
            select(func.count())
            .select_from(MeetingDecision)
            .where(MeetingDecision.proposer_slug == twin_slug)
        )
        counts["decisions"] = int(decision_result.scalar_one() or 0)
        return counts
