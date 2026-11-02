import pytest
from sqlalchemy import text

from yuxi.storage.postgres.manager import pg_manager

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_digital_employee_migration_is_idempotent_and_preserves_existing_agents():
    """新增列可重复执行，既有内置智能体仍保持普通智能体身份。"""
    pg_manager.initialize()
    await pg_manager.ensure_business_schema()
    await pg_manager.ensure_business_schema()

    async with pg_manager.async_engine.connect() as conn:
        columns = await conn.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = 'agents'")
        )
        names = {row[0] for row in columns}
        assert {"kind", "digital_employee_profile", "tenant_id"} <= names

        indexes = await conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'agents'")
        )
        assert "ix_agents_tenant_id" in {row[0] for row in indexes}

        default_kind = await conn.scalar(text("SELECT kind FROM agents WHERE slug = 'default-chatbot'"))
        assert default_kind == "agent"
