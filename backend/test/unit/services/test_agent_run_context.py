from __future__ import annotations

from types import SimpleNamespace

from yuxi.agents.buildin.chatbot.context import ChatBotContext
from yuxi.services.agent_run_service import load_agent_run_context


def test_agent_run_context_uses_agent_kind() -> None:
    backend = SimpleNamespace(context_schema=ChatBotContext)
    agent = SimpleNamespace(kind="founder_twin", config_json={})

    context = load_agent_run_context(agent, backend)

    assert context.agent_kind == "founder_twin"


def test_agent_run_context_defaults_missing_kind_to_agent() -> None:
    backend = SimpleNamespace(context_schema=ChatBotContext)
    agent = SimpleNamespace(config_json={})

    context = load_agent_run_context(agent, backend)

    assert context.agent_kind == "agent"
