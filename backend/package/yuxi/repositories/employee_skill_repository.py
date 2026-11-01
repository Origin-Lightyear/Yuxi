"""员工技能卡与交接状态的持久化查询。"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import EmployeeSkill, User


class EmployeeSkillRepository:
    """集中 employee_skills 的读取，避免 service 直接拼 SQL。"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, skill_id: int, *, for_update: bool = False) -> EmployeeSkill | None:
        """按主键读取技能卡；状态变更时锁定行。"""
        stmt = select(EmployeeSkill).where(EmployeeSkill.id == skill_id)
        if for_update:
            stmt = stmt.execution_options(populate_existing=True).with_for_update()
        return await self.db.scalar(stmt)

    async def list_all(self) -> list[EmployeeSkill]:
        """按更新时间倒序列出全部技能卡；可见性由 service 裁剪。"""
        stmt = select(EmployeeSkill).order_by(EmployeeSkill.updated_at.desc(), EmployeeSkill.id.desc())
        return list((await self.db.scalars(stmt)).all())

    async def list_handover_candidates(
        self, *, exclude_uid: str, keyword: str = "", limit: int = 30
    ) -> list[User]:
        """按关键词列出可指派接手人，排除当前归属人。"""
        stmt = select(User).where(User.is_deleted == 0, User.uid != exclude_uid)
        if keyword:
            pattern = f"%{keyword}%"
            stmt = stmt.where(or_(User.username.ilike(pattern), User.uid.ilike(pattern)))
        stmt = stmt.order_by(User.username.asc()).limit(max(1, min(limit, 100)))
        return list((await self.db.scalars(stmt)).all())
