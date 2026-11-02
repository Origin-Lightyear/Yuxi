"""分身会议与数字员工会议：生成纪要、人工发言、决议签字和生效回写。"""

from __future__ import annotations

import json
from typing import Any

import json_repair
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.agents.digital_employee import (
    DIGITAL_EMPLOYEE_KIND,
    FOUNDER_TWIN_KIND,
    STATUS_ACTIVE,
)
from yuxi.config.options import system_options
from yuxi.models.chat import select_model
from yuxi.repositories.agent_repository import (
    AgentRepository,
    user_can_access_agent,
    user_can_manage_agent,
)
from yuxi.repositories.founder_twin_repository import FounderTwinRepository
from yuxi.repositories.meeting_repository import MeetingRepository
from yuxi.services.digital_employee_service import (
    digital_employee_is_visible,
    digital_employee_status,
)
from yuxi.services.founder_twin_service import create_experience_entry
from yuxi.storage.postgres.models_business import Agent, Meeting, User
from yuxi.utils.datetime_utils import utc_now_naive

MEETING_KIND_TWIN = "twin"
MEETING_KIND_DIGITAL_EMPLOYEE = "digital_employee"
MEETING_KINDS = (MEETING_KIND_TWIN, MEETING_KIND_DIGITAL_EMPLOYEE)

MEETING_STATUS_DRAFT = "draft"
MEETING_STATUS_AWAITING_DECISIONS = "awaiting_decisions"
MEETING_STATUS_EFFECTIVE = "effective"
MEETING_STATUS_REJECTED = "rejected"

DECISION_STATUS_PENDING = "pending"
DECISION_STATUS_EFFECTIVE = "effective"
DECISION_STATUS_REJECTED = "rejected"
SIGNER_STATUS_PENDING = "pending"
SIGNER_STATUS_APPROVED = "approved"
SIGNER_STATUS_REJECTED = "rejected"

MAX_PARTICIPANTS = 6
MAX_HUMAN_PARTICIPANTS = 6
MAX_SIGNERS = 12
MAX_ROUNDS = 4
MAX_DECISIONS = 5
MAX_TURNS = 24
MAX_SUMMARY_CHARS = 4000
MAX_MESSAGE_CHARS = 3000
MAX_TITLE_CHARS = 200
MAX_DETAIL_CHARS = 4000
MAX_EXPERIENCE_CONTEXT = 20

MEETING_SYSTEM_PROMPT = """你是企业 AI 会议主持人。你要组织一场有明确产出的多角色会议，而不是写泛泛的讨论稿。
只输出一个 JSON 对象，不要输出 Markdown 代码块或额外解释。JSON 结构：
{
  "summary": "会议结论摘要",
  "consensus": ["已达成的共识"],
  "divergences": [{"topic": "分歧点", "positions": [{"participant": "参与者 slug", "view": "观点"}]}],
  "turns": [{"participant": "参与者 slug", "round": 1, "content": "该角色的发言", "citations": [{"entry_id": 1}]}],
  "decisions": [{"title": "决议标题", "detail": "决议内容", "impact": "影响和适用范围", "proposer": "参与者 slug"}]
}
规则：
- 每个参与者至少发言一次，发言要符合其岗位/经验边界。
- 分身只能引用提供的已确认经验条目；没有依据时明确说明。
- 决议必须是可签字、可执行的判断，不要写“继续讨论”。
- 没有足够依据形成决议时，decisions 返回空数组。
- 同一轮已经请教两个其他分身仍没有确定答案时，停止继续请教，明确说明需要转交负责人。
- 最多 5 条决议，最多 24 条发言。"""


class MeetingConflict(ValueError):
    """会议状态或权限不允许当前动作。"""


class MeetingGenerationError(ValueError):
    """会议纪要生成失败；会议保持草稿，可重试。"""


def _is_admin(user: User) -> bool:
    return user.role in {"admin", "superadmin"}


def _dedupe(values: list[str] | None) -> list[str]:
    result: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


async def _load_participant_agents(
    db: AsyncSession,
    *,
    user: User,
    kind: str,
    slugs: list[str],
) -> list[Agent]:
    """按会议类型读取参与者，并校验可见性和在岗状态。"""
    unique_slugs = _dedupe(slugs)
    if not unique_slugs:
        raise ValueError("至少选择一位参与者")
    if len(unique_slugs) > MAX_PARTICIPANTS:
        raise ValueError(f"参与者最多 {MAX_PARTICIPANTS} 位")
    agents = list((await db.scalars(select(Agent).where(Agent.slug.in_(unique_slugs)))).all())
    by_slug = {agent.slug: agent for agent in agents}
    if len(by_slug) != len(unique_slugs):
        raise HTTPException(status_code=422, detail="存在不存在的会议参与者")

    ordered: list[Agent] = []
    for slug in unique_slugs:
        agent = by_slug[slug]
        if kind == MEETING_KIND_TWIN:
            if getattr(agent, "kind", "agent") != FOUNDER_TWIN_KIND:
                raise HTTPException(status_code=422, detail="分身会议只能选择老板分身")
            if not user_can_access_agent(user, agent):
                raise HTTPException(status_code=404, detail="会议参与者不存在")
        else:
            if getattr(agent, "kind", "agent") != DIGITAL_EMPLOYEE_KIND:
                raise HTTPException(status_code=422, detail="AI 会议只能选择数字员工")
            if not digital_employee_is_visible(user, agent):
                raise HTTPException(status_code=404, detail="会议参与者不存在")
            if digital_employee_status(agent) != STATUS_ACTIVE:
                raise HTTPException(status_code=409, detail="数字员工未在岗，不能参加会议")
        ordered.append(agent)
    return ordered


async def _load_users(db: AsyncSession, uids: list[str]) -> list[User]:
    """按 UID 读取有效用户；显式传入不存在的用户时失败。"""
    unique = _dedupe(uids)
    if not unique:
        return []
    users = list((await db.scalars(select(User).where(User.uid.in_(unique), User.is_deleted == 0))).all())
    if len(users) != len(unique):
        raise HTTPException(status_code=422, detail="存在不存在的会议相关人员")
    return users


async def _derived_signer_uids(
    db: AsyncSession, agents: list[Agent], fallback_uid: str
) -> list[str]:
    """默认由仍在岗的参与者归属人签字；没有可用归属人时回落到创建人。"""
    candidates = _dedupe([str(getattr(agent, "created_by", "") or "") for agent in agents])
    if not candidates:
        return [fallback_uid]
    active_uids = set(
        (
            await db.scalars(
                select(User.uid).where(User.uid.in_(candidates), User.is_deleted == 0)
            )
        ).all()
    )
    return [uid for uid in candidates if uid in active_uids] or [fallback_uid]


async def create_meeting(
    db: AsyncSession,
    *,
    user: User,
    kind: str,
    topic: str,
    participant_slugs: list[str],
    human_uids: list[str] | None = None,
    signer_uids: list[str] | None = None,
    rounds: int = 2,
) -> Meeting:
    """创建会议草稿；可见性和发起权限在后端校验。"""
    if kind not in MEETING_KINDS:
        raise ValueError("会议类型只支持 twin 或 digital_employee")
    normalized_topic = str(topic or "").strip()
    if not normalized_topic:
        raise ValueError("会议主题不能为空")
    if len(normalized_topic) > 300:
        raise ValueError("会议主题不能超过 300 个字符")
    normalized_rounds = int(rounds or 2)
    if not 1 <= normalized_rounds <= MAX_ROUNDS:
        raise ValueError(f"会议轮次必须在 1 到 {MAX_ROUNDS} 之间")

    agents = await _load_participant_agents(db, user=user, kind=kind, slugs=participant_slugs)
    if not _is_admin(user) and not any(user_can_manage_agent(user, agent) for agent in agents):
        raise HTTPException(status_code=403, detail="需要管理员或参与者负责人权限才能发起会议")

    normalized_humans = _dedupe(human_uids or [])
    if len(normalized_humans) > MAX_HUMAN_PARTICIPANTS:
        raise ValueError(f"人工参会人最多 {MAX_HUMAN_PARTICIPANTS} 位")
    humans = await _load_users(db, normalized_humans)
    normalized_explicit_signers = _dedupe(signer_uids or [])
    if len(normalized_explicit_signers) > MAX_SIGNERS:
        raise ValueError(f"决议签字人最多 {MAX_SIGNERS} 位")
    explicit_signers = await _load_users(db, normalized_explicit_signers)
    resolved_signers = (
        [item.uid for item in explicit_signers]
        if explicit_signers
        else await _derived_signer_uids(db, agents, str(user.uid))
    )
    if not resolved_signers:
        raise ValueError("会议至少需要一位决议签字人")

    return await MeetingRepository(db).create_meeting(
        kind=kind,
        topic=normalized_topic,
        status=MEETING_STATUS_DRAFT,
        participant_slugs=[agent.slug for agent in agents],
        human_uids=[item.uid for item in humans],
        signer_uids=resolved_signers,
        rounds=normalized_rounds,
        summary="",
        consensus=[],
        divergences=[],
        created_by=str(user.uid),
    )


async def participant_agents_for_meeting(db: AsyncSession, meeting: Meeting) -> list[Agent]:
    """按会议保存的顺序返回参与者 Agent。"""
    slugs = _dedupe(meeting.participant_slugs or [])
    if not slugs:
        return []
    agents = list((await db.scalars(select(Agent).where(Agent.slug.in_(slugs)))).all())
    by_slug = {agent.slug: agent for agent in agents}
    return [by_slug[slug] for slug in slugs if slug in by_slug]


def can_view_meeting(user: User, meeting: Meeting, agents: list[Agent]) -> bool:
    """会议只对管理员、创建人、参与者负责人、人工参会人和签字人可见。"""
    uid = str(user.uid)
    if _is_admin(user) or meeting.created_by == uid:
        return True
    if uid in (meeting.human_uids or []) or uid in (meeting.signer_uids or []):
        return True
    return any(user_can_manage_agent(user, agent) for agent in agents)


def can_manage_meeting(user: User, meeting: Meeting, agents: list[Agent]) -> bool:
    """生成纪要、人工发言和推进会议需要管理员或参与者负责人权限。"""
    if _is_admin(user) or meeting.created_by == str(user.uid):
        return True
    return any(user_can_manage_agent(user, agent) for agent in agents)


async def get_visible_meeting(db: AsyncSession, *, user: User, meeting_id: int) -> tuple[Meeting, list[Agent]]:
    """读取当前用户可见的会议；不存在和不可见统一 404。"""
    meeting = await MeetingRepository(db).get_meeting(meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="会议不存在")
    agents = await participant_agents_for_meeting(db, meeting)
    if not can_view_meeting(user, meeting, agents):
        raise HTTPException(status_code=404, detail="会议不存在")
    return meeting, agents


async def list_visible_meetings(
    db: AsyncSession, *, user: User, kind: str | None = None, limit: int = 100
) -> list[tuple[Meeting, list[Agent]]]:
    """列出可见会议；权限和类型同时生效。"""
    if kind is not None and kind not in MEETING_KINDS:
        raise ValueError("会议类型只支持 twin 或 digital_employee")
    meetings = await MeetingRepository(db).list_meetings(kind=kind, limit=limit)
    result: list[tuple[Meeting, list[Agent]]] = []
    for meeting in meetings:
        agents = await participant_agents_for_meeting(db, meeting)
        if can_view_meeting(user, meeting, agents):
            result.append((meeting, agents))
    return result


async def _participant_context(db: AsyncSession, meeting: Meeting, agents: list[Agent]) -> list[dict[str, Any]]:
    """把分身经验和数字员工岗位档案整理成生成器输入。"""
    contexts: list[dict[str, Any]] = []
    repo = FounderTwinRepository(db)
    for agent in agents:
        context: dict[str, Any] = {
            "slug": agent.slug,
            "name": agent.name,
            "owner_uid": getattr(agent, "created_by", None),
        }
        if meeting.kind == MEETING_KIND_TWIN:
            entries = await repo.list_entries(agent.slug, status="confirmed")
            context["entries"] = [
                {
                    "id": entry.id,
                    "code": entry.code,
                    "title": entry.title,
                    "situation": entry.situation,
                    "judgment": entry.judgment,
                    "boundary": entry.boundary,
                }
                for entry in entries[:MAX_EXPERIENCE_CONTEXT]
            ]
        else:
            profile = agent.digital_employee_profile or {}
            context["profile"] = {
                "position": profile.get("position") or "",
                "department": profile.get("department") or "",
                "responsibilities": profile.get("responsibilities") or "",
                "capabilities": profile.get("capabilities") or [],
                "boundaries": profile.get("boundaries") or "",
                "acceptance": profile.get("acceptance") or "",
            }
        contexts.append(context)
    return contexts


def _clip(value: Any, maximum: int) -> str:
    text = str(value or "").strip()
    return text[:maximum]


def _string_list(value: Any, *, maximum: int = 10, item_maximum: int = 500) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _clip(item, item_maximum)
        if text:
            result.append(text)
        if len(result) >= maximum:
            break
    return result


def _normalize_divergences(value: Any, participant_slugs: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value[:10]:
        if not isinstance(item, dict):
            continue
        topic = _clip(item.get("topic"), 200)
        positions: list[dict[str, str]] = []
        for position in item.get("positions") or []:
            if not isinstance(position, dict):
                continue
            participant = _clip(position.get("participant"), 80)
            if participant not in participant_slugs:
                continue
            view = _clip(position.get("view"), 1000)
            if view:
                positions.append({"participant": participant, "view": view})
        if topic and positions:
            result.append({"topic": topic, "positions": positions})
    return result


def _normalize_generated_payload(
    payload: Any, participant_slugs: set[str], *, rounds: int
) -> dict[str, Any]:
    """校验模型 JSON 的结构；引用条目留给 run_meeting 做数据库校验。"""
    if not isinstance(payload, dict):
        raise MeetingGenerationError("会议模型没有返回 JSON 对象")
    summary = _clip(payload.get("summary"), MAX_SUMMARY_CHARS)
    if not summary:
        raise MeetingGenerationError("会议模型没有返回结论摘要")

    max_rounds = max(1, min(int(rounds), MAX_ROUNDS))
    turns: list[dict[str, Any]] = []
    raw_turns = payload.get("turns")
    if not isinstance(raw_turns, list):
        raise MeetingGenerationError("会议模型没有返回发言")
    for item in raw_turns[:MAX_TURNS]:
        if not isinstance(item, dict):
            continue
        participant = _clip(item.get("participant"), 80)
        content = _clip(item.get("content"), MAX_MESSAGE_CHARS)
        if participant not in participant_slugs or not content:
            continue
        try:
            round_no = max(1, min(int(item.get("round") or 1), max_rounds))
        except (TypeError, ValueError):
            round_no = 1
        citations: list[dict[str, Any]] = []
        for citation in item.get("citations") or []:
            if not isinstance(citation, dict):
                continue
            try:
                entry_id = int(citation.get("entry_id"))
            except (TypeError, ValueError):
                continue
            if entry_id > 0:
                citations.append({"entry_id": entry_id})
        turns.append(
            {
                "participant": participant,
                "round": round_no,
                "content": content,
                "citations": citations[:10],
            }
        )
    if not turns:
        raise MeetingGenerationError("会议模型没有返回有效发言")

    decisions: list[dict[str, Any]] = []
    raw_decisions = payload.get("decisions") or []
    if not isinstance(raw_decisions, list):
        raise MeetingGenerationError("会议模型返回的决议格式无效")
    for item in raw_decisions[:MAX_DECISIONS]:
        if not isinstance(item, dict):
            continue
        title = _clip(item.get("title"), MAX_TITLE_CHARS)
        detail = _clip(item.get("detail"), MAX_DETAIL_CHARS)
        if not title or not detail:
            continue
        proposer = _clip(item.get("proposer"), 80)
        if proposer not in participant_slugs:
            proposer = sorted(participant_slugs)[0]
        decisions.append(
            {
                "title": title,
                "detail": detail,
                "impact": _clip(item.get("impact"), 2000),
                "proposer": proposer,
            }
        )

    return {
        "summary": summary,
        "consensus": _string_list(payload.get("consensus")),
        "divergences": _normalize_divergences(payload.get("divergences"), participant_slugs),
        "turns": turns,
        "decisions": decisions,
    }


async def generate_meeting_draft(
    *,
    kind: str,
    topic: str,
    rounds: int,
    participants: list[dict[str, Any]],
    model_spec: str | None = None,
) -> dict[str, Any]:
    """调用真实模型生成会议 JSON；结构错误显式抛出，不伪造空纪要。"""
    if kind not in MEETING_KINDS:
        raise MeetingGenerationError("会议类型无效")
    participant_slugs = {item["slug"] for item in participants}
    user_prompt = json.dumps(
        {
            "meeting_kind": kind,
            "topic": topic,
            "rounds": rounds,
            "participants": participants,
        },
        ensure_ascii=False,
        default=str,
    )
    resolved_model = model_spec or (await system_options.get()).get("default_model")
    if not resolved_model:
        raise MeetingGenerationError("系统没有配置默认会议模型")
    try:
        model = select_model(model_spec=resolved_model)
        response = await model.call(
            [
                {"role": "system", "content": MEETING_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]
        )
    except Exception as exc:  # noqa: BLE001 - 模型供应商错误统一收敛为会议生成失败
        raise MeetingGenerationError(f"会议模型调用失败：{exc}") from exc
    try:
        parsed = json_repair.loads(str(getattr(response, "content", "") or ""))
    except Exception as exc:  # noqa: BLE001 - 模型输出不可解析时显式失败
        raise MeetingGenerationError("会议模型输出无法解析为 JSON") from exc
    return _normalize_generated_payload(parsed, participant_slugs, rounds=rounds)


async def _validated_citations(
    db: AsyncSession,
    *,
    meeting: Meeting,
    participant: str,
    citations: list[dict[str, Any]],
    visible_entry_ids: set[int],
) -> list[dict[str, Any]]:
    """只保留该分身本轮可见且已确认的经验引用，避免模型编造条目。"""
    if meeting.kind != MEETING_KIND_TWIN or not citations:
        return []
    entry_ids = [
        int(item["entry_id"])
        for item in citations
        if item.get("entry_id") and int(item["entry_id"]) in visible_entry_ids
    ]
    if not entry_ids:
        return []
    entries = await FounderTwinRepository(db).get_entries_by_ids(entry_ids)
    allowed = {
        entry.id: entry
        for entry in entries
        if entry.twin_slug == participant and entry.status == "confirmed"
    }
    return [
        {"entry_id": entry.id, "title": entry.title}
        for entry in allowed.values()
    ]


async def run_meeting(
    db: AsyncSession,
    *,
    user: User,
    meeting_id: int,
    model_spec: str | None = None,
) -> Meeting:
    """生成会议纪要；草稿才可执行，失败时保留草稿并记录可见错误。"""
    repo = MeetingRepository(db)
    meeting = await repo.get_meeting(meeting_id, for_update=True)
    if meeting is None:
        raise HTTPException(status_code=404, detail="会议不存在")
    agents = await participant_agents_for_meeting(db, meeting)
    if not can_manage_meeting(user, meeting, agents):
        raise HTTPException(status_code=403, detail="无权生成该会议纪要")
    if meeting.status != MEETING_STATUS_DRAFT:
        raise MeetingConflict("只有草稿会议可以生成纪要")

    contexts = await _participant_context(db, meeting, agents)
    try:
        payload = await generate_meeting_draft(
            kind=meeting.kind,
            topic=meeting.topic,
            rounds=meeting.rounds,
            participants=contexts,
            model_spec=model_spec,
        )
    except MeetingGenerationError as exc:
        meeting.generation_error = _clip(str(exc), 2000)
        await db.flush()
        raise

    participant_names = {agent.slug: agent.name for agent in agents}
    visible_entry_ids = {
        context["slug"]: {int(entry["id"]) for entry in context.get("entries", [])}
        for context in contexts
    }
    for turn in payload["turns"]:
        speaker = turn["participant"]
        citations = await _validated_citations(
            db,
            meeting=meeting,
            participant=speaker,
            citations=turn["citations"],
            visible_entry_ids=visible_entry_ids.get(speaker, set()),
        )
        await repo.add_message(
            meeting_id=meeting.id,
            round=turn["round"],
            speaker_type=meeting.kind,
            speaker_slug=speaker,
            speaker_name=participant_names.get(speaker),
            content=turn["content"],
            citations=citations,
        )

    participant_set = set(participant_names)
    signer_uids = _dedupe(meeting.signer_uids or []) or await _derived_signer_uids(
        db, agents, meeting.created_by
    )
    for index, decision_payload in enumerate(payload["decisions"], start=1):
        proposer = decision_payload["proposer"]
        if proposer not in participant_set:
            proposer = next(iter(participant_names))
        decision = await repo.add_decision(
            meeting_id=meeting.id,
            code=f"D{index}",
            title=decision_payload["title"],
            detail=decision_payload["detail"],
            impact=decision_payload["impact"],
            proposer_slug=proposer,
            status=DECISION_STATUS_PENDING,
        )
        for uid in signer_uids:
            await repo.add_signer(decision_id=decision.id, uid=uid, status=SIGNER_STATUS_PENDING)

    meeting.summary = payload["summary"]
    meeting.consensus = payload["consensus"]
    meeting.divergences = payload["divergences"]
    meeting.generation_error = None
    meeting.status = MEETING_STATUS_AWAITING_DECISIONS if payload["decisions"] else MEETING_STATUS_EFFECTIVE
    meeting.updated_at = utc_now_naive()
    await db.flush()
    return meeting


async def add_human_message(
    db: AsyncSession,
    *,
    user: User,
    meeting_id: int,
    content: str,
    round_no: int | None = None,
) -> Meeting:
    """允许可见者以人的身份加入会议并写入一条发言。"""
    meeting, agents = await get_visible_meeting(db, user=user, meeting_id=meeting_id)
    joined_uids = set(meeting.human_uids or []) | set(meeting.signer_uids or [])
    if not can_manage_meeting(user, meeting, agents) and str(user.uid) not in joined_uids:
        raise HTTPException(status_code=403, detail="无权加入该会议")
    text = _clip(content, MAX_MESSAGE_CHARS)
    if not text:
        raise ValueError("会议发言不能为空")
    resolved_round = round_no if round_no is not None else meeting.rounds
    if not 1 <= int(resolved_round) <= int(meeting.rounds):
        raise ValueError(f"会议轮次必须在 1 到 {meeting.rounds} 之间")

    repo = MeetingRepository(db)
    locked = await repo.get_meeting(meeting.id, for_update=True)
    if locked is None:
        raise HTTPException(status_code=404, detail="会议不存在")
    meeting = locked
    human_uids = _dedupe(meeting.human_uids or [])
    if str(user.uid) not in human_uids:
        human_uids.append(str(user.uid))
        meeting.human_uids = human_uids
    await repo.add_message(
        meeting_id=meeting.id,
        round=int(resolved_round),
        speaker_type="human",
        speaker_uid=str(user.uid),
        speaker_name=user.username,
        content=text,
        citations=[],
    )
    meeting.updated_at = utc_now_naive()
    await db.flush()
    return meeting


async def _finalize_experience(
    db: AsyncSession,
    *,
    meeting: Meeting,
    twin_slug: str | None,
    decision_id: int,
    title: str,
    detail: str,
    impact: str,
    actor_uid: str,
) -> int | None:
    """决议生效后回写一条草稿经验；不自动确认，仍由分身负责人审阅。"""
    if not twin_slug:
        return None
    agent = await AgentRepository(db).get_by_slug(twin_slug)
    if agent is None or getattr(agent, "kind", None) != FOUNDER_TWIN_KIND:
        return None
    entry = await create_experience_entry(
        db,
        twin_slug=agent.slug,
        data={
            "code": f"meeting-{decision_id}",
            "title": title,
            "situation": meeting.topic,
            "judgment": detail,
            "boundary": impact,
            "source_type": "meeting",
            "source_ref": f"meeting:{meeting.id}",
        },
        actor_uid=actor_uid,
    )
    return int(entry.id)


async def _refresh_meeting_status(db: AsyncSession, meeting: Meeting) -> None:
    """根据决议状态推进会议总状态。"""
    decisions = await MeetingRepository(db).list_decisions(meeting.id)
    if any(decision.status == DECISION_STATUS_PENDING for decision in decisions):
        meeting.status = MEETING_STATUS_AWAITING_DECISIONS
    elif any(decision.status == DECISION_STATUS_REJECTED for decision in decisions):
        meeting.status = MEETING_STATUS_REJECTED
    else:
        meeting.status = MEETING_STATUS_EFFECTIVE
    meeting.updated_at = utc_now_naive()


async def sign_decision(
    db: AsyncSession,
    *,
    user: User,
    meeting_id: int,
    decision_id: int,
    action: str,
    comment: str | None = None,
    text: str | None = None,
) -> Meeting:
    """指定负责人签字；全部同意生效，任一拒绝失败。"""
    if action not in {"approve", "modify", "reject"}:
        raise ValueError("签字动作只支持 approve、modify 或 reject")
    if action == "modify" and not str(text or "").strip():
        raise ValueError("修改后签字必须填写修改文本")
    meeting, agents = await get_visible_meeting(db, user=user, meeting_id=meeting_id)
    repo = MeetingRepository(db)
    locked = await repo.get_meeting(meeting.id, for_update=True)
    if locked is None:
        raise HTTPException(status_code=404, detail="会议不存在")
    meeting = locked
    decision = await repo.get_decision(decision_id, for_update=True)
    if decision is None or decision.meeting_id != meeting.id:
        raise HTTPException(status_code=404, detail="会议决议不存在")
    if decision.status != DECISION_STATUS_PENDING:
        raise MeetingConflict("该决议已经结束签字")
    signer = await repo.get_signer(decision.id, str(user.uid))
    if signer is None:
        raise HTTPException(status_code=403, detail="不是该决议的指定签字人")
    if signer.status != SIGNER_STATUS_PENDING:
        raise MeetingConflict("你已经签过该决议")

    now = utc_now_naive()
    signer.status = SIGNER_STATUS_REJECTED if action == "reject" else SIGNER_STATUS_APPROVED
    signer.action = action
    signer.comment = _clip(comment, 1000) or None
    signer.signed_text = (
        _clip(text, MAX_DETAIL_CHARS) or None if action == "modify" else None
    )
    signer.signed_at = now

    signers = await repo.list_signers([decision.id])
    if any(item.status == SIGNER_STATUS_REJECTED for item in signers):
        decision.status = DECISION_STATUS_REJECTED
        decision.effective_at = now
        decision.effective_text = None
    elif all(item.status == SIGNER_STATUS_APPROVED for item in signers):
        decision.status = DECISION_STATUS_EFFECTIVE
        modified = sorted(
            (item for item in signers if item.signed_text),
            key=lambda item: (item.signed_at or now, item.id),
        )
        decision.effective_text = modified[-1].signed_text if modified else decision.detail
        decision.effective_at = now
        if decision.experience_entry_id is None:
            entry_id = await _finalize_experience(
                db,
                meeting=meeting,
                twin_slug=decision.proposer_slug,
                decision_id=decision.id,
                title=decision.title,
                detail=decision.effective_text,
                impact=decision.impact,
                actor_uid=str(user.uid),
            )
            if entry_id is not None:
                decision.experience_entry_id = entry_id

    await _refresh_meeting_status(db, meeting)
    await db.flush()
    return meeting


async def build_serialized_decision_context(
    db: AsyncSession, meeting: Meeting
) -> tuple[list, dict[int, list], dict[str, str]]:
    """读取决议、签字人和用户姓名，供 router 组装 DTO。"""
    repo = MeetingRepository(db)
    decisions = await repo.list_decisions(meeting.id)
    signers = await repo.list_signers([decision.id for decision in decisions])
    signers_by_decision: dict[int, list] = {}
    for signer in signers:
        signers_by_decision.setdefault(signer.decision_id, []).append(signer)
    uids = _dedupe(
        [signer.uid for signer in signers]
        + list(meeting.human_uids or [])
        + list(meeting.signer_uids or [])
    )
    names = await FounderTwinRepository(db).get_user_names(uids)
    return decisions, signers_by_decision, names
