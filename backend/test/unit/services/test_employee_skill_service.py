from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.employee_skill_repository import EmployeeSkillRepository
from yuxi.services import employee_skill_service
from yuxi.storage.postgres.models_business import Base, EmployeeSkill, User

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


async def _seed_user(session, *, uid: str, role: str = "user") -> User:
    user = User(uid=uid, username=uid, password_hash="x", role=role)
    session.add(user)
    await session.commit()
    return user


async def test_skill_visibility_and_handover_state_machine(session):
    owner = await _seed_user(session, uid="owner")
    colleague = await _seed_user(session, uid="colleague")
    successor = await _seed_user(session, uid="successor")
    admin = await _seed_user(session, uid="admin", role="admin")

    skill = await employee_skill_service.create_skill(
        session,
        user=owner,
        data={
            "title": "大客户报价复盘",
            "department": "销售",
            "capture_method": "review",
            "steps": ["确认预算", "核对成本", "给出折扣区间"],
            "rules": ["超过十万元必须二次确认"],
            "boundary": "不承诺未审批折扣",
            "acceptance": "报价单包含预算、成本和审批人",
        },
    )
    await session.commit()
    assert skill.status == "draft"

    assert [item.id for item in await employee_skill_service.list_visible_skills(session, user=owner)] == [skill.id]
    assert await employee_skill_service.list_visible_skills(session, user=colleague) == []

    verified = await employee_skill_service.verify_skill(session, skill, user=owner)
    await session.commit()
    assert verified.status == "ready"
    assert [item.id for item in await employee_skill_service.list_visible_skills(session, user=colleague)] == [skill.id]

    assigned = await employee_skill_service.assign_handover(
        session, skill, user=owner, successor_uid=successor.uid
    )
    await session.commit()
    assert assigned.handover_status == "assigned"
    assert assigned.successor_uid == "successor"

    with pytest.raises(Exception) as accept_error:
        await employee_skill_service.accept_handover(session, skill, user=colleague)
    assert getattr(accept_error.value, "status_code", None) == 403

    accepted = await employee_skill_service.accept_handover(session, skill, user=successor)
    await session.commit()
    assert accepted.handover_status == "accepted"

    updated = await employee_skill_service.update_skill(
        session,
        skill,
        user=owner,
        data={"steps": ["确认预算", "核对成本", "给出折扣区间", "店长复核"]},
    )
    await session.commit()
    assert updated.status == "needs_review"

    with pytest.raises(Exception) as verify_error:
        await employee_skill_service.verify_skill(session, updated, user=colleague)
    assert getattr(verify_error.value, "status_code", None) == 403

    archived = await employee_skill_service.archive_skill(session, updated, user=admin)
    await session.commit()
    assert archived.status == "archived"
    assert await employee_skill_service.list_visible_skills(session, user=colleague) == []


async def test_ready_requires_steps_and_acceptance(session):
    owner = await _seed_user(session, uid="owner")
    skill = await employee_skill_service.create_skill(
        session,
        user=owner,
        data={"title": "空技能", "capture_method": "interview"},
    )
    await session.commit()

    with pytest.raises(ValueError) as error:
        await employee_skill_service.verify_skill(session, skill, user=owner)
    assert "步骤" in str(error.value)
    assert "验收" in str(error.value)


async def test_skill_lock_refreshes_stale_identity_state(session):
    owner = await _seed_user(session, uid="owner")
    skill = await employee_skill_service.create_skill(
        session,
        user=owner,
        data={"title": "锁刷新", "steps": ["一步"], "acceptance": "可验收"},
    )
    await session.commit()
    await session.execute(
        update(EmployeeSkill)
        .where(EmployeeSkill.id == skill.id)
        .values(status="ready"),
        execution_options={"synchronize_session": False},
    )
    assert skill.status == "draft"

    locked = await EmployeeSkillRepository(session).get(skill.id, for_update=True)
    assert locked is skill
    assert locked.status == "ready"


async def test_other_user_cannot_modify_or_assign_draft(session):
    owner = await _seed_user(session, uid="owner")
    stranger = await _seed_user(session, uid="stranger")
    skill = await employee_skill_service.create_skill(
        session,
        user=owner,
        data={"title": "技能", "steps": ["一步"], "acceptance": "可验收"},
    )
    await session.commit()

    with pytest.raises(Exception) as error:
        await employee_skill_service.get_manageable_skill(session, user=stranger, skill_id=skill.id)
    assert getattr(error.value, "status_code", None) == 404
