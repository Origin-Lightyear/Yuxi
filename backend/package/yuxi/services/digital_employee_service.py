"""数字员工的档案校验、生命周期与运行时守卫。"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.agents.buildin import AgentBackendNotFoundError, get_agent_backend
from yuxi.agents.digital_employee import (
    DIGITAL_EMPLOYEE_KIND,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_DRAFT,
)
from yuxi.repositories.agent_repository import (
    AgentRepository,
    SUB_AGENT_BACKEND_ID,
    user_can_access_agent,
    user_can_manage_agent,
)
from yuxi.services.agent_config_service import prepare_agent_config_write
from yuxi.storage.postgres.models_business import Agent, User
from yuxi.utils.datetime_utils import utc_now_naive

TEXT_LIMITS = {
    "position": 80,
    "department": 80,
    "responsibilities": 2000,
    "boundaries": 2000,
    "acceptance": 2000,
}
PROFILE_LABELS = {
    "position": "岗位",
    "department": "部门",
    "responsibilities": "职责",
    "boundaries": "边界",
    "acceptance": "验收",
}
SERVER_FIELDS = ("status", "published_at", "published_by", "disabled_at", "disabled_by")
MAX_CAPABILITIES = 12
MAX_CAPABILITY_LENGTH = 40
DEFAULT_EMPLOYEE_SHARE_CONFIG = {
    "version": 2,
    "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
    "manage_scope": None,
}


def _clean_text(value: Any, field: str, maximum: int) -> str:
    """归一化文本字段并拒绝超长内容。"""
    text = str(value or "").strip()
    if len(text) > maximum:
        raise ValueError(f"{PROFILE_LABELS.get(field, field)}不能超过 {maximum} 个字符")
    return text


def _normalize_capabilities(value: Any) -> list[str]:
    """去空白、去重并校验能力标签的数量和长度。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("能力必须是列表")
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        if len(text) > MAX_CAPABILITY_LENGTH:
            raise ValueError(f"单项能力不能超过 {MAX_CAPABILITY_LENGTH} 个字符")
        if text not in result:
            result.append(text)
    if len(result) > MAX_CAPABILITIES:
        raise ValueError(f"能力最多 {MAX_CAPABILITIES} 项")
    return result


def normalize_profile_patch(patch: dict | None, current: dict | None = None) -> dict:
    """合并档案补丁，保留服务端拥有的状态和发布时间。"""
    if patch is not None and not isinstance(patch, dict):
        raise ValueError("岗位档案必须是对象")
    current_profile = current if isinstance(current, dict) else {}
    result = {field: _clean_text(current_profile.get(field), field, maximum) for field, maximum in TEXT_LIMITS.items()}
    result["capabilities"] = _normalize_capabilities(current_profile.get("capabilities") or [])

    patch = patch or {}
    for field, maximum in TEXT_LIMITS.items():
        if field in patch:
            result[field] = _clean_text(patch[field], field, maximum)
    if "capabilities" in patch:
        result["capabilities"] = _normalize_capabilities(patch["capabilities"])

    for field in SERVER_FIELDS:
        result[field] = current_profile.get(field)
    if not result.get("status"):
        result["status"] = STATUS_DRAFT
    return result


def validate_profile_for_publish(profile: dict) -> None:
    """校验发布所需的最小岗位信息。"""
    missing = [
        label
        for field, label in PROFILE_LABELS.items()
        if not str(profile.get(field) or "").strip()
    ]
    if missing:
        raise ValueError("发布前请填写：" + "、".join(missing))
    if not profile.get("capabilities"):
        raise ValueError("发布前至少填写一项能力")


def digital_employee_status(agent: Agent) -> str | None:
    """返回数字员工状态；普通智能体返回 None。"""
    if getattr(agent, "kind", None) != DIGITAL_EMPLOYEE_KIND:
        return None
    profile = getattr(agent, "digital_employee_profile", None) or {}
    return profile.get("status") or STATUS_DRAFT


def digital_employee_is_runnable(agent: Agent) -> bool:
    """普通智能体始终可运行；数字员工只有 active 可运行。"""
    return getattr(agent, "kind", None) != DIGITAL_EMPLOYEE_KIND or digital_employee_status(agent) == STATUS_ACTIVE


def digital_employee_can_view(user: User, agent: Agent) -> bool:
    """通用详情可见性：停用员工保留历史，草稿只对管理者可见。"""
    if getattr(agent, "kind", None) != DIGITAL_EMPLOYEE_KIND:
        return True
    if not user_can_access_agent(user, agent):
        return False
    return digital_employee_status(agent) in {STATUS_ACTIVE, STATUS_DISABLED} or user_can_manage_agent(user, agent)


def digital_employee_is_visible(user: User, agent: Agent) -> bool:
    """员工目录可见性：普通用户只看到在岗员工，管理者额外看到草稿和停用。"""
    if getattr(agent, "kind", None) != DIGITAL_EMPLOYEE_KIND:
        return True
    if not user_can_access_agent(user, agent):
        return False
    return digital_employee_status(agent) == STATUS_ACTIVE or user_can_manage_agent(user, agent)


def ensure_digital_employee_can_start(user: User, agent: Agent) -> None:
    """在产生新运行、新线程或新绑定前拒绝不可用员工。"""
    if getattr(agent, "kind", None) != DIGITAL_EMPLOYEE_KIND:
        return
    if not digital_employee_can_view(user, agent):
        raise HTTPException(status_code=404, detail="数字员工不存在")
    if digital_employee_status(agent) != STATUS_ACTIVE:
        raise HTTPException(status_code=409, detail="数字员工未发布或已停用，无法发起新的运行")


def _search_text(agent: Agent) -> str:
    profile = agent.digital_employee_profile or {}
    parts = [
        agent.name,
        profile.get("position"),
        profile.get("department"),
        profile.get("responsibilities"),
        *(profile.get("capabilities") or []),
    ]
    return " ".join(str(part or "") for part in parts).lower()


async def list_digital_employees(
    *,
    user: User,
    db: AsyncSession,
    status: str | None = None,
    q: str | None = None,
) -> tuple[list[Agent], dict[str, int]]:
    """列出可见员工，并在筛选前计算管理者需要的人数摘要。"""
    agents = await AgentRepository(db).list_by_kind(kind=DIGITAL_EMPLOYEE_KIND)
    visible = [agent for agent in agents if digital_employee_is_visible(user, agent)]
    summary = {
        status_value: sum(1 for agent in visible if digital_employee_status(agent) == status_value)
        for status_value in (STATUS_ACTIVE, STATUS_DRAFT, STATUS_DISABLED)
    }
    if status:
        visible = [agent for agent in visible if digital_employee_status(agent) == status]
    if q and q.strip():
        needle = q.strip().lower()
        visible = [agent for agent in visible if needle in _search_text(agent)]
    order = {STATUS_ACTIVE: 0, STATUS_DRAFT: 1, STATUS_DISABLED: 2}
    visible.sort(key=lambda agent: (order.get(digital_employee_status(agent), 3), (agent.name or "").casefold()))
    return visible, summary


async def get_digital_employee(*, user: User, db: AsyncSession, slug: str) -> Agent | None:
    """读取可见的数字员工详情；普通智能体和不可见员工返回 None。"""
    agent = await AgentRepository(db).get_by_slug(slug)
    if agent is None or agent.kind != DIGITAL_EMPLOYEE_KIND:
        return None
    return agent if digital_employee_is_visible(user, agent) else None


async def get_manageable_digital_employee(*, user: User, db: AsyncSession, slug: str) -> Agent:
    """读取可管理员工，按不存在和权限不足分别失败。"""
    agent = await AgentRepository(db).get_by_slug(slug)
    if agent is None or agent.kind != DIGITAL_EMPLOYEE_KIND or not user_can_access_agent(user, agent):
        raise HTTPException(status_code=404, detail="数字员工不存在")
    if not user_can_manage_agent(user, agent):
        raise HTTPException(status_code=403, detail="不能管理该数字员工")
    return agent


async def _get_locked_employee(*, db: AsyncSession, slug: str) -> Agent:
    agent = await AgentRepository(db).get_by_slug_for_update(slug=slug)
    if agent is None or agent.kind != DIGITAL_EMPLOYEE_KIND:
        raise HTTPException(status_code=404, detail="数字员工不存在")
    return agent


def _require_admin(user: User) -> None:
    if user.role not in {"admin", "superadmin"}:
        raise HTTPException(status_code=403, detail="需要管理员权限")


def _prepare_profile_for_new_employee(profile: dict | None) -> dict:
    normalized = normalize_profile_patch(profile or {})
    normalized["status"] = STATUS_DRAFT
    normalized["published_at"] = None
    normalized["published_by"] = None
    normalized["disabled_at"] = None
    normalized["disabled_by"] = None
    return normalized


async def create_digital_employee(
    *,
    user: User,
    db: AsyncSession,
    name: str,
    backend_id: str,
    slug: str | None = None,
    description: str | None = None,
    icon: str | None = None,
    profile: dict | None = None,
    config_json: dict | None = None,
    share_config: dict | None = None,
) -> Agent:
    """创建草稿数字员工；创建者必须是管理员。"""
    _require_admin(user)
    if not str(name or "").strip():
        raise ValueError("数字员工名称不能为空")
    if backend_id == SUB_AGENT_BACKEND_ID:
        raise ValueError("数字员工不能使用 SubAgentBackend")
    try:
        backend = get_agent_backend(backend_id)
    except AgentBackendNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    resolved_config, resource_access = await prepare_agent_config_write(
        config_json or {},
        context_schema=backend.context_schema,
        db=db,
        user=user,
    )
    normalized_profile = _prepare_profile_for_new_employee(profile)
    repo = AgentRepository(db)
    return await repo.create(
        name=name,
        slug=slug,
        backend_id=backend_id,
        description=description,
        icon=icon,
        pics=None,
        config_json=resolved_config,
        config_resource_access=resource_access,
        share_config=share_config or DEFAULT_EMPLOYEE_SHARE_CONFIG,
        kind=DIGITAL_EMPLOYEE_KIND,
        digital_employee_profile=normalized_profile,
        is_default=False,
        is_subagent=False,
        created_by=str(user.uid),
        creator=user,
    )


async def update_digital_employee(
    *,
    user: User,
    db: AsyncSession,
    agent: Agent,
    changes: dict,
) -> Agent:
    """更新员工档案和通用配置；active 员工更新后仍必须满足发布条件。"""
    allowed = {"name", "description", "icon", "profile", "config_json", "share_config"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"不支持的字段：{', '.join(sorted(unknown))}")

    locked = await _get_locked_employee(db=db, slug=agent.slug)
    if not user_can_manage_agent(user, locked):
        raise HTTPException(status_code=403, detail="不能管理该数字员工")

    profile = None
    if "profile" in changes:
        if not isinstance(changes["profile"], dict):
            raise ValueError("岗位档案必须是对象")
        profile = normalize_profile_patch(changes["profile"], locked.digital_employee_profile)
        if digital_employee_status(locked) == STATUS_ACTIVE:
            validate_profile_for_publish(profile)

    config_json = None
    resource_access = None
    if changes.get("config_json") is not None:
        try:
            backend = get_agent_backend(locked.backend_id)
        except AgentBackendNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        config_json, resource_access = await prepare_agent_config_write(
            changes["config_json"],
            context_schema=backend.context_schema,
            db=db,
            user=user,
        )

    if "name" in changes:
        name = str(changes["name"] or "").strip()
        if not name:
            raise ValueError("数字员工名称不能为空")
        locked.name = name
    if "description" in changes:
        locked.description = changes["description"]
    if "icon" in changes:
        locked.icon = changes["icon"]

    repo = AgentRepository(db)
    return await repo.update(
        locked,
        config_json=config_json,
        config_resource_access=resource_access,
        share_config=changes.get("share_config"),
        digital_employee_profile=profile,
        updated_by=str(user.uid),
        updater=user,
    )


async def publish_digital_employee(*, user: User, db: AsyncSession, agent: Agent) -> Agent:
    """发布员工档案；重复发布幂等。"""
    locked = await _get_locked_employee(db=db, slug=agent.slug)
    if not user_can_manage_agent(user, locked):
        raise HTTPException(status_code=403, detail="不能管理该数字员工")
    if digital_employee_status(locked) == STATUS_ACTIVE:
        return locked

    profile = normalize_profile_patch({}, locked.digital_employee_profile)
    validate_profile_for_publish(profile)
    profile["status"] = STATUS_ACTIVE
    profile["published_at"] = utc_now_naive().isoformat()
    profile["published_by"] = str(user.uid)
    profile["disabled_at"] = None
    profile["disabled_by"] = None
    return await AgentRepository(db).update(
        locked,
        digital_employee_profile=profile,
        updated_by=str(user.uid),
        updater=user,
    )


async def disable_digital_employee(*, user: User, db: AsyncSession, agent: Agent) -> Agent:
    """停用员工；重复停用幂等，历史数据保留。"""
    locked = await _get_locked_employee(db=db, slug=agent.slug)
    if not user_can_manage_agent(user, locked):
        raise HTTPException(status_code=403, detail="不能管理该数字员工")
    if digital_employee_status(locked) == STATUS_DISABLED:
        return locked

    profile = normalize_profile_patch({}, locked.digital_employee_profile)
    profile["status"] = STATUS_DISABLED
    profile["disabled_at"] = utc_now_naive().isoformat()
    profile["disabled_by"] = str(user.uid)
    return await AgentRepository(db).update(
        locked,
        digital_employee_profile=profile,
        updated_by=str(user.uid),
        updater=user,
    )


async def delete_digital_employee(*, user: User, db: AsyncSession, agent: Agent) -> None:
    """删除无引用的草稿；已发布或已有引用的员工拒绝删除。"""
    locked = await _get_locked_employee(db=db, slug=agent.slug)
    if not user_can_manage_agent(user, locked):
        raise HTTPException(status_code=403, detail="不能管理该数字员工")
    if digital_employee_status(locked) != STATUS_DRAFT:
        raise HTTPException(status_code=409, detail="只有草稿数字员工可以删除")
    counts = await AgentRepository(db).count_digital_employee_references(slug=locked.slug)
    if any(counts.values()):
        raise HTTPException(status_code=409, detail="数字员工仍被对话、定时任务或 IM Bot 引用")
    await AgentRepository(db).delete(agent=locked)
