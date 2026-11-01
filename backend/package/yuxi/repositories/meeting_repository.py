"""分身会议与数字员工会议的持久化查询。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import (
    Meeting,
    MeetingDecision,
    MeetingDecisionSigner,
    MeetingMessage,
)


class MeetingRepository:
    """集中会议主表、发言、决议和签字人的读取。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_meeting(self, **values) -> Meeting:
        """写入会议主记录并返回带主键的对象。"""
        meeting = Meeting(**values)
        self.db.add(meeting)
        await self.db.flush()
        return meeting

    async def get_meeting(self, meeting_id: int, *, for_update: bool = False) -> Meeting | None:
        """按主键读取会议；生成纪要时使用行锁避免重复执行。"""
        stmt = select(Meeting).where(Meeting.id == meeting_id)
        if for_update:
            stmt = stmt.execution_options(populate_existing=True).with_for_update()
        return await self.db.scalar(stmt)

    async def list_meetings(self, *, kind: str | None = None, limit: int = 100) -> list[Meeting]:
        """按类型列出最近会议；可见性由 service 统一裁剪。"""
        stmt = select(Meeting)
        if kind:
            stmt = stmt.where(Meeting.kind == kind)
        stmt = stmt.order_by(Meeting.updated_at.desc(), Meeting.id.desc()).limit(max(1, min(limit, 200)))
        return list((await self.db.scalars(stmt)).all())

    async def add_message(self, **values) -> MeetingMessage:
        """追加一条会议发言。"""
        message = MeetingMessage(**values)
        self.db.add(message)
        await self.db.flush()
        return message

    async def list_messages(self, meeting_id: int) -> list[MeetingMessage]:
        """按写入顺序返回会议发言。"""
        stmt = (
            select(MeetingMessage)
            .where(MeetingMessage.meeting_id == meeting_id)
            .order_by(MeetingMessage.created_at.asc(), MeetingMessage.id.asc())
        )
        return list((await self.db.scalars(stmt)).all())

    async def add_decision(self, **values) -> MeetingDecision:
        """写入一条会议决议。"""
        decision = MeetingDecision(**values)
        self.db.add(decision)
        await self.db.flush()
        return decision

    async def get_decision(self, decision_id: int, *, for_update: bool = False) -> MeetingDecision | None:
        """按主键读取决议；签字时锁定决议行串行化状态变更。"""
        stmt = select(MeetingDecision).where(MeetingDecision.id == decision_id)
        if for_update:
            stmt = stmt.execution_options(populate_existing=True).with_for_update()
        return await self.db.scalar(stmt)

    async def list_decisions(self, meeting_id: int) -> list[MeetingDecision]:
        """按创建顺序返回会议决议。"""
        stmt = (
            select(MeetingDecision)
            .where(MeetingDecision.meeting_id == meeting_id)
            .order_by(MeetingDecision.created_at.asc(), MeetingDecision.id.asc())
        )
        return list((await self.db.scalars(stmt)).all())

    async def add_signer(self, **values) -> MeetingDecisionSigner:
        """写入决议签字人。"""
        signer = MeetingDecisionSigner(**values)
        self.db.add(signer)
        await self.db.flush()
        return signer

    async def get_signer(self, decision_id: int, uid: str) -> MeetingDecisionSigner | None:
        """读取某个决议下指定签字人的记录。"""
        stmt = select(MeetingDecisionSigner).where(
            MeetingDecisionSigner.decision_id == decision_id,
            MeetingDecisionSigner.uid == uid,
        )
        return await self.db.scalar(stmt)

    async def list_signers(self, decision_ids: list[int]) -> list[MeetingDecisionSigner]:
        """一次读取多条决议的签字人。"""
        if not decision_ids:
            return []
        stmt = (
            select(MeetingDecisionSigner)
            .where(MeetingDecisionSigner.decision_id.in_(decision_ids))
            .order_by(MeetingDecisionSigner.id.asc())
        )
        return list((await self.db.scalars(stmt)).all())
