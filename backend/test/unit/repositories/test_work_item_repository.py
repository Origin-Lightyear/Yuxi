from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.work_item_repository import WorkItemRepository
from yuxi.storage.postgres.models_business import (
    Agent,
    AgentRun,
    Base,
    Conversation,
    HandoffRequest,
    Message,
    User,
)

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


async def _seed(session) -> None:
    base = datetime(2026, 1, 1, 9, 0, 0)
    session.add(
        Agent(
            slug="assistant",
            backend_id="ChatbotAgent",
            name="执行助手",
            description="",
            pics=[],
            config_json={},
            share_config={},
        )
    )
    session.add_all(
        [
            Conversation(
                thread_id="thread-new",
                project_id="project-1",
                uid="user-1",
                agent_id="assistant",
                title="季度复盘",
            ),
            Conversation(
                thread_id="thread-pending",
                project_id="project-1",
                uid="user-1",
                agent_id="assistant",
                title=None,
            ),
        ]
    )
    await session.flush()
    session.add_all(
        [
            Message(conversation_id=1, role="user", content="帮我整理季度复盘", run_id="run-old"),
            Message(conversation_id=2, role="user", content="发一封提醒邮件", run_id="run-pending"),
        ]
    )
    await session.flush()
    session.add_all(
        [
            AgentRun(
                id="run-old",
                conversation_thread_id="thread-new",
                runtime_scope_id="thread-new",
                agent_slug="assistant",
                uid="user-1",
                status="completed",
                request_id="req-old",
                source="chat",
                conversation_id=1,
                run_type="chat",
                input_message_id=1,
                input_payload={},
                created_at=base,
                updated_at=base,
            ),
            AgentRun(
                id="run-new",
                conversation_thread_id="thread-new",
                runtime_scope_id="thread-new",
                agent_slug="assistant",
                uid="user-1",
                status="interrupted",
                request_id="req-new",
                source="chat",
                conversation_id=1,
                run_type="resume",
                created_by_run_id="run-old",
                input_message_id=1,
                input_payload={},
                error_type="human_approval_required",
                created_at=base + timedelta(minutes=3),
                updated_at=base + timedelta(minutes=3),
            ),
            AgentRun(
                id="run-pending",
                conversation_thread_id="thread-pending",
                runtime_scope_id="thread-pending",
                agent_slug="assistant",
                uid="user-1",
                status="pending",
                request_id="req-pending",
                source="scheduled_agent",
                conversation_id=2,
                run_type="chat",
                input_message_id=2,
                input_payload={},
                created_at=base + timedelta(minutes=2),
                updated_at=base + timedelta(minutes=2),
            ),
            AgentRun(
                id="run-subagent",
                conversation_thread_id="child-thread",
                runtime_scope_id="thread-new",
                agent_slug="assistant",
                uid="user-1",
                status="completed",
                request_id="req-subagent",
                source="subagent",
                run_type="subagent",
                created_by_run_id="run-old",
                input_payload={},
                created_at=base + timedelta(minutes=4),
                updated_at=base + timedelta(minutes=4),
            ),
            AgentRun(
                id="run-agent-call",
                conversation_thread_id="agent-call-thread",
                runtime_scope_id="agent-call-thread",
                agent_slug="assistant",
                uid="user-1",
                status="completed",
                request_id="req-agent-call",
                source="agent_call",
                run_type="chat",
                input_payload={},
                created_at=base + timedelta(minutes=5),
                updated_at=base + timedelta(minutes=5),
            ),
            AgentRun(
                id="run-other-user",
                conversation_thread_id="other-thread",
                runtime_scope_id="other-thread",
                agent_slug="assistant",
                uid="user-2",
                status="completed",
                request_id="req-other",
                source="chat",
                run_type="chat",
                input_payload={},
                created_at=base + timedelta(minutes=6),
                updated_at=base + timedelta(minutes=6),
            ),
        ]
    )
    await session.commit()


async def test_list_latest_uses_one_latest_run_per_thread(session):
    await _seed(session)
    repo = WorkItemRepository(session)

    rows, total = await repo.list_items(
        "user-1", managed_twin_slugs=[], status=None, limit=50, offset=0
    )

    assert [row.run_id for row in rows] == ["run-new", "run-pending"]
    assert total == 2
    assert rows[0].title == "季度复盘"
    assert rows[0].agent_name == "执行助手"
    assert rows[1].input_text == "发一封提醒邮件"
    assert (
        await repo.get_item("user-1", managed_twin_slugs=[], source_type="agent_run", source_id="run-old")
        is None
    )
    assert (
        await repo.get_item("user-1", managed_twin_slugs=[], source_type="agent_run", source_id="run-new")
    ).work_status == "waiting_approval"


async def test_list_latest_filters_by_derived_status_and_summary(session):
    await _seed(session)
    repo = WorkItemRepository(session)

    rows, total = await repo.list_items(
        "user-1", managed_twin_slugs=[], status="waiting_approval", limit=50, offset=0
    )

    assert [row.run_id for row in rows] == ["run-new"]
    assert total == 1
    assert await repo.summary("user-1", managed_twin_slugs=[]) == {
        "queued": 1,
        "waiting_approval": 1,
    }
    assert await repo.unread_count("user-1", managed_twin_slugs=[]) == 2
    await repo.mark_viewed("user-1", "run:run-new")
    await session.commit()
    assert await repo.unread_count("user-1", managed_twin_slugs=[]) == 1


async def test_mark_viewed_is_idempotent_and_isolated_by_user(session):
    await _seed(session)
    repo = WorkItemRepository(session)

    await repo.mark_viewed("user-1", "run:run-new")
    await repo.mark_viewed("user-1", "run:run-new")

    assert await repo.viewed_keys("user-1", ["run:run-new", "run:run-pending"]) == {
        "run:run-new"
    }
    assert await repo.viewed_keys("user-2", ["run:run-new"]) == set()


async def test_handoff_items_are_visible_to_requester_and_managed_twin(session):
    base = datetime(2026, 1, 2, 9, 0, 0)
    session.add_all(
        [
            Agent(
                slug="boss",
                backend_id="ChatbotAgent",
                name="老板分身",
                description="",
                pics=[],
                config_json={},
                share_config={},
                kind="founder_twin",
                created_by="manager",
            ),
            User(uid="user-1", username="张三", password_hash="x", role="user"),
            User(uid="user-2", username="李四", password_hash="x", role="user"),
        ]
    )
    await session.flush()
    session.add(
        HandoffRequest(
            twin_slug="boss",
            uid="user-1",
            question="组织架构怎么调整",
            status="open",
            created_at=base,
        )
    )
    await session.commit()
    repo = WorkItemRepository(session)

    rows, total = await repo.list_items("user-1", managed_twin_slugs=[], status=None, limit=50, offset=0)
    assert total == 1
    assert rows[0].source_type == "handoff"
    assert rows[0].work_status == "running"
    assert rows[0].agent_slug == "boss"
    assert rows[0].agent_name == "老板分身"
    assert rows[0].title == "组织架构怎么调整"
    assert rows[0].initiator_name == "张三"
    assert rows[0].thread_id is None
    assert rows[0].run_id is None
    assert await repo.get_item("user-2", managed_twin_slugs=[], source_type="handoff", source_id=str(rows[0].source_id)) is None
    assert (
        await repo.get_item(
            "user-2",
            managed_twin_slugs=["boss"],
            source_type="handoff",
            source_id=str(rows[0].source_id),
        )
    ).source_id == rows[0].source_id
    assert await repo.summary("user-1", managed_twin_slugs=[]) == {"running": 1}
    assert await repo.unread_count("manager", managed_twin_slugs=["boss"]) == 1
