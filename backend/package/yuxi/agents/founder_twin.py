"""老板分身咨询工具：检索经验、提交答案、无依据转交。"""

from __future__ import annotations

import json
from typing import Annotated

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt.tool_node import ToolRuntime

from yuxi.agents.digital_employee import FOUNDER_TWIN_KIND
from yuxi.repositories.agent_repository import AgentRepository
from yuxi.services.founder_twin_service import (
    record_consultation_answer,
    record_handoff,
    retrieve_experiences,
    validate_citations,
)
from yuxi.storage.postgres.manager import pg_manager

HANDOFF_ANSWER = "已将问题转交老板本人，请等待本人回答。"


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _founder_context(runtime: ToolRuntime) -> dict[str, str]:
    context = runtime.context
    if getattr(context, "agent_kind", None) != FOUNDER_TWIN_KIND:
        raise ValueError("当前不是老板分身会话")
    values = {
        "twin_slug": str(getattr(context, "agent_slug", "") or "").strip(),
        "uid": str(getattr(context, "uid", "") or "").strip(),
        "thread_id": str(getattr(context, "thread_id", "") or "").strip(),
        "run_id": str(getattr(context, "run_id", "") or "").strip(),
    }
    if not all(values.values()):
        raise ValueError("老板分身会话缺少必要身份")
    return values


def _current_question(runtime: ToolRuntime) -> str:
    state = runtime.state or {}
    messages = state.get("messages") if isinstance(state, dict) else None
    for message in reversed(messages or []):
        if isinstance(message, HumanMessage) and getattr(message, "name", None) != "founder_twin_guard":
            content = message.content
            return content if isinstance(content, str) else str(content)
    return ""


def _retrieved_entry_ids(runtime: ToolRuntime) -> set[int]:
    """读取最近用户轮次内 retrieve_experience 返回过的条目 ID。"""
    state = runtime.state or {}
    messages = state.get("messages") if isinstance(state, dict) else None
    if not messages:
        return set()
    boundary = -1
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, HumanMessage) and getattr(message, "name", None) != "founder_twin_guard":
            boundary = index
            break
    retrieved: set[int] = set()
    for message in messages[boundary + 1 :]:
        if not isinstance(message, ToolMessage) or getattr(message, "name", None) != "retrieve_experience":
            continue
        content = message.content
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            continue
        for entry in payload.get("entries") or []:
            if isinstance(entry, dict) and entry.get("id") is not None:
                retrieved.add(int(entry["id"]))
    return retrieved


async def _retrieve_experience(query: str, runtime: ToolRuntime) -> str:
    scope = _founder_context(runtime)
    try:
        async with pg_manager.get_async_session_context() as db:
            results = await retrieve_experiences(
                db,
                twin_slug=scope["twin_slug"],
                query=query,
                limit=5,
            )
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc)})
    return _json(
        {
            "ok": True,
            "entries": [
                {
                    "id": entry.id,
                    "title": entry.title,
                    "situation": entry.situation,
                    "judgment": entry.judgment,
                    "boundary": entry.boundary,
                    "score": round(score, 4),
                }
                for entry, score in results
            ],
        }
    )


async def _submit_answer(
    answer: str,
    cited_entry_ids: list[int],
    confidence: float,
    runtime: ToolRuntime,
) -> str:
    scope = _founder_context(runtime)
    if not str(answer or "").strip():
        return _json({"ok": False, "error": "答案不能为空"})
    if not cited_entry_ids:
        return _json({"ok": False, "error": "没有引用依据，不能提交答案；请转交老板"})
    retrieved_ids = _retrieved_entry_ids(runtime)
    cited_ids = {int(value) for value in cited_entry_ids}
    if not retrieved_ids:
        return _json({"ok": False, "error": "必须先调用 retrieve_experience 检索经验"})
    if not cited_ids.issubset(retrieved_ids):
        return _json({"ok": False, "error": "引用条目必须来自本轮检索结果"})
    if not 0.0 <= float(confidence) <= 1.0:
        return _json({"ok": False, "error": "confidence 必须在 0 到 1 之间"})
    async with pg_manager.get_async_session_context() as db:
        try:
            entries = await validate_citations(
                db,
                twin_slug=scope["twin_slug"],
                cited_entry_ids=cited_entry_ids,
            )
            consultation = await record_consultation_answer(
                db,
                twin_slug=scope["twin_slug"],
                uid=scope["uid"],
                thread_id=scope["thread_id"],
                run_id=scope["run_id"],
                question=_current_question(runtime),
                answer=answer.strip(),
                cited_entries=entries,
                confidence=float(confidence),
            )
            await db.commit()
        except ValueError as exc:
            await db.rollback()
            return _json({"ok": False, "error": str(exc)})
    return _json(
        {
            "ok": True,
            "answer": answer.strip(),
            "consultation_id": consultation.id,
            "cited_entry_ids": [int(entry.id) for entry in entries],
        }
    )


async def _create_handoff(question: str, reason: str, runtime: ToolRuntime) -> str:
    scope = _founder_context(runtime)
    if not str(question or "").strip() or not str(reason or "").strip():
        return _json({"ok": False, "error": "转交问题与原因不能为空"})
    try:
        async with pg_manager.get_async_session_context() as db:
            agent = await AgentRepository(db).get_by_slug(scope["twin_slug"])
            assigned_to_uid = str(getattr(agent, "created_by", "") or "").strip() or None
            handoff = await record_handoff(
                db,
                twin_slug=scope["twin_slug"],
                uid=scope["uid"],
                thread_id=scope["thread_id"],
                run_id=scope["run_id"],
                question=question.strip(),
                reason=reason.strip(),
                assigned_to_uid=assigned_to_uid,
            )
            await db.commit()
    except ValueError as exc:
        return _json({"ok": False, "error": str(exc)})
    return _json(
        {
            "ok": True,
            "handoff_id": int(handoff.id),
            "handoff_key": f"handoff:{int(handoff.id)}",
            "answer": HANDOFF_ANSWER,
        }
    )


def build_founder_twin_tools() -> list[StructuredTool]:
    """构建只在老板分身会话注册的咨询工具。"""

    async def retrieve_experience(
        query: Annotated[str, "当前用户问题或需要检索的经验主题"],
        runtime: ToolRuntime,
    ) -> str:
        return await _retrieve_experience(query, runtime)

    async def submit_consultation_answer(
        answer: Annotated[str, "准备原样展示给用户的最终答案"],
        cited_entry_ids: Annotated[list[int], "支撑答案的经验条目 ID，不能为空"],
        confidence: Annotated[float, "对答案的置信度，范围 0 到 1"],
        runtime: ToolRuntime,
    ) -> str:
        return await _submit_answer(answer, cited_entry_ids, confidence, runtime)

    async def create_handoff_request(
        question: Annotated[str, "需要老板本人回答的问题"],
        reason: Annotated[str, "为什么现有经验不足以回答"],
        runtime: ToolRuntime,
    ) -> str:
        return await _create_handoff(question, reason, runtime)

    return [
        StructuredTool.from_function(
            name="retrieve_experience",
            coroutine=retrieve_experience,
            description="检索当前老板分身已确认的经验条目；回答前必须先调用。",
            infer_schema=True,
        ),
        StructuredTool.from_function(
            name="submit_consultation_answer",
            coroutine=submit_consultation_answer,
            description=(
                "提交有经验依据的最终答案；必须先单独调用 retrieve_experience，"
                "cited_entry_ids 只能来自本轮检索结果，成功提交后最终回复必须原样复述 answer。"
            ),
            infer_schema=True,
        ),
        StructuredTool.from_function(
            name="create_handoff_request",
            coroutine=create_handoff_request,
            description="没有可引用经验时，把问题转交老板本人；不要直接猜测回答。",
            infer_schema=True,
        ),
    ]
