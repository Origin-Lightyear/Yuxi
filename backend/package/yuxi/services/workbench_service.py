"""待办收件箱的运行时投影、DTO 与已读语义。"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

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
    """把投影行转换成前后端共享的 WorkItem DTO。"""
    approval_kind = _approval_kind(row.error_type)
    is_digital_employee = row.source == "scheduled_agent"
    key = run_work_item_key(row.run_id)
    return {
        "key": key,
        "source_type": "agent_run",
        "source_id": row.run_id,
        "title": _title(row),
        "agent_slug": row.agent_slug,
        "agent_name": row.agent_name or row.agent_slug,
        "initiator_type": "digital_employee" if is_digital_employee else "human",
        "initiator_uid": viewer.uid,
        "initiator_name": (row.agent_name or row.agent_slug) if is_digital_employee else viewer.username,
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
    rows, total = await repo.list_latest(user.uid, status=status, limit=limit, offset=offset)
    keys = [run_work_item_key(row.run_id) for row in rows]
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
    if source_type != "run":
        raise HTTPException(status_code=404, detail="待办不存在")
    repo = WorkItemRepository(db)
    row = await repo.get_latest_run(user.uid, source_id)
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
    counts = await repo.summary(user.uid)
    normalized = {status: int(counts.get(status, 0)) for status in WORK_ITEM_STATUSES}
    return {
        "counts": normalized,
        "total": sum(normalized.values()),
        "unread": await repo.unread_count(user.uid),
    }
