"""AgentMemoryRepository 的作用域过滤、幂等与容量截断测试。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.agent_memory_repository import AgentMemoryRepository
from yuxi.storage.postgres.models_business import Base

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


@pytest_asyncio.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        yield db
    await engine.dispose()


async def test_add_list_and_count_stay_scoped_and_ordered(session):
    repository = AgentMemoryRepository(session)
    for index in range(3):
        await repository.add(uid="u1", agent_slug="a1", content=f"记忆{index}", source_run_id=f"r{index}")

    rows = await repository.list_by_agent(uid="u1", agent_slug="a1")
    count = await repository.count_by_agent(uid="u1", agent_slug="a1")
    other_count = await repository.count_by_agent(uid="u1", agent_slug="a2")

    assert count == 3
    assert other_count == 0
    assert [row["source_run_id"] for row in rows] == ["r2", "r1", "r0"]
    assert all(row["uid"] == "u1" and row["agent_slug"] == "a1" for row in rows)


async def test_duplicate_source_run_id_raises_unique_violation(session):
    repository = AgentMemoryRepository(session)
    await repository.add(uid="u1", agent_slug="a1", content="首次抽取", source_run_id="r1")
    await session.commit()

    with pytest.raises(IntegrityError):
        await repository.add(uid="u1", agent_slug="a1", content="重复抽取", source_run_id="r1")
    await session.rollback()

    assert await repository.count_by_agent(uid="u1", agent_slug="a1") == 1


async def test_delete_requires_matching_uid_and_agent_slug(session):
    repository = AgentMemoryRepository(session)
    created = await repository.add(uid="u1", agent_slug="a1", content="归属记忆", source_run_id="r1")

    deleted = await repository.delete(uid="other", agent_slug="a1", memory_id=created["id"])
    wrong_agent = await repository.delete(uid="u1", agent_slug="a2", memory_id=created["id"])
    owned = await repository.delete(uid="u1", agent_slug="a1", memory_id=created["id"])

    assert deleted is False
    assert wrong_agent is False
    assert owned is True
    assert await repository.count_by_agent(uid="u1", agent_slug="a1") == 0


async def test_delete_oldest_over_limit_trims_only_overflow(session):
    repository = AgentMemoryRepository(session)
    for index in range(50):
        await repository.add(uid="u1", agent_slug="a1", content=f"记忆{index}", source_run_id=f"r{index}")

    untouched = await repository.delete_oldest_over_limit(uid="u1", agent_slug="a1", limit=50)
    await repository.add(uid="u1", agent_slug="a1", content="超限条目", source_run_id="r50")
    trimmed = await repository.delete_oldest_over_limit(uid="u1", agent_slug="a1", limit=50)

    rows = await repository.list_by_agent(uid="u1", agent_slug="a1")

    assert untouched == 0
    assert trimmed == 1
    assert len(rows) == 50
    assert rows[0]["source_run_id"] == "r50"
    assert all(row["source_run_id"] != "r0" for row in rows)
