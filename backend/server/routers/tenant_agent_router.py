"""租户平台调用的数字员工与老板分身管理 API。

鉴权：`X-Tenant-Admin-Key` + `X-Tenant-Id`，与租户知识库管理 API 一致。
所有查询按 `agents.tenant_id` 精确过滤；跨租户访问统一 404。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from server.routers.digital_employee_router import DigitalEmployeeCreate, DigitalEmployeeUpdate
from server.routers.founder_twin_router import FounderTwinCreate, FounderTwinUpdate
from server.utils.auth_middleware import get_db
from server.utils.tenant_admin import TenantAdminContext, require_tenant_admin
from yuxi.agents.digital_employee import (
    DIGITAL_EMPLOYEE_KIND,
    FOUNDER_TWIN_KIND,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_DRAFT,
)
from yuxi.permissions import normalize_permission_config
from yuxi.repositories.agent_repository import AgentRepository
from yuxi.services.digital_employee_service import (
    create_digital_employee,
    delete_digital_employee,
    disable_digital_employee,
    list_digital_employees,
    publish_digital_employee,
    update_digital_employee,
)
from yuxi.services.founder_twin_service import (
    create_founder_twin,
    delete_founder_twin,
    get_tenant_twin,
    get_twin_stats,
    update_founder_twin,
)
from yuxi.storage.postgres.models_business import Agent, User


tenant_digital_employee_router = APIRouter(
    prefix="/tenant/digital-employees",
    tags=["tenant-digital-employee"],
)
tenant_founder_twin_router = APIRouter(
    prefix="/tenant/founder-twins",
    tags=["tenant-founder-twin"],
)


def _tenant_actor(ctx: TenantAdminContext) -> User:
    """构造不落库的租户管理员身份，供现有 service 复用。"""
    actor = User()
    actor.id = 0
    actor.uid = f"tenant:{ctx.tenant_id}"
    actor.username = f"租户 {ctx.tenant_id} 管理员"
    actor.role = "admin"
    actor.department_id = None
    actor.tenant_id = ctx.tenant_id
    actor.tenant_department_id = None
    return actor


def _tenant_twin_payload(agent: Agent, *, stats: dict[str, int]) -> dict:
    """序列化租户平台可见的老板分身字段。"""
    config = agent.config_json if isinstance(agent.config_json, dict) else {}
    context = config.get("context") if isinstance(config.get("context"), dict) else {}
    return {
        "slug": agent.slug,
        "name": agent.name,
        "description": agent.description,
        "icon": agent.icon,
        "kind": agent.kind,
        "tenant_id": agent.tenant_id,
        "backend_id": agent.backend_id,
        "share_config": normalize_permission_config(agent.share_config),
        "system_prompt": context.get("system_prompt"),
        "can_manage": True,
        "stats": stats,
    }


async def _tenant_employee(db: AsyncSession, *, slug: str, tenant_id: int) -> Agent:
    """按租户读取数字员工；不存在或跨租户统一 404。"""
    agent = await AgentRepository(db).get_by_slug_for_tenant(slug=slug, tenant_id=tenant_id)
    if agent is None or agent.kind != DIGITAL_EMPLOYEE_KIND:
        raise HTTPException(status_code=404, detail="数字员工不存在")
    return agent


async def _ensure_slug_available(db: AsyncSession, slug: str | None) -> None:
    """租户创建不接受静默改后缀；slug 在全实例内冲突时显式 409。"""
    if slug and await AgentRepository(db).get_by_slug(slug) is not None:
        raise HTTPException(status_code=409, detail="slug 已被占用")


# =============================================================================
# === 数字员工 ===
# =============================================================================


@tenant_digital_employee_router.get("")
async def list_tenant_employees(
    status: str | None = Query(None),
    q: str | None = Query(None),
    ctx: TenantAdminContext = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db),
):
    """本租户数字员工列表。"""
    if status is not None and status not in {STATUS_ACTIVE, STATUS_DRAFT, STATUS_DISABLED}:
        raise HTTPException(status_code=422, detail="status 只支持 active、draft 或 disabled")
    actor = _tenant_actor(ctx)
    employees, summary = await list_digital_employees(
        user=actor,
        db=db,
        status=status,
        q=q,
        tenant_id=ctx.tenant_id,
    )
    repo = AgentRepository(db)
    items = [
        await repo.serialize(agent, user=actor, include_configurable_items=False)
        for agent in employees
    ]
    return {"employees": items, "summary": summary}


@tenant_digital_employee_router.get("/{slug}")
async def get_tenant_employee(
    slug: str,
    ctx: TenantAdminContext = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db),
):
    """本租户数字员工详情。"""
    agent = await _tenant_employee(db, slug=slug, tenant_id=ctx.tenant_id)
    actor = _tenant_actor(ctx)
    data = await AgentRepository(db).serialize(agent, user=actor, include_configurable_items=True)
    return {"employee": data}


@tenant_digital_employee_router.post("", status_code=201)
async def create_tenant_employee(
    payload: DigitalEmployeeCreate,
    ctx: TenantAdminContext = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db),
):
    """在本租户创建数字员工草稿。"""
    await _ensure_slug_available(db, payload.slug)
    actor = _tenant_actor(ctx)
    try:
        agent = await create_digital_employee(
            user=actor,
            db=db,
            name=payload.name,
            backend_id=payload.backend_id,
            slug=payload.slug,
            description=payload.description,
            icon=payload.icon,
            profile=payload.profile.model_dump(exclude_unset=True),
            config_json=payload.config_json,
            system_prompt=payload.system_prompt,
            share_config=payload.share_config,
            tenant_id=ctx.tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    data = await AgentRepository(db).serialize(agent, user=actor, include_configurable_items=False)
    return {"employee": data}


@tenant_digital_employee_router.put("/{slug}")
async def update_tenant_employee(
    slug: str,
    payload: DigitalEmployeeUpdate,
    ctx: TenantAdminContext = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db),
):
    """更新本租户数字员工；不允许修改租户归属或 kind。"""
    agent = await _tenant_employee(db, slug=slug, tenant_id=ctx.tenant_id)
    actor = _tenant_actor(ctx)
    changes = payload.model_dump(exclude_unset=True)
    if "profile" in payload.model_fields_set:
        changes["profile"] = payload.profile.model_dump(exclude_unset=True) if payload.profile is not None else None
    try:
        updated = await update_digital_employee(user=actor, db=db, agent=agent, changes=changes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    data = await AgentRepository(db).serialize(updated, user=actor, include_configurable_items=False)
    return {"employee": data}


@tenant_digital_employee_router.post("/{slug}/publish")
async def publish_tenant_employee(
    slug: str,
    ctx: TenantAdminContext = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db),
):
    """发布本租户数字员工。"""
    agent = await _tenant_employee(db, slug=slug, tenant_id=ctx.tenant_id)
    actor = _tenant_actor(ctx)
    try:
        updated = await publish_digital_employee(user=actor, db=db, agent=agent)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    data = await AgentRepository(db).serialize(updated, user=actor, include_configurable_items=False)
    return {"employee": data}


@tenant_digital_employee_router.post("/{slug}/disable")
async def disable_tenant_employee(
    slug: str,
    ctx: TenantAdminContext = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db),
):
    """停用本租户数字员工。"""
    agent = await _tenant_employee(db, slug=slug, tenant_id=ctx.tenant_id)
    actor = _tenant_actor(ctx)
    updated = await disable_digital_employee(user=actor, db=db, agent=agent)
    data = await AgentRepository(db).serialize(updated, user=actor, include_configurable_items=False)
    return {"employee": data}


@tenant_digital_employee_router.delete("/{slug}")
async def delete_tenant_employee(
    slug: str,
    ctx: TenantAdminContext = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db),
):
    """删除本租户无引用草稿数字员工。"""
    agent = await _tenant_employee(db, slug=slug, tenant_id=ctx.tenant_id)
    await delete_digital_employee(user=_tenant_actor(ctx), db=db, agent=agent)
    return {"success": True}


# =============================================================================
# === 老板分身 ===
# =============================================================================


@tenant_founder_twin_router.get("")
async def list_tenant_twins(
    ctx: TenantAdminContext = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db),
):
    """本租户老板分身列表。"""
    agents = await AgentRepository(db).list_by_kind(kind=FOUNDER_TWIN_KIND, tenant_id=ctx.tenant_id)
    return {
        "twins": [
            _tenant_twin_payload(agent, stats=await get_twin_stats(db, agent.slug))
            for agent in agents
        ]
    }


@tenant_founder_twin_router.get("/{slug}")
async def get_tenant_twin_detail(
    slug: str,
    ctx: TenantAdminContext = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db),
):
    """本租户老板分身详情。"""
    agent = await get_tenant_twin(db, slug=slug, tenant_id=ctx.tenant_id)
    return {"twin": _tenant_twin_payload(agent, stats=await get_twin_stats(db, agent.slug))}


@tenant_founder_twin_router.post("", status_code=201)
async def create_tenant_twin(
    payload: FounderTwinCreate,
    ctx: TenantAdminContext = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db),
):
    """在本租户创建老板分身。"""
    await _ensure_slug_available(db, payload.slug)
    actor = _tenant_actor(ctx)
    try:
        agent = await create_founder_twin(
            db,
            user=actor,
            name=payload.name,
            slug=payload.slug,
            description=payload.description,
            icon=payload.icon,
            system_prompt=payload.system_prompt,
            share_config=payload.share_config,
            tenant_id=ctx.tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "twin": _tenant_twin_payload(
            agent,
            stats={"confirmed_entries": 0, "draft_entries": 0, "open_handoffs": 0},
        )
    }


@tenant_founder_twin_router.put("/{slug}")
async def update_tenant_twin(
    slug: str,
    payload: FounderTwinUpdate,
    ctx: TenantAdminContext = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db),
):
    """更新本租户老板分身；不允许修改租户归属或 kind。"""
    agent = await get_tenant_twin(db, slug=slug, tenant_id=ctx.tenant_id)
    try:
        updated = await update_founder_twin(
            db,
            user=_tenant_actor(ctx),
            agent=agent,
            name=payload.name,
            description=payload.description,
            icon=payload.icon,
            system_prompt=payload.system_prompt,
            share_config=payload.share_config,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"twin": _tenant_twin_payload(updated, stats=await get_twin_stats(db, updated.slug))}


@tenant_founder_twin_router.delete("/{slug}")
async def delete_tenant_twin(
    slug: str,
    ctx: TenantAdminContext = Depends(require_tenant_admin),
    db: AsyncSession = Depends(get_db),
):
    """删除无历史引用的本租户老板分身。"""
    agent = await get_tenant_twin(db, slug=slug, tenant_id=ctx.tenant_id)
    await delete_founder_twin(db, agent=agent)
    return {"success": True}
