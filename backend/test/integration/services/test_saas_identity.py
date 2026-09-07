"""SaaS 员工身份持久化集成测试（依赖运行中的 PostgreSQL）。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select

from yuxi.services.saas_identity import get_saas_employee_context, save_saas_employee_context
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import User, UserConfig

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture
async def saas_user_uid():
    """取一个真实本地用户做身份绑定载体，测试后还原为未绑定。"""
    async with pg_manager.get_async_session_context() as db:
        result = await db.execute(select(User).limit(1))
        user = result.scalar_one_or_none()
        if user is None:
            pytest.skip("数据库中无用户，跳过 SaaS 身份集成测试")
        uid = user.uid

    yield uid

    async with pg_manager.get_async_session_context() as db:
        result = await db.execute(select(UserConfig).where(UserConfig.uid == uid))
        record = result.scalar_one_or_none()
        if record is not None:
            record.tenant_id = None
            record.employee_id = None
            await db.commit()


async def test_save_and_read_employee_context_roundtrip(saas_user_uid):
    async with pg_manager.get_async_session_context() as db:
        assert await get_saas_employee_context(db, saas_user_uid) is None

        await save_saas_employee_context(db, saas_user_uid, tenant_id=100, employee_id=200)
        await db.commit()

        ctx = await get_saas_employee_context(db, saas_user_uid)

    assert ctx is not None
    assert ctx.uid == saas_user_uid
    assert ctx.tenant_id == 100
    assert ctx.employee_id == 200
    assert ctx.headers() == {"X-Tenant-Id": "100", "X-Employee-Id": "200"}
