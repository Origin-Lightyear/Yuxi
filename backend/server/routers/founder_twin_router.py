"""老板分身、经验库、咨询和转交接口。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_admin_user, get_db, get_required_user
from yuxi.agents.buildin import get_agent_backend
from yuxi.agents.digital_employee import FOUNDER_TWIN_KIND
from yuxi.repositories.agent_repository import AgentRepository, user_can_manage_agent
from yuxi.repositories.founder_twin_repository import FounderTwinRepository
from yuxi.services.agent_config_service import prepare_agent_config_write
from yuxi.services.founder_twin_service import (
    EXPERIENCE_STATUSES,
    ExperienceCodeConflict,
    ExperienceStateConflict,
    ExperienceValidationError,
    EXPERIENCE_STATUS_CONFIRMED,
    confirm_experience,
    create_experience_entry,
    get_manageable_twin,
    get_twin_stats,
    get_visible_twin,
    invalidate_experience,
    update_experience_entry,
)
from yuxi.storage.postgres.models_business import (
    Agent,
    ExperienceConsultation,
    ExperienceEntry,
    HandoffRequest,
    User,
)
from yuxi.utils.datetime_utils import format_utc_datetime, utc_now_naive

founder_twin_router = APIRouter(prefix="/founder-twins", tags=["founder-twin"])

DEFAULT_TWIN_PROMPT = (
    "你是老板分身。先调用 retrieve_experience 检索已确认经验，再给出结论；"
    "只有 submit_consultation_answer 成功后才能输出最终答案，且最终答案必须与提交答案一致。"
    "没有可引用依据时，只能调用 create_handoff_request 转交老板本人。"
)


class FounderTwinCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    slug: str | None = None
    description: str | None = None
    icon: str | None = None
    system_prompt: str | None = None
    share_config: dict | None = None


class FounderTwinUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None
    icon: str | None = None
    system_prompt: str | None = None
    share_config: dict | None = None


class ExperienceEntryCreate(BaseModel):
    code: str | None = Field(None, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    situation: str = ""
    judgment: str = Field(min_length=1)
    boundary: str = ""
    source_type: str = Field("manual", max_length=32)
    source_ref: str | None = Field(None, max_length=512)
    source_excerpt: str | None = None


class ExperienceEntryUpdate(BaseModel):
    code: str | None = Field(None, max_length=64)
    title: str | None = Field(None, min_length=1, max_length=200)
    situation: str | None = None
    judgment: str | None = Field(None, min_length=1)
    boundary: str | None = None
    source_type: str | None = Field(None, max_length=32)
    source_ref: str | None = Field(None, max_length=512)
    source_excerpt: str | None = None


class HandoffUpdate(BaseModel):
    status: Literal["answered", "closed"] | None = None
    answer: str | None = None
    assigned_to_uid: str | None = Field(None, max_length=64)


def _twin_payload(agent: Agent, *, user: User, stats: dict[str, int]) -> dict:
    """序列化分身公开字段，不暴露 Agent 配置、embedding 和管理统计。"""
    can_manage = user_can_manage_agent(user, agent)
    public_stats = stats if can_manage else {"confirmed_entries": stats.get("confirmed_entries", 0)}
    return {
        "slug": agent.slug,
        "name": agent.name,
        "description": agent.description,
        "icon": agent.icon,
        "kind": agent.kind,
        "backend_id": agent.backend_id,
        "can_manage": can_manage,
        "stats": public_stats,
    }


def _entry_payload(entry: ExperienceEntry) -> dict:
    """序列化经验条目，隐藏 embedding 与内部模型信息。"""
    return {
        "id": entry.id,
        "twin_slug": entry.twin_slug,
        "code": entry.code,
        "title": entry.title,
        "situation": entry.situation,
        "judgment": entry.judgment,
        "boundary": entry.boundary,
        "source_type": entry.source_type,
        "source_ref": entry.source_ref,
        "source_excerpt": entry.source_excerpt,
        "status": entry.status,
        "citation_count": entry.citation_count,
        "confirmed_by": entry.confirmed_by,
        "confirmed_at": format_utc_datetime(entry.confirmed_at),
        "created_at": format_utc_datetime(entry.created_at),
        "updated_at": format_utc_datetime(entry.updated_at),
    }


def _consultation_payload(
    item: ExperienceConsultation,
    *,
    cited_entries: dict[int, ExperienceEntry] | None = None,
) -> dict:
    entries = cited_entries or {}
    return {
        "id": item.id,
        "twin_slug": item.twin_slug,
        "uid": item.uid,
        "thread_id": item.thread_id,
        "run_id": item.run_id,
        "question": item.question,
        "answer": item.answer,
        "cited_entry_ids": item.cited_entry_ids or [],
        "cited_entries": [
            {
                "id": entry.id,
                "title": entry.title,
                "status": entry.status,
            }
            for entry_id in (item.cited_entry_ids or [])
            if (entry := entries.get(int(entry_id))) is not None
        ],
        "confidence": item.confidence,
        "handoff_id": item.handoff_id,
        "created_at": format_utc_datetime(item.created_at),
    }


def _handoff_payload(
    item: HandoffRequest,
    *,
    requester_name: str | None = None,
    assignee_name: str | None = None,
    can_answer: bool = False,
) -> dict:
    return {
        "id": item.id,
        "twin_slug": item.twin_slug,
        "question": item.question,
        "requester_name": requester_name or "员工",
        "assignee_name": assignee_name,
        "can_answer": can_answer,
        "status": item.status,
        "answer": item.answer,
        "created_at": format_utc_datetime(item.created_at),
        "answered_at": format_utc_datetime(item.answered_at),
    }


@founder_twin_router.get("")
async def list_founder_twins(
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """列出当前用户可见的老板分身。"""
    repo = AgentRepository(db)
    items = await repo.list_visible(user=current_user)
    items = [
        item for item in items if (getattr(item, "kind", "agent") or "agent") == FOUNDER_TWIN_KIND
    ]
    twins = [
        _twin_payload(item, user=current_user, stats=await get_twin_stats(db, item.slug))
        for item in items
    ]
    return {"twins": twins}


@founder_twin_router.post("")
async def create_founder_twin(
    payload: FounderTwinCreate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """创建老板分身；仅管理员可操作。"""
    backend = get_agent_backend("ChatbotAgent")
    config_json, resource_access = await prepare_agent_config_write(
        {"context": {"system_prompt": payload.system_prompt or DEFAULT_TWIN_PROMPT}},
        context_schema=backend.context_schema,
        db=db,
        user=current_user,
    )
    share_config = payload.share_config or {
        "version": 2,
        "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
        "manage_scope": None,
    }
    try:
        agent = await AgentRepository(db).create(
            name=payload.name,
            slug=payload.slug,
            backend_id="ChatbotAgent",
            description=payload.description,
            icon=payload.icon,
            config_json=config_json,
            config_resource_access=resource_access,
            share_config=share_config,
            kind=FOUNDER_TWIN_KIND,
            created_by=str(current_user.uid),
            creator=current_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "twin": _twin_payload(
            agent,
            user=current_user,
            stats={"confirmed_entries": 0, "draft_entries": 0, "open_handoffs": 0},
        )
    }


@founder_twin_router.get("/{slug}")
async def get_founder_twin(
    slug: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """读取单个老板分身。"""
    agent = await get_visible_twin(db, slug=slug, user=current_user)
    return {
        "twin": _twin_payload(
            agent,
            user=current_user,
            stats=await get_twin_stats(db, agent.slug),
        )
    }


@founder_twin_router.put("/{slug}")
async def update_founder_twin(
    slug: str,
    payload: FounderTwinUpdate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """更新分身展示信息和系统提示词；kind 不可变。"""
    agent = await get_manageable_twin(db, slug=slug, user=current_user)
    config_json = None
    resource_access = None
    if payload.system_prompt is not None:
        backend = get_agent_backend(agent.backend_id)
        config_json, resource_access = await prepare_agent_config_write(
            {"context": {"system_prompt": payload.system_prompt}},
            context_schema=backend.context_schema,
            db=db,
            user=current_user,
        )
    try:
        updated = await AgentRepository(db).update(
            agent,
            name=payload.name,
            description=payload.description,
            icon=payload.icon,
            config_json=config_json,
            config_resource_access=resource_access,
            share_config=payload.share_config,
            updated_by=str(current_user.uid),
            updater=current_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "twin": _twin_payload(
            updated,
            user=current_user,
            stats=await get_twin_stats(db, updated.slug),
        )
    }


@founder_twin_router.get("/{slug}/entries")
async def list_experience_entries(
    slug: str,
    status: str | None = Query(None),
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """列出经验；普通员工只能看到已确认条目。"""
    agent = await get_visible_twin(db, slug=slug, user=current_user)
    can_manage = user_can_manage_agent(current_user, agent)
    if status is not None and status not in EXPERIENCE_STATUSES:
        raise HTTPException(status_code=422, detail="status 只支持 draft、confirmed 或 invalid")
    if not can_manage:
        status = EXPERIENCE_STATUS_CONFIRMED
    entries = await FounderTwinRepository(db).list_entries(agent.slug, status=status)
    return {"entries": [_entry_payload(entry) for entry in entries], "total": len(entries)}


@founder_twin_router.post("/{slug}/entries")
async def create_entry(
    slug: str,
    payload: ExperienceEntryCreate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """创建经验草稿；仅分身管理者可操作。"""
    agent = await get_manageable_twin(db, slug=slug, user=current_user)
    try:
        entry = await create_experience_entry(
            db,
            twin_slug=agent.slug,
            data=payload.model_dump(),
            actor_uid=str(current_user.uid),
        )
        await db.commit()
        await db.refresh(entry)
    except ExperienceCodeConflict as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"entry": _entry_payload(entry)}


async def _managed_entry(db: AsyncSession, agent: Agent, entry_id: int) -> ExperienceEntry:
    entry = await FounderTwinRepository(db).get_entry(entry_id)
    if entry is None or entry.twin_slug != agent.slug:
        raise HTTPException(status_code=404, detail="经验条目不存在")
    return entry


@founder_twin_router.put("/{slug}/entries/{entry_id}")
async def update_entry(
    slug: str,
    entry_id: int,
    payload: ExperienceEntryUpdate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """编辑经验；已确认条目编辑后退回草稿。"""
    agent = await get_manageable_twin(db, slug=slug, user=current_user)
    entry = await _managed_entry(db, agent, entry_id)
    try:
        entry = await update_experience_entry(
            db,
            entry,
            data=payload.model_dump(exclude_unset=True),
            actor_uid=str(current_user.uid),
        )
        await db.commit()
        await db.refresh(entry)
    except ExperienceCodeConflict as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"entry": _entry_payload(entry)}


@founder_twin_router.post("/{slug}/entries/{entry_id}/confirm")
async def confirm_entry(
    slug: str,
    entry_id: int,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """确认经验并生成检索向量；embedding 不可用时失败关闭。"""
    agent = await get_manageable_twin(db, slug=slug, user=current_user)
    entry = await _managed_entry(db, agent, entry_id)
    try:
        entry = await confirm_experience(db, entry, actor_uid=str(current_user.uid))
        await db.commit()
        await db.refresh(entry)
    except (ExperienceStateConflict, ExperienceValidationError) as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=503,
            detail=f"embedding 未就绪，经验未确认：{exc}",
        ) from exc
    return {"entry": _entry_payload(entry)}


@founder_twin_router.post("/{slug}/entries/{entry_id}/invalidate")
async def invalidate_entry(
    slug: str,
    entry_id: int,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """标记经验失效。"""
    agent = await get_manageable_twin(db, slug=slug, user=current_user)
    entry = await _managed_entry(db, agent, entry_id)
    entry = await invalidate_experience(db, entry, actor_uid=str(current_user.uid))
    await db.commit()
    await db.refresh(entry)
    return {"entry": _entry_payload(entry)}


@founder_twin_router.get("/{slug}/consultations")
async def list_consultations(
    slug: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """列出咨询；普通员工只看自己的，管理者看全部。"""
    agent = await get_visible_twin(db, slug=slug, user=current_user)
    uid = None if user_can_manage_agent(current_user, agent) else str(current_user.uid)
    repo = FounderTwinRepository(db)
    items = await repo.list_consultations(agent.slug, uid=uid)
    cited_ids = {int(entry_id) for item in items for entry_id in (item.cited_entry_ids or [])}
    cited_entries = {
        entry.id: entry for entry in await repo.get_entries_by_ids(sorted(cited_ids))
    }
    return {
        "consultations": [
            _consultation_payload(item, cited_entries=cited_entries) for item in items
        ]
    }


@founder_twin_router.get("/{slug}/handoffs")
async def list_handoffs(
    slug: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """列出转交单；普通员工只看自己的，管理者看全部。"""
    agent = await get_visible_twin(db, slug=slug, user=current_user)
    is_manager = user_can_manage_agent(current_user, agent)
    uid = None if is_manager else str(current_user.uid)
    repo = FounderTwinRepository(db)
    items = await repo.list_handoffs(agent.slug, uid=uid)
    names = await repo.get_user_names(
        [item.uid for item in items] + [item.assigned_to_uid for item in items if item.assigned_to_uid]
    )
    return {
        "handoffs": [
            _handoff_payload(
                item,
                requester_name=names.get(item.uid),
                assignee_name=names.get(item.assigned_to_uid) if item.assigned_to_uid else None,
                can_answer=is_manager or item.assigned_to_uid == str(current_user.uid),
            )
            for item in items
        ]
    }


@founder_twin_router.put("/{slug}/handoffs/{handoff_id}")
async def update_handoff(
    slug: str,
    handoff_id: int,
    payload: HandoffUpdate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """由分身管理者回答或关闭转交单。"""
    agent = await get_visible_twin(db, slug=slug, user=current_user)
    repo = FounderTwinRepository(db)
    handoff = await repo.get_handoff(handoff_id)
    if handoff is None or handoff.twin_slug != agent.slug:
        raise HTTPException(status_code=404, detail="转交单不存在")
    is_manager = user_can_manage_agent(current_user, agent)
    if not is_manager and handoff.assigned_to_uid != str(current_user.uid):
        raise HTTPException(status_code=403, detail="无权处理该转交单")
    if not is_manager and payload.assigned_to_uid is not None:
        raise HTTPException(status_code=403, detail="只有分身管理者可以改派转交单")
    if payload.status is not None:
        handoff.status = payload.status
    if payload.assigned_to_uid is not None:
        handoff.assigned_to_uid = payload.assigned_to_uid or None
    if payload.answer is not None:
        handoff.answer = payload.answer.strip() or None
        if handoff.answer:
            handoff.status = "answered"
            handoff.answered_at = utc_now_naive()
    if handoff.status == "closed" and handoff.answered_at is None:
        handoff.answered_at = utc_now_naive()
    await db.commit()
    await db.refresh(handoff)
    names = await repo.get_user_names(
        [handoff.uid] + ([handoff.assigned_to_uid] if handoff.assigned_to_uid else [])
    )
    return {
        "handoff": _handoff_payload(
            handoff,
            requester_name=names.get(handoff.uid),
            assignee_name=names.get(handoff.assigned_to_uid) if handoff.assigned_to_uid else None,
            can_answer=True,
        )
    }
