"""老板分身系统提示、咨询工具与答案守卫。"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain.agents.middleware.types import ContextT, ModelRequest, ModelResponse, ResponseT
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from yuxi.agents.digital_employee import FOUNDER_TWIN_KIND
from yuxi.agents.founder_twin import HANDOFF_ANSWER, build_founder_twin_tools

FOUNDER_TWIN_SYSTEM_PROMPT = """## 老板分身执行规则

你是老板的判断分身，不是通用聊天助手。
- 回答任何业务问题前，先调用 `retrieve_experience` 检索已确认经验。
- 有可引用经验时，调用 `submit_consultation_answer` 提交答案、`cited_entry_ids` 和置信度；最终回复必须原样复述工具返回的 `answer`。
- 没有可引用经验时，不要猜测，调用 `create_handoff_request` 把问题转交老板本人，并原样复述工具返回的 `answer`。
- 绝不能用常识、推测或未确认草稿代替经验依据。"""

GUARD_MESSAGE_NAME = "founder_twin_guard"
GUARD_CORRECTION = (
    "上一轮回答没有通过老板分身校验。请重新执行：先调用 retrieve_experience 检索经验；"
    "有依据时调用 submit_consultation_answer 提交答案和 cited_entry_ids，并原样复述提交的 answer；"
    "没有依据时调用 create_handoff_request 转交老板，并原样复述工具返回的 answer。不要直接给出结论。"
)


class FounderTwinMiddleware(AgentMiddleware[Any, ContextT, ResponseT]):
    """为老板分身注入执行规则并注册三项咨询工具。"""

    def __init__(self) -> None:
        super().__init__()
        self.system_prompt = FOUNDER_TWIN_SYSTEM_PROMPT
        self.tools = build_founder_twin_tools()

    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        request = request.override(system_message=append_to_system_message(request.system_message, self.system_prompt))
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Awaitable[ModelResponse[ResponseT]]],
    ) -> ModelResponse[ResponseT]:
        request = request.override(system_message=append_to_system_message(request.system_message, self.system_prompt))
        return await handler(request)


class FounderTwinAnswerGuardMiddleware(AgentMiddleware[Any, ContextT, ResponseT]):
    """阻止老板分身在没有成功提交答案或转交时直接结束。"""

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state, runtime):  # noqa: ARG002
        context = getattr(runtime, "context", None)
        if getattr(context, "agent_kind", None) != FOUNDER_TWIN_KIND:
            return None
        messages = state.get("messages") if isinstance(state, dict) else None
        if not messages:
            return None
        last_message = messages[-1]
        if _has_tool_calls(last_message):
            return None
        if not _is_ai_message(last_message):
            return None

        boundary = _last_user_index(messages)
        if boundary is None:
            return None
        recent = messages[boundary + 1 :]
        last_guard = max((index for index, message in enumerate(recent) if _is_guard_message(message)), default=-1)
        success = _successful_submission(recent)
        if success is not None:
            kind, payload = success
            expected = str(payload.get("answer") or "") if kind == "submit" else str(payload.get("answer") or HANDOFF_ANSWER)
            if _normalize_text(_message_text(last_message)) == _normalize_text(expected):
                return None

        if last_guard >= 0:
            raise RuntimeError("老板分身未提交有依据的答案")
        return {
            "jump_to": "model",
            "messages": [HumanMessage(content=GUARD_CORRECTION, name=GUARD_MESSAGE_NAME)],
        }


def _is_ai_message(message: Any) -> bool:
    if isinstance(message, AIMessage):
        return True
    return isinstance(message, dict) and message.get("type") == "ai"


def _has_tool_calls(message: Any) -> bool:
    if isinstance(message, dict):
        return bool(message.get("tool_calls"))
    return bool(getattr(message, "tool_calls", None))


def _is_guard_message(message: Any) -> bool:
    if isinstance(message, HumanMessage):
        return getattr(message, "name", None) == GUARD_MESSAGE_NAME
    return isinstance(message, dict) and message.get("type") == "human" and message.get("name") == GUARD_MESSAGE_NAME


def _last_user_index(messages: list[Any]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        is_human = isinstance(message, HumanMessage) or (
            isinstance(message, dict) and message.get("type") == "human"
        )
        if is_human and not _is_guard_message(message):
            return index
    return None


def _successful_submission(messages: list[Any]) -> tuple[str, dict] | None:
    for message in reversed(messages):
        if not _is_tool_message(message):
            continue
        name = getattr(message, "name", None)
        if name is None and isinstance(message, dict):
            name = message.get("name")
        if name not in {"submit_consultation_answer", "create_handoff_request"}:
            continue
        payload = _tool_payload(message)
        if not isinstance(payload, dict) or not payload.get("ok"):
            continue
        if name == "create_handoff_request" and payload.get("handoff_id") is not None:
            return "handoff", payload
        if name == "submit_consultation_answer" and isinstance(payload.get("answer"), str):
            return "submit", payload
    return None


def _is_tool_message(message: Any) -> bool:
    if isinstance(message, ToolMessage):
        return True
    return isinstance(message, dict) and message.get("type") == "tool"


def _tool_payload(message: Any) -> dict | None:
    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        return None
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _message_text(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()
