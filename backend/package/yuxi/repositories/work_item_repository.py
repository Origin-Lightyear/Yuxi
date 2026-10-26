"""跨线程待办投影：每线程最新顶层 Run 与老板分身转交单的只读查询。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import String, and_, case, cast, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import (
    Agent,
    AgentRun,
    Conversation,
    HandoffRequest,
    Message,
    User,
    WorkItemView,
)
from yuxi.utils.datetime_utils import utc_now_naive

TOP_LEVEL_RUN_TYPES = ("chat", "resume")
EXCLUDED_SOURCES = ("agent_call", "agent_evaluation")
APPROVAL_ERROR_TYPES = ("human_approval_required", "ask_user_question_required")
@dataclass(frozen=True)
class WorkItemRow:
    """投影后的统一待办行；work_status 是面向待办页的派生状态。"""

    source_type: str
    source_id: str
    run_id: str | None
    thread_id: str | None
    conversation_id: int | None
    agent_slug: str | None
    agent_name: str | None
    title: str | None
    input_text: str | None
    source: str | None
    run_status: str
    work_status: str
    error_type: str | None
    owner_uid: str
    initiator_name: str | None
    created_at: datetime
    updated_at: datetime


class WorkItemRepository:
    """用窗口函数投影 Run，并与老板分身转交单做统一只读视图。"""

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
                AgentRun.uid,
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

    def _run_select(self, uid: str):
        latest = self._latest_subquery(uid)
        return (
            select(
                literal("agent_run").label("source_type"),
                latest.c.run_id.label("source_id"),
                latest.c.run_id,
                latest.c.thread_id,
                latest.c.conversation_id,
                latest.c.agent_slug,
                Agent.name.label("agent_name"),
                Conversation.title.label("title"),
                Message.content.label("input_text"),
                latest.c.source,
                latest.c.run_status,
                latest.c.work_status,
                latest.c.error_type,
                latest.c.uid.label("owner_uid"),
                literal(None, String).label("initiator_name"),
                latest.c.created_at,
                latest.c.updated_at,
                (literal("run:") + latest.c.run_id).label("item_key"),
            )
            .select_from(latest)
            .outerjoin(Conversation, Conversation.thread_id == latest.c.thread_id)
            .outerjoin(Message, Message.id == latest.c.input_message_id)
            .outerjoin(Agent, Agent.slug == latest.c.agent_slug)
            .where(latest.c.rn == 1)
        )

    @staticmethod
    def _handoff_select(uid: str, managed_twin_slugs: list[str]):
        visibility = [HandoffRequest.uid == uid]
        if managed_twin_slugs:
            visibility.append(HandoffRequest.twin_slug.in_(managed_twin_slugs))
        work_status = case(
            (HandoffRequest.status == "open", "running"),
            (HandoffRequest.status == "answered", "completed"),
            (HandoffRequest.status == "closed", "cancelled"),
            else_=HandoffRequest.status,
        )
        updated_at = func.coalesce(HandoffRequest.answered_at, HandoffRequest.created_at)
        return (
            select(
                literal("handoff").label("source_type"),
                cast(HandoffRequest.id, String).label("source_id"),
                literal(None, String).label("run_id"),
                literal(None, String).label("thread_id"),
                literal(None).label("conversation_id"),
                HandoffRequest.twin_slug.label("agent_slug"),
                Agent.name.label("agent_name"),
                HandoffRequest.question.label("title"),
                literal(None, String).label("input_text"),
                literal("founder_twin_handoff", String).label("source"),
                HandoffRequest.status.label("run_status"),
                work_status.label("work_status"),
                literal(None, String).label("error_type"),
                HandoffRequest.uid.label("owner_uid"),
                User.username.label("initiator_name"),
                HandoffRequest.created_at,
                updated_at.label("updated_at"),
                (literal("handoff:") + cast(HandoffRequest.id, String)).label("item_key"),
            )
            .select_from(HandoffRequest)
            .outerjoin(Agent, Agent.slug == HandoffRequest.twin_slug)
            .outerjoin(User, User.uid == HandoffRequest.uid)
            .where(or_(*visibility))
        )

    def _items_subquery(self, uid: str, managed_twin_slugs: list[str]):
        return self._run_select(uid).union_all(self._handoff_select(uid, managed_twin_slugs)).subquery("work_items")

    @staticmethod
    def _to_row(row) -> WorkItemRow:
        created_at = row.created_at or utc_now_naive()
        return WorkItemRow(
            source_type=row.source_type,
            source_id=str(row.source_id),
            run_id=row.run_id,
            thread_id=row.thread_id,
            conversation_id=row.conversation_id,
            agent_slug=row.agent_slug,
            agent_name=row.agent_name,
            title=row.title,
            input_text=row.input_text,
            source=row.source,
            run_status=row.run_status,
            work_status=row.work_status,
            error_type=row.error_type,
            owner_uid=row.owner_uid,
            initiator_name=row.initiator_name,
            created_at=created_at,
            updated_at=row.updated_at or created_at,
        )

    async def list_items(
        self,
        uid: str,
        *,
        managed_twin_slugs: list[str],
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[WorkItemRow], int]:
        """按派生状态筛选并分页，合并 Run 与可见转交单。"""
        items = self._items_subquery(uid, managed_twin_slugs)
        count_stmt = select(func.count()).select_from(items)
        if status:
            count_stmt = count_stmt.where(items.c.work_status == status)
        total = int((await self.db.scalar(count_stmt)) or 0)

        stmt = select(items)
        if status:
            stmt = stmt.where(items.c.work_status == status)
        stmt = (
            stmt.order_by(
                func.coalesce(items.c.updated_at, items.c.created_at).desc(),
                items.c.item_key.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return [self._to_row(row) for row in result.all()], total

    async def get_item(
        self,
        uid: str,
        *,
        managed_twin_slugs: list[str],
        source_type: str,
        source_id: str,
    ) -> WorkItemRow | None:
        """按来源读取当前用户可见的待办；过期 Run 返回 None。"""
        items = self._items_subquery(uid, managed_twin_slugs)
        stmt = select(items).where(items.c.source_type == source_type, items.c.source_id == source_id)
        row = (await self.db.execute(stmt)).first()
        return self._to_row(row) if row else None

    async def summary(self, uid: str, *, managed_twin_slugs: list[str]) -> dict[str, int]:
        """按派生状态统计当前用户可见的全部待办。"""
        items = self._items_subquery(uid, managed_twin_slugs)
        stmt = select(items.c.work_status, func.count()).select_from(items).group_by(items.c.work_status)
        result = await self.db.execute(stmt)
        return {str(status): int(count) for status, count in result.all()}

    async def unread_count(self, uid: str, *, managed_twin_slugs: list[str]) -> int:
        """统计当前用户尚未标记已读的待办数量。"""
        items = self._items_subquery(uid, managed_twin_slugs)
        stmt = (
            select(func.count())
            .select_from(items)
            .outerjoin(
                WorkItemView,
                and_(WorkItemView.uid == uid, WorkItemView.item_key == items.c.item_key),
            )
            .where(WorkItemView.id.is_(None))
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
