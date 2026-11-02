from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.services import founder_twin_service
from yuxi.services.founder_twin_service import (
    confirm_experience,
    cosine_similarity,
    record_consultation_answer,
    record_handoff,
    retrieve_experiences,
    update_experience_entry,
    validate_citations,
)
from yuxi.storage.postgres.models_business import Base, ExperienceEntry, HandoffRequest

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


def _entry(
    *,
    twin_slug: str = "boss",
    code: str,
    status: str = "confirmed",
    embedding: list[float] | None = None,
    title: str | None = None,
    embedding_model: str = "mock-embedding",
) -> ExperienceEntry:
    return ExperienceEntry(
        twin_slug=twin_slug,
        code=code,
        title=title or f"经验 {code}",
        situation="场景",
        judgment="判断",
        boundary="边界",
        status=status,
        embedding=embedding,
        embedding_model=embedding_model if embedding is not None else None,
    )


async def test_cosine_similarity_handles_normal_and_degenerate_vectors() -> None:
    assert cosine_similarity([1, 0], [1, 0]) == 1.0
    assert cosine_similarity([1, 0], [0, 1]) == 0.0
    assert cosine_similarity([0, 0], [1, 0]) == 0.0
    with pytest.raises(ValueError):
        cosine_similarity([1, 0], [1, 0, 0])


async def test_confirm_generates_embedding_and_failure_keeps_draft(session, monkeypatch) -> None:
    entry = _entry(code="draft", status="draft", embedding=None)
    session.add(entry)
    await session.commit()

    async def fake_embed(text: str, *, model_spec: str | None = None):
        assert "经验 draft" in text
        return [1.0, 0.0], "mock-embedding"

    monkeypatch.setattr(founder_twin_service, "embed_text", fake_embed)
    await confirm_experience(session, entry, actor_uid="u1")

    assert entry.status == "confirmed"
    assert entry.embedding == [1.0, 0.0]
    assert entry.embedding_model == "mock-embedding"
    assert entry.confirmed_by == "u1"
    assert entry.confirmed_at is not None

    broken = _entry(code="broken", status="draft", embedding=None)
    session.add(broken)
    await session.commit()

    async def failing_embed(text: str, *, model_spec: str | None = None):
        raise ValueError("embedding unavailable")

    monkeypatch.setattr(founder_twin_service, "embed_text", failing_embed)
    with pytest.raises(ValueError):
        await confirm_experience(session, broken, actor_uid="u1")
    assert broken.status == "draft"
    assert broken.embedding is None


async def test_validate_citations_requires_confirmed_entries_of_same_twin(session) -> None:
    confirmed = _entry(code="confirmed", embedding=[1.0, 0.0])
    draft = _entry(code="draft", status="draft", embedding=[1.0, 0.0])
    other_twin = _entry(twin_slug="other", code="other", embedding=[1.0, 0.0])
    session.add_all([confirmed, draft, other_twin])
    await session.commit()

    assert await validate_citations(session, twin_slug="boss", cited_entry_ids=[]) == []
    assert await validate_citations(session, twin_slug="boss", cited_entry_ids=[confirmed.id]) == [confirmed]
    assert await validate_citations(session, twin_slug="boss", cited_entry_ids=[confirmed.id, confirmed.id]) == [
        confirmed
    ]
    with pytest.raises(ValueError):
        await validate_citations(session, twin_slug="boss", cited_entry_ids=[draft.id])
    with pytest.raises(ValueError):
        await validate_citations(session, twin_slug="boss", cited_entry_ids=[other_twin.id])
    with pytest.raises(ValueError):
        await validate_citations(session, twin_slug="boss", cited_entry_ids=[999999])


async def test_retrieve_returns_top_confirmed_entries_for_same_twin(session, monkeypatch) -> None:
    session.add_all(
        [
            _entry(code="best", embedding=[1.0, 0.0]),
            _entry(code="second", embedding=[0.8, 0.2]),
            _entry(code="weak", embedding=[0.0, 1.0]),
            _entry(code="stale", embedding=[1.0, 0.0], embedding_model="old-model"),
            _entry(code="draft", status="draft", embedding=[1.0, 0.0]),
            _entry(twin_slug="other", code="other", embedding=[1.0, 0.0]),
        ]
    )
    await session.commit()

    async def fake_embed(text: str, *, model_spec: str | None = None):
        assert text == "怎么定价"
        return [1.0, 0.0], "mock-embedding"

    monkeypatch.setattr(founder_twin_service, "embed_text", fake_embed)
    results = await retrieve_experiences(session, twin_slug="boss", query="怎么定价", limit=2)

    assert [entry.code for entry, _score in results] == ["best", "second"]
    assert results[0][1] == pytest.approx(1.0)
    assert results[1][1] > results[0][1] - 0.1

    with pytest.raises(ValueError):
        await retrieve_experiences(session, twin_slug="boss", query="   ", limit=2)


async def test_invalid_experience_is_terminal(session) -> None:
    invalid = _entry(code="invalid", status="invalid", embedding=None)
    session.add(invalid)
    await session.commit()

    with pytest.raises(ValueError, match="只有草稿经验可以确认"):
        await confirm_experience(session, invalid, actor_uid="u1")
    with pytest.raises(ValueError, match="已失效经验不能编辑"):
        await update_experience_entry(session, invalid, data={"title": "新标题"}, actor_uid="u1")


async def test_handoff_and_cited_answer_are_mutually_exclusive(session) -> None:
    entry = _entry(code="pricing", embedding=[1.0, 0.0])
    session.add(entry)
    await session.commit()

    await record_handoff(
        session,
        twin_slug="boss",
        uid="u1",
        thread_id="thread-1",
        run_id="run-handoff",
        question="怎么调整组织",
        reason="没有组织经验",
        assigned_to_uid="boss-uid",
    )
    with pytest.raises(ValueError, match="已转交的问题不能直接提交答案"):
        await record_consultation_answer(
            session,
            twin_slug="boss",
            uid="u1",
            thread_id="thread-1",
            run_id="run-handoff",
            question="怎么调整组织",
            answer="直接答案",
            cited_entries=[entry],
            confidence=0.8,
        )

    await record_consultation_answer(
        session,
        twin_slug="boss",
        uid="u1",
        thread_id="thread-1",
        run_id="run-answer",
        question="怎么定价",
        answer="先看成本",
        cited_entries=[entry],
        confidence=0.8,
    )
    with pytest.raises(ValueError, match="已提交答案的问题不能转交"):
        await record_handoff(
            session,
            twin_slug="boss",
            uid="u1",
            thread_id="thread-1",
            run_id="run-answer",
            question="怎么定价",
            reason="不想回答",
            assigned_to_uid="boss-uid",
        )


async def test_handoff_for_same_run_is_idempotent(session) -> None:
    first = await record_handoff(
        session,
        twin_slug="boss",
        uid="u1",
        thread_id="thread-1",
        run_id="run-handoff",
        question="怎么调整组织",
        reason="没有组织经验",
        assigned_to_uid="boss-uid",
    )
    second = await record_handoff(
        session,
        twin_slug="boss",
        uid="u1",
        thread_id="thread-1",
        run_id="run-handoff",
        question="怎么调整组织",
        reason="没有组织经验",
        assigned_to_uid="boss-uid",
    )

    assert first.id == second.id
    assert len((await session.scalars(select(HandoffRequest))).all()) == 1
