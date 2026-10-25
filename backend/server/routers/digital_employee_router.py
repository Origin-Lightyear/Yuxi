"""数字员工配置与生命周期接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_admin_user, get_db, get_required_user
from yuxi.agents.digital_employee import STATUS_ACTIVE, STATUS_DISABLED, STATUS_DRAFT
from yuxi.repositories.agent_repository import AgentRepository
from yuxi.services.digital_employee_service import (
    create_digital_employee,
    delete_digital_employee,
    disable_digital_employee,
    get_digital_employee,
    get_manageable_digital_employee,
    list_digital_employees,
    publish_digital_employee,
    update_digital_employee,
)
from yuxi.storage.postgres.models_business import Agent, User

digital_employee_router = APIRouter(prefix="/digital-employees", tags=["digital-employee"])


class DigitalEmployeeProfile(BaseModel):
    """可编辑的岗位档案字段。"""

    position: str | None = None
    department: str | None = None
    responsibilities: str | None = None
    capabilities: list[str] | None = None
    boundaries: str | None = None
    acceptance: str | None = None


class DigitalEmployeeCreate(BaseModel):
    """创建数字员工草稿。"""

    name: str
    backend_id: str = "ChatbotAgent"
    slug: str | None = None
    description: str | None = None
    icon: str | None = None
    profile: DigitalEmployeeProfile = Field(default_factory=DigitalEmployeeProfile)
    config_json: dict | None = None
    share_config: dict | None = None


class DigitalEmployeeUpdate(BaseModel):
    """更新数字员工；未提交字段保持原值。"""

    name: str | None = None
    description: str | None = None
    icon: str | None = None
    profile: DigitalEmployeeProfile | None = None
    config_json: dict | None = None
    share_config: dict | None = None


async def _serialize_employee(
    repo: AgentRepository,
    agent: Agent,
    user: User,
    *,
    include_resources: bool,
) -> dict:
    """按管理者/普通员工分别序列化，避免泄露私有资源引用。"""
    data = await repo.serialize(agent, user=user, include_configurable_items=include_resources)
    if not data.get("can_manage"):
        data.pop("config_json", None)
        data.pop("configurable_items", None)
    return data


@digital_employee_router.get("")
async def list_employees(
    status: str | None = Query(None),
    q: str | None = Query(None),
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """列出当前用户可见的数字员工。"""
    if status is not None and status not in {STATUS_ACTIVE, STATUS_DRAFT, STATUS_DISABLED}:
        raise HTTPException(status_code=422, detail="status 只支持 active、draft 或 disabled")
    employees, summary = await list_digital_employees(user=current_user, db=db, status=status, q=q)
    repo = AgentRepository(db)
    items = [await _serialize_employee(repo, agent, current_user, include_resources=False) for agent in employees]
    return {"employees": items, "summary": summary}


@digital_employee_router.get("/{slug}")
async def get_employee(
    slug: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """读取可见数字员工详情。"""
    agent = await get_digital_employee(user=current_user, db=db, slug=slug)
    if agent is None:
        raise HTTPException(status_code=404, detail="数字员工不存在")
    repo = AgentRepository(db)
    data = await _serialize_employee(repo, agent, current_user, include_resources=True)
    return {"employee": data}


@digital_employee_router.post("")
async def create_employee(
    payload: DigitalEmployeeCreate,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """创建草稿数字员工。"""
    try:
        agent = await create_digital_employee(
            user=current_user,
            db=db,
            name=payload.name,
            backend_id=payload.backend_id,
            slug=payload.slug,
            description=payload.description,
            icon=payload.icon,
            profile=payload.profile.model_dump(exclude_unset=True),
            config_json=payload.config_json,
            share_config=payload.share_config,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    repo = AgentRepository(db)
    data = await _serialize_employee(repo, agent, current_user, include_resources=True)
    return {"employee": data}


@digital_employee_router.put("/{slug}")
async def update_employee(
    slug: str,
    payload: DigitalEmployeeUpdate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """更新数字员工档案和通用配置。"""
    agent = await get_manageable_digital_employee(user=current_user, db=db, slug=slug)
    changes = payload.model_dump(exclude_unset=True)
    if "profile" in payload.model_fields_set:
        changes["profile"] = payload.profile.model_dump(exclude_unset=True) if payload.profile is not None else None
    try:
        updated = await update_digital_employee(user=current_user, db=db, agent=agent, changes=changes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    repo = AgentRepository(db)
    data = await _serialize_employee(repo, updated, current_user, include_resources=True)
    return {"employee": data}


@digital_employee_router.post("/{slug}/publish")
async def publish_employee(
    slug: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """发布数字员工，发布后普通员工可见。"""
    agent = await get_manageable_digital_employee(user=current_user, db=db, slug=slug)
    try:
        updated = await publish_digital_employee(user=current_user, db=db, agent=agent)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    repo = AgentRepository(db)
    data = await _serialize_employee(repo, updated, current_user, include_resources=True)
    return {"employee": data}


@digital_employee_router.post("/{slug}/disable")
async def disable_employee(
    slug: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """停用数字员工，保留历史对话。"""
    agent = await get_manageable_digital_employee(user=current_user, db=db, slug=slug)
    updated = await disable_digital_employee(user=current_user, db=db, agent=agent)
    repo = AgentRepository(db)
    data = await _serialize_employee(repo, updated, current_user, include_resources=True)
    return {"employee": data}


@digital_employee_router.delete("/{slug}")
async def delete_employee(
    slug: str,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """删除无引用的草稿数字员工。"""
    agent = await get_manageable_digital_employee(user=current_user, db=db, slug=slug)
    await delete_digital_employee(user=current_user, db=db, agent=agent)
    return {"success": True}
