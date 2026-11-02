from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import json

import pytest
import pytest_asyncio
from langchain_core.messages import HumanMessage, ToolMessage
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.agents import founder_twin as founder_twin_tools
from yuxi.agents.digital_employee import FOUNDER_TWIN_KIND
from yuxi.repositories.agent_repository import DEFAULT_SHARE_CONFIG
from yuxi.repositories.meeting_repository import MeetingRepository
from yuxi.services import founder_twin_service, meeting_service
from yuxi.services.meeting_service import _derived_signer_uids
from yuxi.storage.postgres.models_business import (
    Agent,
    Base,
    ExperienceConsultation,
    ExperienceEntry,
    Meeting,
    MeetingDecision,
    MeetingDecisionSigner,
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


async def _seed_user(session, *, uid: str, username: str, role: str = "user") -> User:
    user = User(uid=uid, username=username, password_hash="x", role=role)
    session.add(user)
    await session.commit()
    return user


async def _seed_twin(session, *, slug: str, name: str, owner_uid: str) -> Agent:
    agent = Agent(
        slug=slug,
        backend_id="chatbot",
        name=name,
        share_config=DEFAULT_SHARE_CONFIG,
        kind=FOUNDER_TWIN_KIND,
        digital_employee_profile={},
        created_by=owner_uid,
    )
    session.add(agent)
    await session.commit()
    return agent


async def _seed_entry(session, *, twin_slug: str, code: str, title: str, vector: list[float]) -> ExperienceEntry:
    entry = ExperienceEntry(
        twin_slug=twin_slug,
        code=code,
        title=title,
        situation="客户询问",
        judgment="先确认预算再报价",
        boundary="不承诺未确认折扣",
        status="confirmed",
        embedding=vector,
        embedding_model="mock",
    )
    session.add(entry)
    await session.commit()
    return entry


def _runtime(*, messages: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        state={"messages": messages or [HumanMessage(content="怎么定价")]},
        context=SimpleNamespace(
            agent_kind=FOUNDER_TWIN_KIND,
            agent_slug="boss",
            uid="admin",
            thread_id="thread-1",
            run_id="run-1",
        ),
    )


@pytest.fixture()
def founder_tools(session, monkeypatch):
    class FakePg:
        @asynccontextmanager
        async def get_async_session_context(self):
            yield session

    monkeypatch.setattr(founder_twin_tools, "pg_manager", FakePg())
    return {tool.name: tool for tool in founder_twin_tools.build_founder_twin_tools()}


async def test_ask_another_twin_returns_unreviewed_confirmed_entries(session, monkeypatch):
    await _seed_user(session, uid="admin", username="管理员", role="admin")
    await _seed_twin(session, slug="boss", name="老板", owner_uid="admin")
    await _seed_twin(session, slug="market", name="市场分身", owner_uid="admin")
    entry = await _seed_entry(session, twin_slug="market", code="price", title="市场定价", vector=[1.0, 0.0])

    async def fake_embed(text: str, *, model_spec: str | None = None):
        return [1.0, 0.0], "mock"

    monkeypatch.setattr(founder_twin_service, "embed_text", fake_embed)

    class FakePg:
        @asynccontextmanager
        async def get_async_session_context(self):
            yield session

    monkeypatch.setattr(founder_twin_tools, "pg_manager", FakePg())
    tools = {tool.name: tool for tool in founder_twin_tools.build_founder_twin_tools()}
    result = await tools["ask_another_twin"].coroutine(
        target_twin_slug="market",
        question="怎么报价",
        runtime=_runtime(),
    )

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["review_status"] == "unreviewed"
    assert payload["twin"]["slug"] == "market"
    assert [item["id"] for item in payload["entries"]] == [entry.id]


async def test_submit_accepts_external_citation_returned_by_ask(session, monkeypatch):
    await _seed_user(session, uid="admin", username="管理员", role="admin")
    await _seed_twin(session, slug="boss", name="老板", owner_uid="admin")
    await _seed_twin(session, slug="market", name="市场分身", owner_uid="admin")
    target_entry = await _seed_entry(
        session, twin_slug="market", code="price", title="市场定价", vector=[1.0, 0.0]
    )

    async def fake_embed(text: str, *, model_spec: str | None = None):
        return [1.0, 0.0], "mock"

    monkeypatch.setattr(founder_twin_service, "embed_text", fake_embed)

    class FakePg:
        @asynccontextmanager
        async def get_async_session_context(self):
            yield session

    monkeypatch.setattr(founder_twin_tools, "pg_manager", FakePg())
    tools = {tool.name: tool for tool in founder_twin_tools.build_founder_twin_tools()}

    ask_payload = json.loads(
        await tools["ask_another_twin"].coroutine(
            target_twin_slug="market",
            question="怎么报价",
            runtime=_runtime(),
        )
    )
    assert ask_payload["ok"] is True
    messages = [
        HumanMessage(content="怎么报价"),
        ToolMessage(
            content=json.dumps(ask_payload, ensure_ascii=False),
            name="ask_another_twin",
            tool_call_id="ask-1",
        ),
    ]
    submitted = json.loads(
        await tools["submit_consultation_answer"].coroutine(
            answer="市场分身建议先确认预算；该口径本人未复核。",
            cited_entry_ids=[],
            confidence=0.7,
            external_citations=[{"twin_slug": "market", "entry_id": target_entry.id}],
            runtime=_runtime(messages=messages),
        )
    )
    assert submitted["ok"] is True, submitted
    assert submitted["external_citations"] == [{"twin_slug": "market", "entry_id": target_entry.id}]

    consultation = await session.scalar(select(ExperienceConsultation))
    assert consultation is not None
    assert consultation.external_citations == [{"twin_slug": "market", "entry_id": target_entry.id}]


async def test_ask_another_twin_rejects_self_and_unasked_reference(session, monkeypatch):
    await _seed_user(session, uid="admin", username="管理员", role="admin")
    await _seed_twin(session, slug="boss", name="老板", owner_uid="admin")
    await _seed_twin(session, slug="market", name="市场分身", owner_uid="admin")
    target_entry = await _seed_entry(
        session, twin_slug="market", code="price", title="市场定价", vector=[1.0, 0.0]
    )

    class FakePg:
        @asynccontextmanager
        async def get_async_session_context(self):
            yield session

    monkeypatch.setattr(founder_twin_tools, "pg_manager", FakePg())
    tools = {tool.name: tool for tool in founder_twin_tools.build_founder_twin_tools()}

    self_result = json.loads(
        await tools["ask_another_twin"].coroutine(
            target_twin_slug="boss", question="怎么报价", runtime=_runtime()
        )
    )
    assert self_result["ok"] is False

    submit = json.loads(
        await tools["submit_consultation_answer"].coroutine(
            answer="市场分身建议先确认预算",
            cited_entry_ids=[],
            confidence=0.7,
            external_citations=[{"twin_slug": "market", "entry_id": target_entry.id}],
            runtime=_runtime(),
        )
    )
    assert submit["ok"] is False
    assert "ask_another_twin" in submit["error"]

    wrong_target = json.loads(
        await tools["submit_consultation_answer"].coroutine(
            answer="拿另一个分身的条目标成市场分身",
            cited_entry_ids=[],
            confidence=0.7,
            external_citations=[{"twin_slug": "ops", "entry_id": target_entry.id}],
            runtime=_runtime(
                messages=[
                    HumanMessage(content="怎么报价"),
                    ToolMessage(
                        content=json.dumps(
                            {
                                "ok": True,
                                "twin": {"slug": "market", "name": "市场分身"},
                                "entries": [{"id": target_entry.id, "title": "市场定价"}],
                            },
                            ensure_ascii=False,
                        ),
                        name="ask_another_twin",
                        tool_call_id="ask-2",
                    ),
                ]
            ),
        )
    )
    assert wrong_target["ok"] is False
    assert "ask_another_twin" in wrong_target["error"]


async def test_run_and_sign_meeting_creates_draft_experience(session, monkeypatch):
    await _seed_user(session, uid="admin", username="管理员", role="admin")
    await _seed_twin(session, slug="boss", name="老板", owner_uid="admin")

    async def fake_generate(**kwargs):
        return {
            "summary": "会议同意先确认客户预算，再给出折扣区间。",
            "consensus": ["先确认预算"],
            "divergences": [],
            "turns": [
                {
                    "participant": "boss",
                    "round": 1,
                    "content": "先确认预算，再报价。",
                    "citations": [],
                }
            ],
            "decisions": [
                {
                    "title": "报价前置确认预算",
                    "detail": "所有超过十万元的报价必须先确认客户预算。",
                    "impact": "适用于大客户报价。",
                    "proposer": "boss",
                }
            ],
        }

    monkeypatch.setattr(meeting_service, "generate_meeting_draft", fake_generate)
    user = await session.scalar(select(User).where(User.uid == "admin"))
    meeting = await meeting_service.create_meeting(
        session,
        user=user,
        kind="twin",
        topic="大客户报价流程",
        participant_slugs=["boss"],
        rounds=2,
    )
    await session.commit()

    ran = await meeting_service.run_meeting(session, user=user, meeting_id=meeting.id)
    await session.commit()
    assert ran.status == "awaiting_decisions"
    decision = await session.scalar(select(MeetingDecision))
    signer = await session.scalar(select(MeetingDecisionSigner))
    assert decision is not None and signer is not None
    assert signer.uid == "admin"
    assert signer.status == "pending"

    await meeting_service.sign_decision(
        session,
        user=user,
        meeting_id=meeting.id,
        decision_id=decision.id,
        action="approve",
        comment="同意",
    )
    await session.commit()

    refreshed = await session.get(Meeting, meeting.id)
    refreshed_decision = await session.get(MeetingDecision, decision.id)
    entry = await session.scalar(select(ExperienceEntry).where(ExperienceEntry.source_type == "meeting"))
    assert refreshed is not None and refreshed.status == "effective"
    assert refreshed_decision is not None and refreshed_decision.status == "effective"
    assert entry is not None
    assert entry.status == "draft"
    assert entry.twin_slug == "boss"
    assert refreshed_decision.experience_entry_id == entry.id


async def test_generation_failure_keeps_draft_with_visible_error(session, monkeypatch):
    await _seed_user(session, uid="admin", username="管理员", role="admin")
    await _seed_twin(session, slug="boss", name="老板", owner_uid="admin")

    async def fake_generate(**kwargs):
        raise meeting_service.MeetingGenerationError("模型返回结构无效")

    monkeypatch.setattr(meeting_service, "generate_meeting_draft", fake_generate)
    user = await session.scalar(select(User).where(User.uid == "admin"))
    meeting = await meeting_service.create_meeting(
        session,
        user=user,
        kind="twin",
        topic="大客户报价流程",
        participant_slugs=["boss"],
        rounds=1,
    )
    await session.commit()

    with pytest.raises(meeting_service.MeetingGenerationError):
        await meeting_service.run_meeting(session, user=user, meeting_id=meeting.id)
    await session.commit()

    refreshed = await session.get(Meeting, meeting.id)
    assert refreshed is not None
    assert refreshed.status == "draft"
    assert refreshed.generation_error == "模型返回结构无效"
    assert await session.scalar(select(MeetingDecision)) is None


async def test_generated_turns_are_clamped_to_configured_rounds():
    payload = {
        "summary": "形成结论",
        "turns": [{"participant": "boss", "round": 4, "content": "只有一轮", "citations": []}],
    }
    normalized = meeting_service._normalize_generated_payload(
        payload, {"boss"}, rounds=1
    )
    assert normalized["turns"][0]["round"] == 1


async def test_generated_payload_rejects_non_list_decisions():
    payload = {
        "summary": "形成结论",
        "turns": [{"participant": "boss", "round": 1, "content": "同意", "citations": []}],
        "decisions": {"title": "错误结构"},
    }
    with pytest.raises(meeting_service.MeetingGenerationError):
        meeting_service._normalize_generated_payload(payload, {"boss"}, rounds=1)


async def test_meeting_lock_refreshes_stale_identity_state(session):
    await _seed_user(session, uid="admin", username="管理员", role="admin")
    await _seed_twin(session, slug="boss", name="老板", owner_uid="admin")
    user = await session.scalar(select(User).where(User.uid == "admin"))
    meeting = await meeting_service.create_meeting(
        session,
        user=user,
        kind="twin",
        topic="锁刷新",
        participant_slugs=["boss"],
        rounds=1,
    )
    await session.commit()
    await session.execute(
        update(Meeting).where(Meeting.id == meeting.id).values(status="effective"),
        execution_options={"synchronize_session": False},
    )
    assert meeting.status == "draft"

    locked = await MeetingRepository(session).get_meeting(meeting.id, for_update=True)
    assert locked is meeting
    assert locked.status == "effective"


async def test_derived_signers_skip_deleted_owners_and_fall_back(session):
    await _seed_user(session, uid="active-owner", username="在岗负责人")
    deleted = User(
        uid="deleted-owner",
        username="已删除负责人",
        password_hash="x",
        role="user",
        is_deleted=1,
    )
    session.add(deleted)
    await session.commit()
    active_twin = await _seed_twin(session, slug="active-twin", name="在岗分身", owner_uid="active-owner")
    deleted_twin = await _seed_twin(session, slug="deleted-twin", name="失主分身", owner_uid="deleted-owner")

    assert await _derived_signer_uids(session, [active_twin, deleted_twin], "admin") == ["active-owner"]
    assert await _derived_signer_uids(session, [deleted_twin], "admin") == ["admin"]
