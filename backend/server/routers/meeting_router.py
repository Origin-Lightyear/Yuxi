"""分身会议与数字员工会议接口。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from server.utils.auth_middleware import get_db, get_required_user
from yuxi.repositories.founder_twin_repository import FounderTwinRepository
from yuxi.repositories.meeting_repository import MeetingRepository
from yuxi.services.meeting_service import (
    MAX_HUMAN_PARTICIPANTS,
    MAX_PARTICIPANTS,
    MAX_ROUNDS,
    MAX_SIGNERS,
    MEETING_KINDS,
    MEETING_KIND_DIGITAL_EMPLOYEE,
    MEETING_KIND_TWIN,
    MeetingConflict,
    MeetingGenerationError,
    add_human_message,
    build_serialized_decision_context,
    can_manage_meeting,
    create_meeting,
    get_visible_meeting,
    list_visible_meetings,
    participant_agents_for_meeting,
    run_meeting,
    sign_decision,
)
from yuxi.storage.postgres.models_business import Agent, Meeting, MeetingDecision, MeetingDecisionSigner, User
from yuxi.utils.datetime_utils import format_utc_datetime

meeting_router = APIRouter(prefix="/meetings", tags=["meeting"])

KIND_LABELS = {
    MEETING_KIND_TWIN: "分身会议",
    MEETING_KIND_DIGITAL_EMPLOYEE: "AI 会议",
}


class MeetingCreate(BaseModel):
    kind: Literal["twin", "digital_employee"]
    topic: str = Field(min_length=1, max_length=300)
    participant_slugs: list[str] = Field(min_length=1, max_length=MAX_PARTICIPANTS)
    human_uids: list[str] = Field(default_factory=list, max_length=MAX_HUMAN_PARTICIPANTS)
    signer_uids: list[str] | None = Field(None, max_length=MAX_SIGNERS)
    rounds: int = Field(2, ge=1, le=MAX_ROUNDS)


class MeetingRun(BaseModel):
    model_spec: str | None = None


class MeetingMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=3000)
    round: int | None = Field(None, ge=1, le=MAX_ROUNDS)


class DecisionSign(BaseModel):
    action: Literal["approve", "modify", "reject"]
    comment: str | None = Field(None, max_length=1000)
    text: str | None = Field(None, max_length=4000)


def _participant_payload(agent: Agent, *, kind: str, owner_names: dict[str, str]) -> dict:
    profile = agent.digital_employee_profile or {}
    return {
        "slug": agent.slug,
        "name": agent.name,
        "kind": kind,
        "owner_uid": agent.created_by,
        "owner_name": owner_names.get(agent.created_by) if agent.created_by else None,
        "position": profile.get("position") if kind == MEETING_KIND_DIGITAL_EMPLOYEE else None,
        "department": profile.get("department") if kind == MEETING_KIND_DIGITAL_EMPLOYEE else None,
    }


def _decision_payload(
    decision: MeetingDecision,
    *,
    signers: list[MeetingDecisionSigner],
    names: dict[str, str],
    user: User,
) -> dict:
    my_signer = next((item for item in signers if item.uid == str(user.uid)), None)
    return {
        "id": decision.id,
        "code": decision.code,
        "title": decision.title,
        "detail": decision.detail,
        "impact": decision.impact,
        "proposer_slug": decision.proposer_slug,
        "status": decision.status,
        "effective_text": decision.effective_text,
        "effective_at": format_utc_datetime(decision.effective_at),
        "experience_entry_id": decision.experience_entry_id,
        "signers": [
            {
                "uid": signer.uid,
                "name": names.get(signer.uid) or signer.uid,
                "status": signer.status,
                "action": signer.action,
                "comment": signer.comment,
                "signed_text": signer.signed_text,
                "signed_at": format_utc_datetime(signer.signed_at),
            }
            for signer in signers
        ],
        "can_sign": bool(
            decision.status == "pending" and my_signer is not None and my_signer.status == "pending"
        ),
        "my_action": my_signer.action if my_signer else None,
    }


async def _meeting_payload(
    db: AsyncSession,
    meeting: Meeting,
    agents: list[Agent],
    *,
    user: User,
    include_detail: bool,
) -> dict:
    """组装会议 DTO；名单和签字人姓名一次查询，避免逐条请求用户表。"""
    decisions, signers_by_decision, names = await build_serialized_decision_context(db, meeting)
    owner_uids = [agent.created_by for agent in agents if agent.created_by]
    if owner_uids:
        names.update(await FounderTwinRepository(db).get_user_names(owner_uids))
    repo = MeetingRepository(db)
    messages = await repo.list_messages(meeting.id) if include_detail else []
    payload = {
        "id": meeting.id,
        "kind": meeting.kind,
        "kind_label": KIND_LABELS.get(meeting.kind, meeting.kind),
        "topic": meeting.topic,
        "status": meeting.status,
        "participant_slugs": meeting.participant_slugs or [],
        "participants": [
            _participant_payload(agent, kind=meeting.kind, owner_names=names) for agent in agents
        ],
        "human_uids": meeting.human_uids or [],
        "signer_uids": meeting.signer_uids or [],
        "rounds": meeting.rounds,
        "summary": meeting.summary,
        "consensus": meeting.consensus or [],
        "divergences": meeting.divergences or [],
        "generation_error": meeting.generation_error,
        "created_by": meeting.created_by,
        "created_at": format_utc_datetime(meeting.created_at),
        "updated_at": format_utc_datetime(meeting.updated_at),
        "can_manage": can_manage_meeting(user, meeting, agents),
        "can_join": can_manage_meeting(user, meeting, agents)
        or str(user.uid) in (meeting.human_uids or [])
        or str(user.uid) in (meeting.signer_uids or []),
        "decision_count": len(decisions),
        "pending_decision_count": sum(1 for item in decisions if item.status == "pending"),
        "decisions": [
            _decision_payload(
                decision,
                signers=signers_by_decision.get(decision.id, []),
                names=names,
                user=user,
            )
            for decision in decisions
        ],
    }
    if include_detail:
        payload["messages"] = [
            {
                "id": message.id,
                "round": message.round,
                "speaker_type": message.speaker_type,
                "speaker_slug": message.speaker_slug,
                "speaker_uid": message.speaker_uid,
                "speaker_name": message.speaker_name,
                "content": message.content,
                "citations": message.citations or [],
                "created_at": format_utc_datetime(message.created_at),
            }
            for message in messages
        ]
    return payload


@meeting_router.get("")
async def list_meetings(
    kind: str | None = Query(None),
    limit: int = Query(100, ge=1, le=200),
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """列出当前用户可见的会议。"""
    if kind is not None and kind not in MEETING_KINDS:
        raise HTTPException(status_code=422, detail="kind 只支持 twin 或 digital_employee")
    items = await list_visible_meetings(db, user=current_user, kind=kind, limit=limit)
    return {
        "meetings": [
            await _meeting_payload(db, meeting, agents, user=current_user, include_detail=False)
            for meeting, agents in items
        ]
    }


@meeting_router.post("")
async def create_meeting_endpoint(
    payload: MeetingCreate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """创建会议草稿；纪要需要通过 run 显式生成。"""
    try:
        meeting = await create_meeting(
            db,
            user=current_user,
            kind=payload.kind,
            topic=payload.topic,
            participant_slugs=payload.participant_slugs,
            human_uids=payload.human_uids,
            signer_uids=payload.signer_uids,
            rounds=payload.rounds,
        )
        await db.commit()
        await db.refresh(meeting)
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    agents = await participant_agents_for_meeting(db, meeting)
    return {"meeting": await _meeting_payload(db, meeting, agents, user=current_user, include_detail=True)}


@meeting_router.get("/{meeting_id}")
async def get_meeting_endpoint(
    meeting_id: int,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """读取会议详情、发言和决议。"""
    meeting, agents = await get_visible_meeting(db, user=current_user, meeting_id=meeting_id)
    return {"meeting": await _meeting_payload(db, meeting, agents, user=current_user, include_detail=True)}


@meeting_router.post("/{meeting_id}/run")
async def run_meeting_endpoint(
    meeting_id: int,
    payload: MeetingRun | None = None,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """调用真实模型生成会议纪要和决议。"""
    try:
        meeting = await run_meeting(
            db,
            user=current_user,
            meeting_id=meeting_id,
            model_spec=payload.model_spec if payload else None,
        )
        await db.commit()
        await db.refresh(meeting)
    except MeetingGenerationError as exc:
        await db.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except MeetingConflict as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    agents = await participant_agents_for_meeting(db, meeting)
    return {"meeting": await _meeting_payload(db, meeting, agents, user=current_user, include_detail=True)}


@meeting_router.post("/{meeting_id}/messages")
async def add_meeting_message(
    meeting_id: int,
    payload: MeetingMessageCreate,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """人加入会议并写入一条发言。"""
    try:
        meeting = await add_human_message(
            db,
            user=current_user,
            meeting_id=meeting_id,
            content=payload.content,
            round_no=payload.round,
        )
        await db.commit()
        await db.refresh(meeting)
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    agents = await participant_agents_for_meeting(db, meeting)
    return {"meeting": await _meeting_payload(db, meeting, agents, user=current_user, include_detail=True)}


@meeting_router.post("/{meeting_id}/decisions/{decision_id}/sign")
async def sign_meeting_decision(
    meeting_id: int,
    decision_id: int,
    payload: DecisionSign,
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """指定负责人对会议决议签字。"""
    try:
        meeting = await sign_decision(
            db,
            user=current_user,
            meeting_id=meeting_id,
            decision_id=decision_id,
            action=payload.action,
            comment=payload.comment,
            text=payload.text,
        )
        await db.commit()
        await db.refresh(meeting)
    except MeetingConflict as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        await db.rollback()
        raise
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    agents = await participant_agents_for_meeting(db, meeting)
    return {"meeting": await _meeting_payload(db, meeting, agents, user=current_user, include_detail=True)}
