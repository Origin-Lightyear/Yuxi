from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio
from langchain_core.messages import HumanMessage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.agents import founder_twin as founder_twin_tools
from yuxi.services import founder_twin_service
from yuxi.storage.postgres.models_business import Agent, Base, ExperienceConsultation, ExperienceEntry, HandoffRequest

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


@pytest_asyncio.fixture()
async def founder_tools(session, monkeypatch):
    class FakePg:
        @asynccontextmanager
        async def get_async_session_context(self):
            yield session

    monkeypatch.setattr(founder_twin_tools, "pg_manager", FakePg())
    tools = {tool.name: tool for tool in founder_twin_tools.build_founder_twin_tools()}
    yield tools


def _runtime(*, run_id: str = "run-1", uid: str = "u1"):
    return SimpleNamespace(
        context=SimpleNamespace(
            agent_kind="founder_twin",
            agent_slug="boss",
            uid=uid,
            thread_id="thread-1",
            run_id=run_id,
        ),
        state={"messages": [HumanMessage("怎么定价")]},
    )


async def _seed_entry(
    session,
    *,
    code: str,
    title: str,
    vector: list[float],
    twin_slug: str = "boss",
    status: str = "confirmed",
) -> ExperienceEntry:
    entry = ExperienceEntry(
        twin_slug=twin_slug,
        code=code,
        title=title,
        situation="客户询价",
        judgment="先看成本再报价",
        boundary="不承诺未确认折扣",
        status=status,
        embedding=vector,
        embedding_model="mock",
    )
    session.add(entry)
    await session.commit()
    return entry


async def test_retrieve_tool_returns_confirmed_experience(session, founder_tools, monkeypatch):
    entry = await _seed_entry(session, code="pricing", title="定价经验", vector=[1.0, 0.0])

    async def fake_embed(text: str, *, model_spec: str | None = None):
        return [1.0, 0.0], "mock"

    monkeypatch.setattr(founder_twin_service, "embed_text", fake_embed)
    result = json.loads(await founder_tools["retrieve_experience"].coroutine(query="怎么定价", runtime=_runtime()))

    assert result["ok"] is True
    assert result["entries"][0]["id"] == entry.id
    assert result["entries"][0]["title"] == "定价经验"
    assert result["entries"][0]["score"] == pytest.approx(1.0)


async def test_submit_tool_requires_citations_and_records_once(session, founder_tools):
    first = await _seed_entry(session, code="pricing", title="定价经验", vector=[1.0, 0.0])
    second = await _seed_entry(session, code="delivery", title="交付经验", vector=[0.0, 1.0])
    first_id, second_id = first.id, second.id

    invalid = json.loads(
        await founder_tools["submit_consultation_answer"].coroutine(
            answer="随便报价", cited_entry_ids=[999999], confidence=0.9, runtime=_runtime()
        )
    )
    assert invalid["ok"] is False
    assert await session.scalar(select(ExperienceConsultation)) is None

    first_result = json.loads(
        await founder_tools["submit_consultation_answer"].coroutine(
            answer="先看成本再报价", cited_entry_ids=[first_id], confidence=0.8, runtime=_runtime()
        )
    )
    assert first_result["ok"] is True
    assert first_result["answer"] == "先看成本再报价"

    second_result = json.loads(
        await founder_tools["submit_consultation_answer"].coroutine(
            answer="先看成本再报价", cited_entry_ids=[first_id, second_id], confidence=0.9, runtime=_runtime()
        )
    )
    assert second_result["ok"] is True

    consultation = await session.scalar(select(ExperienceConsultation))
    assert await session.scalar(select(func.count()).select_from(ExperienceConsultation)) == 1
    assert consultation.run_id == "run-1"
    assert consultation.cited_entry_ids == [first_id, second_id]
    assert consultation.question == "怎么定价"
    first = await session.get(ExperienceEntry, first_id)
    second = await session.get(ExperienceEntry, second_id)
    assert first.citation_count == 1
    assert second.citation_count == 1


async def test_handoff_tool_creates_open_request_and_links_consultation(session, founder_tools):
    session.add(
        Agent(
            slug="boss",
            backend_id="ChatbotAgent",
            name="老板",
            config_json={},
            share_config={},
            created_by="owner-uid",
        )
    )
    await session.commit()

    result = json.loads(
        await founder_tools["create_handoff_request"].coroutine(
            question="组织架构怎么调整",
            reason="现有经验没有组织设计依据",
            runtime=_runtime(run_id="run-2", uid="u2"),
        )
    )

    assert result["ok"] is True
    handoff = await session.scalar(select(HandoffRequest))
    assert handoff.status == "open"
    assert handoff.assigned_to_uid == "owner-uid"
    consultation = await session.scalar(select(ExperienceConsultation))
    assert consultation.run_id == "run-2"
    assert consultation.handoff_id == handoff.id
    assert consultation.cited_entry_ids == []


async def test_submit_rejects_empty_other_twin_and_invalid_citations(session, founder_tools):
    await _seed_entry(session, code="pricing", title="定价经验", vector=[1.0, 0.0])
    other_twin = await _seed_entry(
        session,
        code="other",
        title="别的分身经验",
        vector=[1.0, 0.0],
        twin_slug="other",
    )
    invalid = await _seed_entry(
        session,
        code="invalid",
        title="失效经验",
        vector=[1.0, 0.0],
        status="invalid",
    )

    other_twin_id, invalid_id = other_twin.id, invalid.id
    cases = [([], "run-empty"), ([other_twin_id], "run-other"), ([invalid_id], "run-invalid")]
    for cited_entry_ids, run_id in cases:
        result = json.loads(
            await founder_tools["submit_consultation_answer"].coroutine(
                answer="没有依据的答案",
                cited_entry_ids=cited_entry_ids,
                confidence=0.5,
                runtime=_runtime(run_id=run_id),
            )
        )
        assert result["ok"] is False

    assert await session.scalar(select(func.count()).select_from(ExperienceConsultation)) == 0
