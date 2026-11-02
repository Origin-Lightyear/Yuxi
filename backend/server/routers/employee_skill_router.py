"""员工技能卡与交接看板接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_db, get_required_user
from yuxi.repositories.founder_twin_repository import FounderTwinRepository
from yuxi.services.employee_skill_service import (
    CAPTURE_METHODS,
    HANDOVER_STATUSES,
    SKILL_STATUSES,
    accept_handover,
    archive_skill,
    assign_handover,
    can_manage_skill,
    create_skill,
    delete_skill,
    get_manageable_skill,
    get_visible_skill,
    list_handover_candidates,
    list_visible_skills,
    submit_skill_for_review,
    update_skill,
    verify_skill,
)
from yuxi.storage.postgres.models_business import EmployeeSkill, User
from yuxi.utils.datetime_utils import format_utc_datetime

employee_skill_router = APIRouter(prefix="/employee-skills", tags=["employee-skill"])

CAPTURE_METHOD_LABELS = {
    "interview": "访谈",
    "recording": "录音",
    "process": "流程",
    "review": "复盘",
}
SKILL_STATUS_LABELS = {
    "draft": "草稿",
    "needs_review": "待确认",
    "ready": "已就绪",
    "archived": "已归档",
}
HANDOVER_STATUS_LABELS = {
    "unassigned": "未分配",
    "assigned": "已指派",
    "accepted": "已接手",
}


class EmployeeSkillCreate(BaseModel):
    code: str | None = Field(None, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    department: str = Field("", max_length=80)
    owner_uid: str | None = Field(None, max_length=64)
    capture_method: str = Field("interview", max_length=32)
    steps: list[str] = Field(default_factory=list)
    rules: list[str] = Field(default_factory=list)
    boundary: str = ""
    acceptance: str = ""
    source_ref: str | None = Field(None, max_length=512)


class EmployeeSkillUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    department: str | None = Field(None, max_length=80)
    capture_method: str | None = Field(None, max_length=32)
    steps: list[str] | None = None
    rules: list[str] | None = None
    boundary: str | None = None
    acceptance: str | None = None
    source_ref: str | None = Field(None, max_length=512)


class HandoverAssign(BaseModel):
    successor_uid: str = Field(min_length=1, max_length=64)


def _skill_payload(skill: EmployeeSkill, *, user: User, names: dict[str, str]) -> dict:
    return {
        "id": skill.id,
        "code": skill.code,
        "title": skill.title,
        "department": skill.department,
        "owner_uid": skill.owner_uid,
        "owner_name": names.get(skill.owner_uid) or skill.owner_uid,
        "capture_method": skill.capture_method,
        "capture_method_label": CAPTURE_METHOD_LABELS.get(skill.capture_method, skill.capture_method),
        "status": skill.status,
        "status_label": SKILL_STATUS_LABELS.get(skill.status, skill.status),
        "steps": skill.steps or [],
        "rules": skill.rules or [],
        "boundary": skill.boundary,
        "acceptance": skill.acceptance,
        "successor_uid": skill.successor_uid,
        "successor_name": names.get(skill.successor_uid) if skill.successor_uid else None,
        "handover_status": skill.handover_status,
        "handover_status_label": HANDOVER_STATUS_LABELS.get(skill.handover_status, skill.handover_status),
        "source_ref": skill.source_ref,
        "can_manage": can_manage_skill(user, skill),
        "created_at": format_utc_datetime(skill.created_at),
        "updated_at": format_utc_datetime(skill.updated_at),
    }


async def _payload_with_names(db: AsyncSession, skill: EmployeeSkill, *, user: User) -> dict:
    uids = [skill.owner_uid] + ([skill.successor_uid] if skill.successor_uid else [])
    names = await FounderTwinRepository(db).get_user_names(uids)
    return _skill_payload(skill, user=user, names=names)


@employee_skill_router.get("")
async def list_employee_skills(
    status: str | None = Query(None),
    handover_status: str | None = Query(None),
    q: str | None = Query(None),
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """列出当前用户可见的技能卡。"""
    if status is not None and status not in SKILL_STATUSES:
        raise HTTPException(status_code=422, detail="status 只支持 draft、needs_review、ready 或 archived")
    if handover_status is not None and handover_status not in HANDOVER_STATUSES:
        raise HTTPException(status_code=422, detail="handover_status 无效")
    try:
        skills = await list_visible_skills(
            db,
            user=current_user,
            status=status,
            handover_status=handover_status,
            q=q,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    uids = {skill.owner_uid for skill in skills} | {
        skill.successor_uid for skill in skills if skill.successor_uid
    }
    names = await FounderTwinRepository(db).get_user_names(sorted(uids))
    return {
        "skills": [_skill_payload(skill, user=current_user, names=names) for skill in skills],
        "total": len(skills),
    }


@employee_skill_router.get("/{skill_id}/handover-candidates")
async def list_skill_handover_candidates(
    skill_id: int,
    q: str | None = Query(None),
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """列出可指派接手人；只有技能管理者能读取候选名单。"""
    users = await list_handover_candidates(
        db, user=current_user, skill_id=skill_id, q=q
    )
    return {
        "users": [
            {"uid": user.uid, "name": user.username, "department_id": user.department_id}
            for user in users
        ]
    }


@employee_skill_router.get("/{skill_id}")
async def get_employee_skill(
    skill_id: int,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """读取技能卡详情。"""
    skill = await get_visible_skill(db, user=current_user, skill_id=skill_id)
    return {"skill": await _payload_with_names(db, skill, user=current_user)}


@employee_skill_router.post("")
async def create_employee_skill(
    payload: EmployeeSkillCreate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """创建技能卡草稿。"""
    if payload.capture_method not in CAPTURE_METHODS:
        raise HTTPException(status_code=422, detail="capture_method 无效")
    try:
        skill = await create_skill(db, user=current_user, data=payload.model_dump())
        await db.commit()
        await db.refresh(skill)
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"skill": await _payload_with_names(db, skill, user=current_user)}


@employee_skill_router.put("/{skill_id}")
async def update_employee_skill(
    skill_id: int,
    payload: EmployeeSkillUpdate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """更新技能卡内容。"""
    skill = await get_manageable_skill(db, user=current_user, skill_id=skill_id)
    try:
        updated = await update_skill(
            db,
            skill,
            user=current_user,
            data=payload.model_dump(exclude_unset=True),
        )
        await db.commit()
        await db.refresh(updated)
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"skill": await _payload_with_names(db, updated, user=current_user)}


@employee_skill_router.post("/{skill_id}/submit")
async def submit_employee_skill(
    skill_id: int,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """把技能卡送入待确认。"""
    skill = await get_manageable_skill(db, user=current_user, skill_id=skill_id)
    try:
        skill = await submit_skill_for_review(db, skill, user=current_user)
        await db.commit()
        await db.refresh(skill)
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"skill": await _payload_with_names(db, skill, user=current_user)}


@employee_skill_router.post("/{skill_id}/verify")
async def verify_employee_skill(
    skill_id: int,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """确认技能就绪。"""
    skill = await get_manageable_skill(db, user=current_user, skill_id=skill_id)
    try:
        skill = await verify_skill(db, skill, user=current_user)
        await db.commit()
        await db.refresh(skill)
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"skill": await _payload_with_names(db, skill, user=current_user)}


@employee_skill_router.post("/{skill_id}/archive")
async def archive_employee_skill(
    skill_id: int,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """归档技能卡。"""
    skill = await get_manageable_skill(db, user=current_user, skill_id=skill_id)
    try:
        skill = await archive_skill(db, skill, user=current_user)
        await db.commit()
        await db.refresh(skill)
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"skill": await _payload_with_names(db, skill, user=current_user)}


@employee_skill_router.post("/{skill_id}/handover")
async def assign_employee_skill_handover(
    skill_id: int,
    payload: HandoverAssign,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """指派技能接手人。"""
    skill = await get_manageable_skill(db, user=current_user, skill_id=skill_id)
    try:
        skill = await assign_handover(
            db,
            skill,
            user=current_user,
            successor_uid=payload.successor_uid,
        )
        await db.commit()
        await db.refresh(skill)
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"skill": await _payload_with_names(db, skill, user=current_user)}


@employee_skill_router.post("/{skill_id}/accept")
async def accept_employee_skill_handover(
    skill_id: int,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """接手人确认接手技能。"""
    skill = await get_visible_skill(db, user=current_user, skill_id=skill_id)
    try:
        skill = await accept_handover(db, skill, user=current_user)
        await db.commit()
        await db.refresh(skill)
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"skill": await _payload_with_names(db, skill, user=current_user)}


@employee_skill_router.delete("/{skill_id}")
async def delete_employee_skill(
    skill_id: int,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """删除草稿技能卡。"""
    skill = await get_manageable_skill(db, user=current_user, skill_id=skill_id)
    try:
        await delete_skill(db, skill, user=current_user)
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"success": True}
