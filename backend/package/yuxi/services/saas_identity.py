"""SaaS 员工身份上下文。

SaaS 登录时把 Tenant 侧的 tenant_id / employee_id / department_id 持久化到 user_config，
供双写同步与定时任务执行时构造 X-Tenant-Id / X-Employee-Id 请求头。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import UserConfig
from yuxi.utils.datetime_utils import utc_now_naive


@dataclass(frozen=True, slots=True)
class SaasEmployeeContext:
    """员工在某租户下的身份。"""

    uid: str
    tenant_id: int
    employee_id: int
    tenant_department_id: int | None = None

    def headers(self) -> dict[str, str]:
        return {"X-Tenant-Id": str(self.tenant_id), "X-Employee-Id": str(self.employee_id)}


async def save_saas_employee_context(
    db: AsyncSession,
    uid: str,
    tenant_id: int,
    employee_id: int,
    tenant_department_id: int | None = None,
) -> None:
    """登录成功后持久化员工身份（upsert user_config）。"""
    now = utc_now_naive()
    result = await db.execute(
        update(UserConfig)
        .where(UserConfig.uid == uid)
        .values(
            tenant_id=tenant_id,
            employee_id=employee_id,
            tenant_department_id=tenant_department_id,
            updated_at=now,
        )
    )
    if result.rowcount == 0:
        db.add(
            UserConfig(
                uid=uid,
                tenant_id=tenant_id,
                employee_id=employee_id,
                tenant_department_id=tenant_department_id,
                created_at=now,
                updated_at=now,
            )
        )


async def get_saas_employee_context(db: AsyncSession, uid: str) -> SaasEmployeeContext | None:
    """读取员工身份；未绑定 SaaS 身份（非 SaaS 用户或未登录过）返回 None。"""
    result = await db.execute(select(UserConfig).filter(UserConfig.uid == uid))
    record = result.scalar_one_or_none()
    if record is None or record.tenant_id is None or record.employee_id is None:
        return None
    return SaasEmployeeContext(
        uid=uid,
        tenant_id=record.tenant_id,
        employee_id=record.employee_id,
        tenant_department_id=record.tenant_department_id,
    )
