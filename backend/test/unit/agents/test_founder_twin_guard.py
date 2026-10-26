from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from yuxi.agents.middlewares.founder_twin import FounderTwinAnswerGuardMiddleware

pytestmark = pytest.mark.asyncio


def _runtime(*, kind: str = "founder_twin"):
    return SimpleNamespace(context=SimpleNamespace(agent_kind=kind))


def _tool(name: str, payload: str) -> ToolMessage:
    return ToolMessage(content=payload, name=name, tool_call_id="call-1")


async def test_guard_corrects_then_fails_without_submitted_answer() -> None:
    guard = FounderTwinAnswerGuardMiddleware()
    runtime = _runtime()

    first = await guard.aafter_model(
        {"messages": [HumanMessage("怎么定价"), AIMessage("直接结论")]},
        runtime,
    )

    assert first is not None
    assert first["jump_to"] == "model"
    correction = first["messages"][0]
    assert correction.name == "founder_twin_guard"
    assert "submit_consultation_answer" in correction.content

    with pytest.raises(RuntimeError, match="老板分身未提交有依据的答案"):
        await guard.aafter_model(
            {
                "messages": [
                    HumanMessage("怎么定价"),
                    AIMessage("直接结论"),
                    correction,
                    AIMessage("还是直接结论"),
                ]
            },
            runtime,
        )


async def test_guard_accepts_matching_submitted_answer() -> None:
    guard = FounderTwinAnswerGuardMiddleware()

    result = await guard.aafter_model(
        {
            "messages": [
                HumanMessage("怎么定价"),
                AIMessage(content="", tool_calls=[{"id": "call-1", "name": "submit_consultation_answer", "args": {}}]),
                _tool("submit_consultation_answer", '{"ok": true, "answer": "先看成本再报价"}'),
                AIMessage("先看成本再报价"),
            ]
        },
        _runtime(),
    )

    assert result is None


async def test_guard_corrects_mismatched_submitted_answer() -> None:
    guard = FounderTwinAnswerGuardMiddleware()

    result = await guard.aafter_model(
        {
            "messages": [
                HumanMessage("怎么定价"),
                AIMessage(content="", tool_calls=[{"id": "call-1", "name": "submit_consultation_answer", "args": {}}]),
                _tool("submit_consultation_answer", '{"ok": true, "answer": "先看成本再报价"}'),
                AIMessage("随便报个价"),
            ]
        },
        _runtime(),
    )

    assert result is not None
    assert result["jump_to"] == "model"
    assert result["messages"][0].name == "founder_twin_guard"


async def test_guard_allows_handoff_and_ignores_non_founder() -> None:
    guard = FounderTwinAnswerGuardMiddleware()
    handoff_state = {
        "messages": [
            HumanMessage("组织架构怎么调整"),
            AIMessage(content="", tool_calls=[{"id": "call-1", "name": "create_handoff_request", "args": {}}]),
            _tool("create_handoff_request", '{"ok": true, "handoff_id": 7, "handoff_key": "handoff:7"}'),
            AIMessage("这个问题需要老板本人判断。"),
        ]
    }

    assert await guard.aafter_model(handoff_state, _runtime()) is None
    assert await guard.aafter_model({"messages": [HumanMessage("问题"), AIMessage("直接结论")]}, _runtime(kind="agent")) is None
