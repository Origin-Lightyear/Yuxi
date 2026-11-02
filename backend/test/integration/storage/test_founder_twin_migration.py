"""分身、会议和员工技能表在真实 PostgreSQL 上的幂等迁移测试。"""

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
    """旧库补齐分身、会议和技能表、唯一约束与外键，重复执行保持幂等。"""
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
            for table in (
                "meeting_decision_signers",
                "meeting_decisions",
                "meeting_messages",
                "meetings",
                "employee_skills",
            ):
                await connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
            await connection.execute(text("DROP TABLE experience_consultations"))
            await connection.execute(text("DROP TABLE experience_entries"))
            await connection.execute(text("DROP TABLE handoff_requests"))

        await manager.ensure_business_schema()
        await manager.ensure_business_schema()

        async with scoped_engine.connect() as connection:
            for table in (
                "experience_entries",
                "experience_consultations",
                "handoff_requests",
                "meetings",
                "meeting_messages",
                "meeting_decisions",
                "meeting_decision_signers",
                "employee_skills",
            ):
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
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_schema = :schema AND table_name = 'experience_consultations' "
                        "AND column_name = 'external_citations'"
                    ),
                    {"schema": schema},
                )
                == 1
            )
            for constraint_name in (
                "fk_experience_consultations_handoff",
                "ck_meetings_kind",
                "ck_meeting_messages_speaker_type",
                "ck_meeting_decisions_status",
                "ck_meeting_decision_signers_status",
                "ck_meeting_decision_signers_action",
                "ck_employee_skills_capture_method",
                "ck_employee_skills_status",
                "ck_employee_skills_handover_status",
                "fk_meeting_messages_meeting",
                "fk_meeting_decisions_meeting",
                "fk_meeting_decisions_experience",
                "fk_meeting_decision_signers_decision",
            ):
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM information_schema.table_constraints "
                            "WHERE constraint_schema = :schema AND constraint_name = :name"
                        ),
                        {"schema": schema, "name": constraint_name},
                    )
                    == 1
                ), constraint_name
            for index_name, table_name in (
                ("uq_meeting_decisions_meeting_code", "meeting_decisions"),
                ("uq_meeting_decision_signers_decision_uid", "meeting_decision_signers"),
                ("ix_meeting_messages_meeting_created", "meeting_messages"),
                ("ix_employee_skills_owner_status", "employee_skills"),
            ):
                assert (
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_indexes "
                            "WHERE schemaname = :schema AND tablename = :table AND indexname = :name"
                        ),
                        {"schema": schema, "table": table_name, "name": index_name},
                    )
                    == 1
                ), index_name
        assert BUSINESS_SCHEMA_VERSION == 14
    finally:
        await scoped_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()
