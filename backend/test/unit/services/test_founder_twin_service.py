from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.services import founder_twin_service
from yuxi.services.founder_twin_service import (
    confirm_experience,
    cosine_similarity,
    retrieve_experiences,
    validate_citations,
)
from yuxi.storage.postgres.models_business import Base, ExperienceEntry

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
        embedding_model="mock-embedding" if embedding is not None else None,
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
