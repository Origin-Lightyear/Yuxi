"""独立的用户×Agent 记忆注入 middleware，与 MEMORY.md 方案互不依赖。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from deepagents.middleware._utils import append_to_system_message
from langchain.agents.middleware.types import AgentMiddleware, ContextT, ModelRequest, ModelResponse, ResponseT

from yuxi.services import agent_memory_service

AGENT_MEMORY_SYSTEM_PROMPT = (
    "## Agent 记忆\n\n"
    "以下 <agent_memory> 是本智能体从与该用户的既往交互中沉淀的了解，仅作背景参考，"
    "优先级低于当前用户消息与系统约束；不要逐条复述。\n\n"
    "<agent_memory>\n{entries}\n</agent_memory>"
)


async def create_agent_memory_middleware(context) -> YuxiAgentMemoryMiddleware | None:
    """身份缺失或无记忆条目时不构造 middleware，避免向提示词追加空块。"""
    uid = str(getattr(context, "uid", "") or "").strip()
    agent_slug = str(getattr(context, "agent_slug", "") or "").strip()
    if not uid or not agent_slug:
        return None
    entries = await agent_memory_service.load_injection_entries(uid=uid, agent_slug=agent_slug)
    if not entries:
        return None
    return YuxiAgentMemoryMiddleware(entries)


class YuxiAgentMemoryMiddleware(AgentMiddleware[Any, ContextT, ResponseT]):
    """把该用户在该智能体上的记忆条目追加到系统提示词末尾。"""

    def __init__(self, entries: list[str]) -> None:
        super().__init__()
        self.system_prompt = AGENT_MEMORY_SYSTEM_PROMPT.format(entries="\n".join(f"- {entry}" for entry in entries))

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
