"""Agent 记忆条目查看与删除的 HTTP 适配层。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.repositories.agent_repository import AgentRepository
from yuxi.services.agent_memory_service import delete_memory, list_memories
from yuxi.storage.postgres.models_business import User

from server.utils.auth_middleware import get_db, get_required_user

agent_memory_router = APIRouter(prefix="/agent", tags=["agent-memory"])


async def _require_visible_agent(slug: str, *, user: User, db: AsyncSession) -> None:
    """入口可见性统一 fail-closed：不可见智能体一律 404，不泄露存在性。"""
    agent = await AgentRepository(db).get_visible_by_slug(slug=slug, user=user, kind="main")
    if agent is None:
        raise HTTPException(status_code=404, detail="智能体不存在")


@agent_memory_router.get("/{slug}/memories")
async def get_agent_memories(
    slug: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """列出当前用户在该智能体下的记忆条目。"""
    await _require_visible_agent(slug, user=current_user, db=db)
    items = await list_memories(uid=str(current_user.uid), agent_slug=slug)
    return {"items": items}


@agent_memory_router.delete("/{slug}/memories/{memory_id}")
async def delete_agent_memory(
    memory_id: int,
    slug: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """删除当前用户在该智能体下的一条记忆；未命中按 404 处理。"""
    await _require_visible_agent(slug, user=current_user, db=db)
    try:
        await delete_memory(uid=str(current_user.uid), agent_slug=slug, memory_id=memory_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}
