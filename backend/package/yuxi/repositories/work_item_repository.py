"""跨线程待办投影：每线程最新顶层 Run 的只读查询。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import Agent, AgentRun, Conversation, Message, WorkItemView
from yuxi.utils.datetime_utils import utc_now_naive

TOP_LEVEL_RUN_TYPES = ("chat", "resume")
EXCLUDED_SOURCES = ("agent_call", "agent_evaluation")
APPROVAL_ERROR_TYPES = ("human_approval_required", "ask_user_question_required")


@dataclass(frozen=True)
class WorkItemRow:
    """投影后的 Run 事实；work_status 是面向待办页的派生状态。"""

    run_id: str
    thread_id: str
    conversation_id: int | None
    agent_slug: str | None
    agent_name: str | None
    title: str | None
    input_text: str | None
    source: str | None
    run_status: str
    work_status: str
    error_type: str | None
    created_at: datetime
    updated_at: datetime


class WorkItemRepository:
    """用窗口函数投影待办，不持有 Run 状态。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    @staticmethod
    def _status_expression():
        return case(
            (AgentRun.status == "pending", "queued"),
            (AgentRun.status.in_(("running", "cancel_requested")), "running"),
            (
                and_(
                    AgentRun.status == "interrupted",
                    AgentRun.error_type.in_(APPROVAL_ERROR_TYPES),
                ),
                "waiting_approval",
            ),
            (AgentRun.status == "interrupted", "failed"),
            else_=AgentRun.status,
        )

    def _latest_subquery(self, uid: str):
        status_expression = self._status_expression()
        return (
            select(
                AgentRun.id.label("run_id"),
                AgentRun.conversation_thread_id.label("thread_id"),
                AgentRun.conversation_id,
                AgentRun.agent_slug,
                AgentRun.source,
                AgentRun.status.label("run_status"),
                status_expression.label("work_status"),
                AgentRun.error_type,
                AgentRun.created_at,
                AgentRun.updated_at,
                AgentRun.input_message_id,
                func.row_number()
                .over(
                    partition_by=AgentRun.conversation_thread_id,
                    order_by=(AgentRun.created_at.desc(), AgentRun.id.desc()),
                )
                .label("rn"),
            )
            .where(AgentRun.uid == uid)
            .where(AgentRun.run_type.in_(TOP_LEVEL_RUN_TYPES))
            .where(or_(AgentRun.source.is_(None), AgentRun.source.notin_(EXCLUDED_SOURCES)))
            .subquery("latest_work_runs")
        )

    @staticmethod
    def _page_statement(latest):
        return (
            select(
                latest,
                Conversation.title.label("conversation_title"),
                Message.content.label("input_text"),
                Agent.name.label("agent_name"),
            )
            .select_from(latest)
            .outerjoin(Conversation, Conversation.thread_id == latest.c.thread_id)
            .outerjoin(Message, Message.id == latest.c.input_message_id)
            .outerjoin(Agent, Agent.slug == latest.c.agent_slug)
            .where(latest.c.rn == 1)
        )

    @staticmethod
    def _to_row(row) -> WorkItemRow:
        created_at = row.created_at or utc_now_naive()
        return WorkItemRow(
            run_id=row.run_id,
            thread_id=row.thread_id,
            conversation_id=row.conversation_id,
            agent_slug=row.agent_slug,
            agent_name=row.agent_name,
            title=row.conversation_title,
            input_text=row.input_text,
            source=row.source,
            run_status=row.run_status,
            work_status=row.work_status,
            error_type=row.error_type,
            created_at=created_at,
            updated_at=row.updated_at or created_at,
        )

    async def list_latest(
        self,
        uid: str,
        *,
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[WorkItemRow], int]:
        """按派生状态筛选并分页，返回当前用户每线程最新顶层 Run。"""
        latest = self._latest_subquery(uid)
        count_stmt = select(func.count()).select_from(latest).where(latest.c.rn == 1)
        if status:
            count_stmt = count_stmt.where(latest.c.work_status == status)
        total = int((await self.db.scalar(count_stmt)) or 0)

        stmt = self._page_statement(latest)
        if status:
            stmt = stmt.where(latest.c.work_status == status)
        stmt = (
            stmt.order_by(
                func.coalesce(latest.c.updated_at, latest.c.created_at).desc(),
                latest.c.run_id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return [self._to_row(row) for row in result.all()], total

    async def get_latest_run(self, uid: str, run_id: str) -> WorkItemRow | None:
        """读取仍属于线程最新 Run 的待办；过期 Run 返回 None。"""
        latest = self._latest_subquery(uid)
        stmt = self._page_statement(latest).where(latest.c.run_id == run_id)
        row = (await self.db.execute(stmt)).first()
        return self._to_row(row) if row else None

    async def summary(self, uid: str) -> dict[str, int]:
        """按派生状态统计当前用户的全部待办。"""
        latest = self._latest_subquery(uid)
        stmt = (
            select(latest.c.work_status, func.count())
            .select_from(latest)
            .where(latest.c.rn == 1)
            .group_by(latest.c.work_status)
        )
        result = await self.db.execute(stmt)
        return {str(status): int(count) for status, count in result.all()}

    async def unread_count(self, uid: str) -> int:
        """统计当前用户尚未标记已读的待办数量。"""
        latest = self._latest_subquery(uid)
        stmt = (
            select(func.count())
            .select_from(latest)
            .outerjoin(
                WorkItemView,
                and_(
                    WorkItemView.uid == uid,
                    WorkItemView.item_key == literal("run:") + latest.c.run_id,
                ),
            )
            .where(latest.c.rn == 1, WorkItemView.id.is_(None))
        )
        return int((await self.db.scalar(stmt)) or 0)

    async def viewed_keys(self, uid: str, keys: list[str]) -> set[str]:
        """一次查询返回给定待办 key 中已读的集合。"""
        if not keys:
            return set()
        stmt = select(WorkItemView.item_key).where(
            WorkItemView.uid == uid,
            WorkItemView.item_key.in_(keys),
        )
        return set((await self.db.scalars(stmt)).all())

    async def mark_viewed(self, uid: str, key: str) -> None:
        """幂等标记已读；PostgreSQL 用唯一约束原子 upsert。"""
        now = utc_now_naive()
        if self.db.get_bind().dialect.name == "postgresql":
            stmt = (
                pg_insert(WorkItemView)
                .values(uid=uid, item_key=key, viewed_at=now)
                .on_conflict_do_update(
                    index_elements=[WorkItemView.uid, WorkItemView.item_key],
                    set_={"viewed_at": now},
                )
            )
            await self.db.execute(stmt)
            return

        existing = await self.db.scalar(
            select(WorkItemView).where(WorkItemView.uid == uid, WorkItemView.item_key == key)
        )
        if existing is None:
            self.db.add(WorkItemView(uid=uid, item_key=key, viewed_at=now))
        else:
            existing.viewed_at = now
        await self.db.flush()
