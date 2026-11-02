"""员工技能卡：沉淀、送审、确认和交接状态机。"""

from __future__ import annotations

import re
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.repositories.employee_skill_repository import EmployeeSkillRepository
from yuxi.storage.postgres.models_business import EmployeeSkill, User
from yuxi.utils.datetime_utils import utc_now_naive

SKILL_STATUS_DRAFT = "draft"
SKILL_STATUS_NEEDS_REVIEW = "needs_review"
SKILL_STATUS_READY = "ready"
SKILL_STATUS_ARCHIVED = "archived"
SKILL_STATUSES = (
    SKILL_STATUS_DRAFT,
    SKILL_STATUS_NEEDS_REVIEW,
    SKILL_STATUS_READY,
    SKILL_STATUS_ARCHIVED,
)

CAPTURE_METHODS = ("interview", "recording", "process", "review")
HANDOVER_STATUS_UNASSIGNED = "unassigned"
HANDOVER_STATUS_ASSIGNED = "assigned"
HANDOVER_STATUS_ACCEPTED = "accepted"
HANDOVER_STATUSES = (HANDOVER_STATUS_UNASSIGNED, HANDOVER_STATUS_ASSIGNED, HANDOVER_STATUS_ACCEPTED)

MAX_STEPS = 20
MAX_RULES = 20
MAX_LIST_ITEM_CHARS = 300
MAX_TITLE_CHARS = 200
MAX_DEPARTMENT_CHARS = 80
MAX_BOUNDARY_CHARS = 2000
MAX_ACCEPTANCE_CHARS = 2000


def _is_admin(user: User) -> bool:
    return user.role in {"admin", "superadmin"}


def _clip(value: Any, maximum: int) -> str:
    return str(value or "").strip()[:maximum]


def _string_list(value: Any, *, maximum: int) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("步骤和规则必须是文本列表")
    result: list[str] = []
    for item in value:
        text = _clip(item, MAX_LIST_ITEM_CHARS)
        if text and text not in result:
            result.append(text)
        if len(result) >= maximum:
            break
    return result


def _normalize_code(value: str | None) -> str:
    text = _clip(value, 64)
    if not text:
        return f"skill-{uuid.uuid4().hex[:12]}"
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", text).strip("-").lower()
    if not normalized:
        raise ValueError("技能编码只能包含字母、数字、下划线和短横线")
    return normalized[:64]


def can_manage_skill(user: User, skill: EmployeeSkill) -> bool:
    """管理员或技能归属人可以修改技能卡和交接状态。"""
    return _is_admin(user) or skill.owner_uid == str(user.uid)


def can_view_skill(user: User, skill: EmployeeSkill) -> bool:
    """已就绪技能对所有员工可见；草稿和送审只对归属人、接手人、管理员可见。"""
    if can_manage_skill(user, skill):
        return True
    if skill.status == SKILL_STATUS_READY:
        return True
    return skill.successor_uid == str(user.uid)


async def _load_user(db: AsyncSession, uid: str) -> User:
    user = await db.scalar(select(User).where(User.uid == uid, User.is_deleted == 0))
    if user is None:
        raise HTTPException(status_code=422, detail="用户不存在")
    return user


async def list_visible_skills(
    db: AsyncSession,
    *,
    user: User,
    status: str | None = None,
    handover_status: str | None = None,
    q: str | None = None,
) -> list[EmployeeSkill]:
    """列出当前用户可见的技能卡，并按状态、交接状态和关键词过滤。"""
    if status is not None and status not in SKILL_STATUSES:
        raise ValueError("技能状态无效")
    if handover_status is not None and handover_status not in HANDOVER_STATUSES:
        raise ValueError("交接状态无效")
    repo = EmployeeSkillRepository(db)
    items = await repo.list_all()
    visible = [item for item in items if can_view_skill(user, item)]
    if status:
        visible = [item for item in visible if item.status == status]
    if handover_status:
        visible = [item for item in visible if item.handover_status == handover_status]
    keyword = str(q or "").strip().lower()
    if keyword:
        visible = [
            item
            for item in visible
            if keyword
            in " ".join(
                [
                    item.title,
                    item.department,
                    item.code,
                    item.boundary,
                    *(item.steps or []),
                    *(item.rules or []),
                ]
            ).lower()
        ]
    return visible


async def get_visible_skill(db: AsyncSession, *, user: User, skill_id: int) -> EmployeeSkill:
    """读取可见技能卡；不存在和不可见统一 404。"""
    skill = await EmployeeSkillRepository(db).get(skill_id)
    if skill is None or not can_view_skill(user, skill):
        raise HTTPException(status_code=404, detail="技能卡不存在")
    return skill


async def get_manageable_skill(db: AsyncSession, *, user: User, skill_id: int) -> EmployeeSkill:
    """读取可管理技能卡；不可见 404，可见但无权限 403。"""
    skill = await get_visible_skill(db, user=user, skill_id=skill_id)
    if not can_manage_skill(user, skill):
        raise HTTPException(status_code=403, detail="无权管理该技能卡")
    return skill


async def list_handover_candidates(
    db: AsyncSession, *, user: User, skill_id: int, q: str | None = None
) -> list[User]:
    """列出技能可指派接手人；只有技能管理者能读取候选名单。"""
    skill = await get_manageable_skill(db, user=user, skill_id=skill_id)
    return await EmployeeSkillRepository(db).list_handover_candidates(
        exclude_uid=skill.owner_uid,
        keyword=str(q or "").strip(),
    )


async def create_skill(
    db: AsyncSession,
    *,
    user: User,
    data: dict,
) -> EmployeeSkill:
    """创建技能卡草稿；普通员工只能创建自己的技能卡。"""
    owner_uid = _clip(data.get("owner_uid"), 64) or str(user.uid)
    if owner_uid != str(user.uid) and not _is_admin(user):
        raise HTTPException(status_code=403, detail="只有管理员可以代建技能卡")
    await _load_user(db, owner_uid)
    title = _clip(data.get("title"), MAX_TITLE_CHARS)
    if not title:
        raise ValueError("技能标题不能为空")
    capture_method = _clip(data.get("capture_method"), 32) or "interview"
    if capture_method not in CAPTURE_METHODS:
        raise ValueError("沉淀方式只支持 interview、recording、process 或 review")
    skill = EmployeeSkill(
        code=_normalize_code(data.get("code")),
        title=title,
        department=_clip(data.get("department"), MAX_DEPARTMENT_CHARS),
        owner_uid=owner_uid,
        capture_method=capture_method,
        status=SKILL_STATUS_DRAFT,
        steps=_string_list(data.get("steps"), maximum=MAX_STEPS),
        rules=_string_list(data.get("rules"), maximum=MAX_RULES),
        boundary=_clip(data.get("boundary"), MAX_BOUNDARY_CHARS),
        acceptance=_clip(data.get("acceptance"), MAX_ACCEPTANCE_CHARS),
        successor_uid=None,
        handover_status=HANDOVER_STATUS_UNASSIGNED,
        source_ref=_clip(data.get("source_ref"), 512) or None,
        created_by=str(user.uid),
        updated_by=str(user.uid),
    )
    db.add(skill)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="技能编码已存在") from exc
    return skill


async def update_skill(
    db: AsyncSession,
    skill: EmployeeSkill,
    *,
    user: User,
    data: dict,
) -> EmployeeSkill:
    """更新技能内容；已就绪技能内容变化后退回送审。"""
    locked = await _lock_skill(db, skill)
    if locked.status == SKILL_STATUS_ARCHIVED:
        raise ValueError("已归档技能不能再编辑")
    if not can_manage_skill(user, locked):
        raise HTTPException(status_code=403, detail="无权管理该技能卡")

    for field_name in ("title", "department", "capture_method", "boundary", "acceptance", "source_ref"):
        if field_name not in data:
            continue
        maximum = {
            "title": MAX_TITLE_CHARS,
            "department": MAX_DEPARTMENT_CHARS,
            "capture_method": 32,
            "boundary": MAX_BOUNDARY_CHARS,
            "acceptance": MAX_ACCEPTANCE_CHARS,
            "source_ref": 512,
        }[field_name]
        value = _clip(data[field_name], maximum) or ("" if field_name != "source_ref" else None)
        setattr(locked, field_name, value)
    if not locked.title:
        raise ValueError("技能标题不能为空")
    if locked.capture_method not in CAPTURE_METHODS:
        raise ValueError("沉淀方式无效")
    if "steps" in data:
        locked.steps = _string_list(data["steps"], maximum=MAX_STEPS)
    if "rules" in data:
        locked.rules = _string_list(data["rules"], maximum=MAX_RULES)
    if locked.status == SKILL_STATUS_READY:
        locked.status = SKILL_STATUS_NEEDS_REVIEW
    locked.updated_by = str(user.uid)
    locked.updated_at = utc_now_naive()
    await db.flush()
    return locked


async def submit_skill_for_review(db: AsyncSession, skill: EmployeeSkill, *, user: User) -> EmployeeSkill:
    """把草稿送入待确认；已就绪或已归档状态拒绝。"""
    locked = await _lock_skill(db, skill)
    if locked.status not in {SKILL_STATUS_DRAFT, SKILL_STATUS_NEEDS_REVIEW}:
        raise ValueError("只有草稿或送审中的技能可以提交确认")
    if not can_manage_skill(user, locked):
        raise HTTPException(status_code=403, detail="无权管理该技能卡")
    _require_ready_content(locked)
    locked.status = SKILL_STATUS_NEEDS_REVIEW
    locked.updated_by = str(user.uid)
    locked.updated_at = utc_now_naive()
    await db.flush()
    return locked


async def verify_skill(db: AsyncSession, skill: EmployeeSkill, *, user: User) -> EmployeeSkill:
    """确认技能就绪；必须满足标题、步骤和验收。"""
    locked = await _lock_skill(db, skill)
    if locked.status == SKILL_STATUS_ARCHIVED:
        raise ValueError("已归档技能不能确认")
    if not can_manage_skill(user, locked):
        raise HTTPException(status_code=403, detail="无权管理该技能卡")
    _require_ready_content(locked)
    locked.status = SKILL_STATUS_READY
    locked.updated_by = str(user.uid)
    locked.updated_at = utc_now_naive()
    await db.flush()
    return locked


async def archive_skill(db: AsyncSession, skill: EmployeeSkill, *, user: User) -> EmployeeSkill:
    """归档技能；保留历史但不再向普通员工展示。"""
    locked = await _lock_skill(db, skill)
    if not can_manage_skill(user, locked):
        raise HTTPException(status_code=403, detail="无权管理该技能卡")
    locked.status = SKILL_STATUS_ARCHIVED
    locked.updated_by = str(user.uid)
    locked.updated_at = utc_now_naive()
    await db.flush()
    return locked


async def assign_handover(
    db: AsyncSession,
    skill: EmployeeSkill,
    *,
    user: User,
    successor_uid: str,
) -> EmployeeSkill:
    """由归属人或管理员指派接手人；不改变技能就绪状态。"""
    locked = await _lock_skill(db, skill)
    if locked.status != SKILL_STATUS_READY:
        raise ValueError("只有已就绪技能可以进入交接")
    if not can_manage_skill(user, locked):
        raise HTTPException(status_code=403, detail="无权管理该技能卡")
    target_uid = _clip(successor_uid, 64)
    if not target_uid:
        raise ValueError("接手人不能为空")
    await _load_user(db, target_uid)
    if target_uid == locked.owner_uid:
        raise ValueError("接手人不能是技能归属人本人")
    locked.successor_uid = target_uid
    locked.handover_status = HANDOVER_STATUS_ASSIGNED
    locked.updated_by = str(user.uid)
    locked.updated_at = utc_now_naive()
    await db.flush()
    return locked


async def accept_handover(db: AsyncSession, skill: EmployeeSkill, *, user: User) -> EmployeeSkill:
    """接手人或管理员确认接手；未指派时拒绝。"""
    locked = await _lock_skill(db, skill)
    if locked.handover_status != HANDOVER_STATUS_ASSIGNED or not locked.successor_uid:
        raise ValueError("技能尚未指派接手人")
    if not (_is_admin(user) or locked.successor_uid == str(user.uid)):
        raise HTTPException(status_code=403, detail="只有指定接手人可以确认接手")
    locked.handover_status = HANDOVER_STATUS_ACCEPTED
    locked.updated_by = str(user.uid)
    locked.updated_at = utc_now_naive()
    await db.flush()
    return locked


async def delete_skill(db: AsyncSession, skill: EmployeeSkill, *, user: User) -> None:
    """删除草稿技能；已进入交接的事实不能删除。"""
    locked = await _lock_skill(db, skill)
    if not can_manage_skill(user, locked):
        raise HTTPException(status_code=403, detail="无权管理该技能卡")
    if locked.status != SKILL_STATUS_DRAFT or locked.handover_status != HANDOVER_STATUS_UNASSIGNED:
        raise ValueError("只有未进入交接的草稿技能可以删除")
    await db.delete(locked)
    await db.flush()


async def _lock_skill(db: AsyncSession, skill: EmployeeSkill) -> EmployeeSkill:
    """锁定技能卡行并刷新状态，避免并发状态转换互相覆盖。"""
    locked = await EmployeeSkillRepository(db).get(skill.id, for_update=True)
    if locked is None:
        raise HTTPException(status_code=404, detail="技能卡不存在")
    return locked


def _require_ready_content(skill: EmployeeSkill) -> None:
    missing: list[str] = []
    if not skill.title:
        missing.append("标题")
    if not skill.steps:
        missing.append("步骤")
    if not skill.acceptance:
        missing.append("验收")
    if missing:
        raise ValueError("确认就绪前请填写：" + "、".join(missing))
