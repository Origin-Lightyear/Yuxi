"""老板分身三张表在真实 PostgreSQL 上的幂等迁移测试。"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from yuxi.storage.postgres.manager import BUSINESS_SCHEMA_VERSION, PostgresManager

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本文件自行创建隔离 Schema，不依赖运行中的 API。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """隔离 Schema 测试没有 HTTP 资源需要清理。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """隔离 Schema 测试没有 Sandbox 资源需要清理。"""
    yield


def _scoped_manager(engine) -> PostgresManager:
    """创建不触碰进程单例的隔离 manager。"""
    manager = object.__new__(PostgresManager)
    PostgresManager.__init__(manager)
    manager.async_engine = engine
    manager._initialized = True
    return manager


async def test_founder_twin_tables_migration_is_idempotent() -> None:
    """旧库补齐三张表、唯一约束和外键，重复执行保持幂等。"""
    schema = f"pytest_founder_twin_{uuid.uuid4().hex[:16]}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_engine = create_async_engine(
        os.environ["POSTGRES_URL"],
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    manager = _scoped_manager(scoped_engine)
    try:
        await manager.create_business_tables()
        async with scoped_engine.begin() as connection:
            await connection.execute(text("DROP TABLE experience_consultations"))
            await connection.execute(text("DROP TABLE experience_entries"))
            await connection.execute(text("DROP TABLE handoff_requests"))

        await manager.ensure_business_schema()
        await manager.ensure_business_schema()

        async with scoped_engine.connect() as connection:
            for table in ("experience_entries", "experience_consultations", "handoff_requests"):
                assert await connection.scalar(text(f"SELECT to_regclass('{table}')")) == table
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_indexes "
                        "WHERE schemaname = :schema AND tablename = 'experience_entries' "
                        "AND indexname = 'uq_experience_entries_twin_code'"
                    ),
                    {"schema": schema},
                )
                == 1
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_indexes "
                        "WHERE schemaname = :schema AND tablename = 'experience_consultations' "
                        "AND indexname = 'uq_experience_consultations_run_id'"
                    ),
                    {"schema": schema},
                )
                == 1
            )
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM information_schema.table_constraints "
                        "WHERE constraint_schema = :schema "
                        "AND constraint_name = 'fk_experience_consultations_handoff'"
                    ),
                    {"schema": schema},
                )
                == 1
            )
        assert BUSINESS_SCHEMA_VERSION == 12
    finally:
        await scoped_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
