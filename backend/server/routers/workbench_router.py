"""待办收件箱 HTTP 接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_db, get_required_user
from yuxi.services.workbench_service import (
    get_work_item,
    get_work_item_summary,
    list_work_items,
    mark_work_item_viewed,
)
from yuxi.storage.postgres.models_business import User

workbench_router = APIRouter(prefix="/workbench", tags=["workbench"])


@workbench_router.get("/todos/summary")
async def workbench_summary(
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """返回当前用户待办状态计数与未读数。"""
    return await get_work_item_summary(db, current_user)


@workbench_router.get("/todos")
async def workbench_todos(
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """列出当前用户每条线程的最新顶层 Run。"""
    try:
        return await list_work_items(db, current_user, status=status, limit=limit, offset=offset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@workbench_router.get("/todos/{key}")
async def workbench_todo(
    key: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """读取单条待办详情。"""
    return {"item": await get_work_item(db, current_user, key)}


@workbench_router.post("/todos/{key}/viewed")
async def workbench_mark_viewed(
    key: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """把当前待办标记为已读。"""
    return await mark_work_item_viewed(db, current_user, key)
