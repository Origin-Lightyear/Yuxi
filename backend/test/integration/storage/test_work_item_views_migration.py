"""待办已读表在真实 PostgreSQL 上的幂等迁移测试。"""

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


async def test_work_item_views_migration_is_idempotent() -> None:
    """旧库补齐已读表与唯一索引，重复执行保持幂等。"""
    schema = f"pytest_work_item_views_{uuid.uuid4().hex[:16]}"
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
            await connection.execute(text("DROP TABLE work_item_views"))

        await manager.ensure_business_schema()
        await manager.ensure_business_schema()

        async with scoped_engine.connect() as connection:
            assert await connection.scalar(text("SELECT to_regclass('work_item_views')")) == "work_item_views"
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_indexes "
                        "WHERE schemaname = :schema AND tablename = 'work_item_views' "
                        "AND indexname = 'uq_work_item_views_uid_item_key'"
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
