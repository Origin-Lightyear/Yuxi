"""数字员工的档案校验、生命周期与运行时守卫。"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from yuxi.agents.digital_employee import (
    DIGITAL_EMPLOYEE_KIND,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_DRAFT,
)
from yuxi.repositories.agent_repository import user_can_access_agent, user_can_manage_agent
from yuxi.storage.postgres.models_business import Agent, User

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
