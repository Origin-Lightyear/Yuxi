"""老板分身的经验确认、向量检索与引用校验。"""

from __future__ import annotations

import math
import uuid

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.agents.digital_employee import FOUNDER_TWIN_KIND
from yuxi.config import config as app_config
from yuxi.models.embed import select_embedding_model
from yuxi.repositories.agent_repository import AgentRepository, user_can_manage_agent
from yuxi.repositories.founder_twin_repository import FounderTwinRepository
from yuxi.storage.postgres.models_business import (
    Agent,
    ExperienceConsultation,
    ExperienceEntry,
    HandoffRequest,
    User,
)
from yuxi.utils.datetime_utils import utc_now_naive

EXPERIENCE_STATUS_DRAFT = "draft"
EXPERIENCE_STATUS_CONFIRMED = "confirmed"
EXPERIENCE_STATUS_INVALID = "invalid"
EXPERIENCE_STATUSES = (EXPERIENCE_STATUS_DRAFT, EXPERIENCE_STATUS_CONFIRMED, EXPERIENCE_STATUS_INVALID)


class ExperienceCodeConflict(ValueError):
    """同一分身下的经验编码冲突。"""


class ExperienceStateConflict(ValueError):
    """经验状态不允许当前动作。"""


class ExperienceValidationError(ValueError):
    """经验内容不满足确认条件。"""

MAX_EXPERIENCES_PER_TWIN = 2000
DEFAULT_RETRIEVAL_LIMIT = 5


def experience_embedding_text(entry: ExperienceEntry) -> str:
    """拼接参与语义检索的字段，空白字段不产生空段。"""
    parts = (
        entry.title,
        entry.situation,
        entry.judgment,
        entry.boundary,
        entry.source_excerpt,
    )
    return "\n\n".join(str(part).strip() for part in parts if part and str(part).strip())


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """计算余弦相似度；零向量返回 0，维度不一致显式失败。"""
    if len(left) != len(right):
        raise ValueError("embedding 维度不一致")
    dot = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


async def embed_text(text: str, *, model_spec: str | None = None) -> tuple[list[float], str]:
    """用当前 embedding 模型生成单条向量，返回向量和实际模型 spec。"""
    normalized = str(text or "").strip()
    if not normalized:
        raise ValueError("待检索文本为空")
    model = select_embedding_model(model_spec or app_config.embed_model)
    vectors = await model.abatch_encode([normalized])
    if not vectors or not vectors[0]:
        raise ValueError("Embedding 模型未返回向量")
    return [float(value) for value in vectors[0]], model.spec


async def confirm_experience(db: AsyncSession, entry: ExperienceEntry, *, actor_uid: str) -> ExperienceEntry:
    """生成 embedding 后把经验置为 confirmed；失败不改变原状态。"""
    if entry.status != EXPERIENCE_STATUS_DRAFT:
        raise ExperienceStateConflict("只有草稿经验可以确认")
    text = experience_embedding_text(entry)
    if not text:
        raise ExperienceValidationError("经验内容为空，无法确认")
    vector, model_spec = await embed_text(text)
    now = utc_now_naive()
    entry.embedding = vector
    entry.embedding_model = model_spec
    entry.status = EXPERIENCE_STATUS_CONFIRMED
    entry.confirmed_by = actor_uid
    entry.confirmed_at = now
    entry.updated_by = actor_uid
    entry.updated_at = now
    await db.flush()
    return entry


async def retrieve_experiences(
    db: AsyncSession,
    *,
    twin_slug: str,
    query: str,
    limit: int = DEFAULT_RETRIEVAL_LIMIT,
) -> list[tuple[ExperienceEntry, float]]:
    """按余弦相似度返回同分身已确认经验 top-k。"""
    normalized = str(query or "").strip()
    if not normalized:
        raise ValueError("检索问题为空")
    query_vector, model_spec = await embed_text(normalized)
    entries = await FounderTwinRepository(db).list_retrievable(
        twin_slug,
        limit=MAX_EXPERIENCES_PER_TWIN,
        embedding_model=model_spec,
    )
    scored: list[tuple[ExperienceEntry, float]] = []
    for entry in entries:
        vector = entry.embedding if isinstance(entry.embedding, list) else []
        if not vector:
            continue
        try:
            score = cosine_similarity(query_vector, vector)
        except ValueError:
            continue
        scored.append((entry, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[: max(1, min(int(limit), 10))]


async def validate_citations(
    db: AsyncSession,
    *,
    twin_slug: str,
    cited_entry_ids: list[int],
) -> list[ExperienceEntry]:
    """校验引用去重后全部属于该分身、已确认且有 embedding。"""
    unique_ids: list[int] = []
    seen: set[int] = set()
    for value in cited_entry_ids or []:
        try:
            entry_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("引用条目 ID 无效") from exc
        if entry_id <= 0:
            raise ValueError("引用条目 ID 无效")
        if entry_id not in seen:
            seen.add(entry_id)
            unique_ids.append(entry_id)
    if not unique_ids:
        return []

    entries = await FounderTwinRepository(db).get_entries_by_ids(unique_ids)
    by_id = {
        entry.id: entry
        for entry in entries
        if entry.twin_slug == twin_slug
        and entry.status == EXPERIENCE_STATUS_CONFIRMED
        and bool(entry.embedding)
    }
    if len(by_id) != len(unique_ids):
        raise ValueError("引用条目不存在、未确认或不属于该分身")
    return [by_id[entry_id] for entry_id in unique_ids]


async def validate_external_citations(
    db: AsyncSession,
    *,
    external_citations: list[dict] | None,
) -> list[tuple[str, ExperienceEntry]]:
    """校验跨分身引用去重后属于目标分身、已确认且有 embedding。"""
    normalized: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for item in external_citations or []:
        if not isinstance(item, dict):
            raise ValueError("跨分身引用格式无效")
        twin_slug = str(item.get("twin_slug") or "").strip()
        try:
            entry_id = int(item.get("entry_id"))
        except (TypeError, ValueError) as exc:
            raise ValueError("跨分身引用条目 ID 无效") from exc
        if not twin_slug or entry_id <= 0:
            raise ValueError("跨分身引用格式无效")
        key = (twin_slug, entry_id)
        if key not in seen:
            seen.add(key)
            normalized.append(key)
    if not normalized:
        return []

    entries = await FounderTwinRepository(db).get_entries_by_ids([entry_id for _, entry_id in normalized])
    by_id = {
        entry.id: entry
        for entry in entries
        if entry.status == EXPERIENCE_STATUS_CONFIRMED and bool(entry.embedding)
    }
    result: list[tuple[str, ExperienceEntry]] = []
    for twin_slug, entry_id in normalized:
        entry = by_id.get(entry_id)
        if entry is None or entry.twin_slug != twin_slug:
            raise ValueError("跨分身引用条目不存在、未确认或不属于目标分身")
        result.append((twin_slug, entry))
    return result


async def create_experience_entry(
    db: AsyncSession,
    *,
    twin_slug: str,
    data: dict,
    actor_uid: str,
) -> ExperienceEntry:
    """创建草稿经验；超过单分身 2,000 条上限时拒绝。"""
    repo = FounderTwinRepository(db)
    if await repo.count_entries(twin_slug) >= MAX_EXPERIENCES_PER_TWIN:
        raise ValueError(f"单个分身最多保存 {MAX_EXPERIENCES_PER_TWIN} 条经验")
    now = utc_now_naive()
    entry = ExperienceEntry(
        twin_slug=twin_slug,
        code=str(data.get("code") or f"exp-{uuid.uuid4().hex[:12]}").strip(),
        title=str(data.get("title") or "").strip(),
        situation=str(data.get("situation") or "").strip(),
        judgment=str(data.get("judgment") or "").strip(),
        boundary=str(data.get("boundary") or "").strip(),
        source_type=str(data.get("source_type") or "manual").strip(),
        source_ref=str(data.get("source_ref") or "").strip() or None,
        source_excerpt=str(data.get("source_excerpt") or "").strip() or None,
        status=EXPERIENCE_STATUS_DRAFT,
        created_by=actor_uid,
        updated_by=actor_uid,
        created_at=now,
        updated_at=now,
    )
    if not entry.title or not entry.judgment:
        raise ValueError("经验标题和判断不能为空")
    db.add(entry)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise ExperienceCodeConflict("经验编码已存在") from exc
    return entry


async def update_experience_entry(
    db: AsyncSession,
    entry: ExperienceEntry,
    *,
    data: dict,
    actor_uid: str,
) -> ExperienceEntry:
    """编辑草稿/已确认经验；内容变化后退回草稿并清空旧 embedding。"""
    if entry.status == EXPERIENCE_STATUS_INVALID:
        raise ExperienceStateConflict("已失效经验不能编辑，请新建条目")
    for field_name in (
        "code",
        "title",
        "situation",
        "judgment",
        "boundary",
        "source_type",
        "source_ref",
        "source_excerpt",
    ):
        if field_name not in data:
            continue
        value = data[field_name]
        setattr(entry, field_name, str(value).strip() if value is not None else None)
    if not entry.title or not entry.judgment:
        raise ValueError("经验标题和判断不能为空")
    entry.status = EXPERIENCE_STATUS_DRAFT
    entry.embedding = None
    entry.embedding_model = None
    entry.confirmed_by = None
    entry.confirmed_at = None
    entry.updated_by = actor_uid
    entry.updated_at = utc_now_naive()
    try:
        await db.flush()
    except IntegrityError as exc:
        raise ExperienceCodeConflict("经验编码已存在") from exc
    return entry


async def invalidate_experience(db: AsyncSession, entry: ExperienceEntry, *, actor_uid: str) -> ExperienceEntry:
    """标记经验失效并移除检索向量。"""
    entry.status = EXPERIENCE_STATUS_INVALID
    entry.embedding = None
    entry.embedding_model = None
    entry.updated_by = actor_uid
    entry.updated_at = utc_now_naive()
    await db.flush()
    return entry


async def get_twin_stats(db: AsyncSession, twin_slug: str) -> dict[str, int]:
    """返回分身页需要的条目和待处理转交计数。"""
    repo = FounderTwinRepository(db)
    entry_counts = await repo.entry_status_counts(twin_slug)
    handoff_counts = await repo.handoff_status_counts(twin_slug)
    return {
        "confirmed_entries": entry_counts.get(EXPERIENCE_STATUS_CONFIRMED, 0),
        "draft_entries": entry_counts.get(EXPERIENCE_STATUS_DRAFT, 0),
        "open_handoffs": handoff_counts.get("open", 0),
    }


async def _lock_run(db: AsyncSession, run_id: str) -> None:
    """PostgreSQL 下按 Run 串行化工具写入，SQLite 测试不需要。"""
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
            {"lock_key": f"founder_twin_run:{run_id}"},
        )


async def record_consultation_answer(
    db: AsyncSession,
    *,
    twin_slug: str,
    uid: str,
    thread_id: str,
    run_id: str,
    question: str,
    answer: str,
    cited_entries: list[ExperienceEntry],
    confidence: float | None,
    external_citations: list[dict] | None = None,
) -> ExperienceConsultation:
    """把已校验答案写入本次 Run 的唯一咨询记录，并只对新增引用累加计数。"""
    await _lock_run(db, run_id)
    validated_external = await validate_external_citations(db, external_citations=external_citations)
    repo = FounderTwinRepository(db)
    consultation = await repo.get_consultation_by_run(run_id)
    if consultation is not None and consultation.handoff_id is not None:
        raise ValueError("已转交的问题不能直接提交答案")
    previous_ids: set[int] = set()
    previous_external: set[tuple[str, int]] = set()
    if consultation is None:
        consultation = ExperienceConsultation(
            twin_slug=twin_slug,
            uid=uid,
            thread_id=thread_id,
            run_id=run_id,
            question=question,
            answer=answer,
            cited_entry_ids=[],
            external_citations=[],
        )
        db.add(consultation)
    else:
        for value in consultation.cited_entry_ids or []:
            try:
                previous_ids.add(int(value))
            except (TypeError, ValueError):
                continue
        for item in consultation.external_citations or []:
            if not isinstance(item, dict):
                continue
            try:
                entry_id = int(item.get("entry_id"))
            except (TypeError, ValueError):
                continue
            external_twin_slug = str(item.get("twin_slug") or "").strip()
            if external_twin_slug and entry_id > 0:
                previous_external.add((external_twin_slug, entry_id))
        consultation.question = question
        consultation.answer = answer
    cited_ids = [int(entry.id) for entry in cited_entries]
    external_refs = [
        {"twin_slug": external_twin, "entry_id": int(entry.id)}
        for external_twin, entry in validated_external
    ]
    consultation.cited_entry_ids = cited_ids
    consultation.external_citations = external_refs
    consultation.confidence = confidence
    consultation.handoff_id = None
    for entry in cited_entries:
        if int(entry.id) not in previous_ids:
            entry.citation_count = int(entry.citation_count or 0) + 1
    for external_twin, entry in validated_external:
        if (external_twin, int(entry.id)) not in previous_external:
            entry.citation_count = int(entry.citation_count or 0) + 1
    await db.flush()
    return consultation


async def record_handoff(
    db: AsyncSession,
    *,
    twin_slug: str,
    uid: str,
    thread_id: str,
    run_id: str,
    question: str,
    reason: str,
    assigned_to_uid: str | None,
) -> HandoffRequest:
    """创建转交单，并让本次 Run 的咨询记录指向它。"""
    await _lock_run(db, run_id)
    repo = FounderTwinRepository(db)
    existing = await repo.get_consultation_by_run(run_id)
    if existing is not None and existing.cited_entry_ids:
        raise ValueError("已提交答案的问题不能转交")
    if existing is not None and existing.handoff_id is not None:
        handoff = await repo.get_handoff(int(existing.handoff_id))
        if handoff is not None:
            return handoff
    handoff = HandoffRequest(
        twin_slug=twin_slug,
        uid=uid,
        question=question,
        assigned_to_uid=assigned_to_uid,
        status="open",
    )
    db.add(handoff)
    await db.flush()
    consultation = existing
    if consultation is None:
        consultation = ExperienceConsultation(
            twin_slug=twin_slug,
            uid=uid,
            thread_id=thread_id,
            run_id=run_id,
            question=question,
            answer=reason,
            cited_entry_ids=[],
        )
        db.add(consultation)
    else:
        consultation.question = question
        consultation.answer = reason
        consultation.cited_entry_ids = []
        consultation.external_citations = []
        consultation.confidence = None
    consultation.handoff_id = int(handoff.id)
    await db.flush()
    return handoff


async def get_visible_twin(db: AsyncSession, *, slug: str, user: User) -> Agent:
    """读取用户可见的老板分身，非分身或不可见统一 404。"""
    agent = await AgentRepository(db).get_visible_by_slug(slug=slug, user=user, kind="any")
    if agent is None or getattr(agent, "kind", "agent") != FOUNDER_TWIN_KIND:
        raise HTTPException(status_code=404, detail="老板分身不存在")
    return agent


async def get_manageable_twin(db: AsyncSession, *, slug: str, user: User) -> Agent:
    """读取用户可管理的老板分身；可见但无管理权限返回 403。"""
    agent = await AgentRepository(db).get_by_slug(slug)
    if agent is None or getattr(agent, "kind", "agent") != FOUNDER_TWIN_KIND:
        raise HTTPException(status_code=404, detail="老板分身不存在")
    if not user_can_manage_agent(user, agent):
        raise HTTPException(status_code=403, detail="无权管理该老板分身")
    return agent
