"""待办收件箱的运行时投影、DTO 与已读语义。"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.agents.digital_employee import FOUNDER_TWIN_KIND
from yuxi.repositories.agent_repository import AgentRepository, user_can_manage_agent
from yuxi.repositories.work_item_repository import WorkItemRepository, WorkItemRow
from yuxi.storage.postgres.models_business import User
from yuxi.utils.datetime_utils import format_utc_datetime

WORK_ITEM_STATUSES = ("queued", "running", "waiting_approval", "completed", "failed", "cancelled")
SOURCE_TYPES = ("run", "handoff")
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


def run_work_item_key(run_id: str) -> str:
    """生成稳定的 Run 待办 key。"""
    return f"run:{run_id}"


def handoff_work_item_key(handoff_id: int | str) -> str:
    """生成稳定的分身转交待办 key。"""
    return f"handoff:{handoff_id}"


def parse_work_item_key(key: str) -> tuple[str, str]:
    """拆分待办 key，拒绝未知来源和空 ID。"""
    source_type, separator, source_id = str(key or "").partition(":")
    if not separator or source_type not in SOURCE_TYPES or not source_id:
        raise ValueError("待办 key 无效")
    return source_type, source_id


def _title(row: WorkItemRow) -> str:
    title = str(row.title or "").strip()
    if title:
        return title
    input_text = str(row.input_text or "").strip()
    if not input_text:
        return "未命名任务"
    return input_text if len(input_text) <= 80 else input_text[:80] + "…"


def _approval_kind(error_type: str | None) -> str | None:
    if error_type == "human_approval_required":
        return "tool"
    if error_type == "ask_user_question_required":
        return "question"
    return None


def work_item_to_dto(row: WorkItemRow, *, viewer: User, unread: bool) -> dict[str, Any]:
    """把统一投影行转换成前后端共享的 WorkItem DTO。"""
    is_handoff = row.source_type == "handoff"
    approval_kind = None if is_handoff else _approval_kind(row.error_type)
    is_digital_employee = row.source == "scheduled_agent"
    key = handoff_work_item_key(row.source_id) if is_handoff else run_work_item_key(row.source_id)
    if is_handoff:
        initiator_type = "human"
        initiator_name = row.initiator_name or row.owner_uid
    elif is_digital_employee:
        initiator_type = "digital_employee"
        initiator_name = row.agent_name or row.agent_slug
    else:
        initiator_type = "human"
        initiator_name = row.initiator_name or viewer.username
    return {
        "key": key,
        "source_type": row.source_type,
        "source_id": row.source_id,
        "title": _title(row),
        "agent_slug": row.agent_slug,
        "agent_name": row.agent_name or row.agent_slug,
        "initiator_type": initiator_type,
        "initiator_uid": row.owner_uid,
        "initiator_name": initiator_name,
        "status": row.work_status,
        "approval_kind": approval_kind,
        "approval_summary": {
            "tool": "等待工具审批",
            "question": "等待回答执行问题",
        }.get(approval_kind),
        "thread_id": row.thread_id,
        "run_id": row.run_id,
        "created_at": format_utc_datetime(row.created_at),
        "updated_at": format_utc_datetime(row.updated_at),
        "unread": unread,
    }


async def _managed_twin_slugs(db: AsyncSession, user: User) -> list[str]:
    """列出当前用户可管理的老板分身，供转交单可见性过滤。"""
    agents = await AgentRepository(db).list_by_kind(kind=FOUNDER_TWIN_KIND)
    return [agent.slug for agent in agents if user_can_manage_agent(user, agent)]


async def list_work_items(
    db: AsyncSession,
    user: User,
    *,
    status: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
    offset: int = 0,
) -> dict[str, Any]:
    """列出当前用户每线程最新顶层 Run。"""
    if status is not None and status not in WORK_ITEM_STATUSES:
        raise ValueError("status 只支持 queued、running、waiting_approval、completed、failed 或 cancelled")
    limit = max(1, min(int(limit), MAX_PAGE_SIZE))
    offset = max(0, int(offset))
    repo = WorkItemRepository(db)
    managed_twin_slugs = await _managed_twin_slugs(db, user)
    rows, total = await repo.list_items(
        user.uid,
        managed_twin_slugs=managed_twin_slugs,
        status=status,
        limit=limit,
        offset=offset,
    )
    keys = [
        handoff_work_item_key(row.source_id) if row.source_type == "handoff" else run_work_item_key(row.source_id)
        for row in rows
    ]
    viewed = await repo.viewed_keys(user.uid, keys)
    return {
        "items": [work_item_to_dto(row, viewer=user, unread=key not in viewed) for row, key in zip(rows, keys)],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def get_work_item(db: AsyncSession, user: User, key: str) -> dict[str, Any]:
    """读取属于当前用户且仍为线程最新 Run 的待办。"""
    try:
        source_type, source_id = parse_work_item_key(key)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="待办不存在") from exc
    row_source_type = "agent_run" if source_type == "run" else "handoff"
    repo = WorkItemRepository(db)
    row = await repo.get_item(
        user.uid,
        managed_twin_slugs=await _managed_twin_slugs(db, user),
        source_type=row_source_type,
        source_id=source_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="待办不存在")
    viewed = await repo.viewed_keys(user.uid, [key])
    return work_item_to_dto(row, viewer=user, unread=key not in viewed)


async def mark_work_item_viewed(db: AsyncSession, user: User, key: str) -> dict[str, Any]:
    """校验可见性后幂等标记已读。"""
    item = await get_work_item(db, user, key)
    await WorkItemRepository(db).mark_viewed(user.uid, key)
    await db.commit()
    return {"viewed": True, "key": item["key"]}


async def get_work_item_summary(db: AsyncSession, user: User) -> dict[str, Any]:
    """返回状态计数与未读总数。"""
    repo = WorkItemRepository(db)
    managed_twin_slugs = await _managed_twin_slugs(db, user)
    counts = await repo.summary(user.uid, managed_twin_slugs=managed_twin_slugs)
    normalized = {status: int(counts.get(status, 0)) for status in WORK_ITEM_STATUSES}
    return {
        "counts": normalized,
        "total": sum(normalized.values()),
        "unread": await repo.unread_count(user.uid, managed_twin_slugs=managed_twin_slugs),
    }
