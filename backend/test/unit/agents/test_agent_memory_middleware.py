"""独立 Agent Memory middleware 单元测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import SystemMessage
from langchain_openai import ChatOpenAI

from yuxi.agents.buildin.chatbot import graph as chatbot_graph
from yuxi.agents.middlewares import agent_memory, memory as memory_middleware
from yuxi.services import agent_memory_service

pytestmark = pytest.mark.unit


def _context(uid: str = "user-1", agent_slug: str | None = "my-agent") -> SimpleNamespace:
    return SimpleNamespace(uid=uid, agent_slug=agent_slug)


def _patch_entries(monkeypatch: pytest.MonkeyPatch, entries: list[str]) -> list[tuple]:
    """替换注入读取入口并记录调用参数。"""
    calls: list[tuple] = []

    async def fake_load(*, uid: str, agent_slug: str, limit: int = 20) -> list[str]:
        calls.append((uid, agent_slug))
        return entries

    monkeypatch.setattr(agent_memory_service, "load_injection_entries", fake_load)
    return calls


async def test_create_agent_memory_middleware_injects_every_entry(monkeypatch: pytest.MonkeyPatch):
    _patch_entries(monkeypatch, ["偏好中文回复", "常用项目 Yuxi"])

    middleware = await agent_memory.create_agent_memory_middleware(_context())

    assert middleware is not None
    assert "<agent_memory>" in middleware.system_prompt
    assert "- 偏好中文回复" in middleware.system_prompt
    assert "- 常用项目 Yuxi" in middleware.system_prompt


async def test_create_agent_memory_middleware_returns_none_for_empty_entries(monkeypatch: pytest.MonkeyPatch):
    _patch_entries(monkeypatch, [])

    assert await agent_memory.create_agent_memory_middleware(_context()) is None


async def test_create_agent_memory_middleware_degrades_on_query_failure(monkeypatch: pytest.MonkeyPatch):
    async def failing_load(*, uid: str, agent_slug: str, limit: int = 20) -> list[str]:
        raise RuntimeError("db down")

    warnings: list[str] = []
    monkeypatch.setattr(agent_memory, "logger", SimpleNamespace(warning=warnings.append), raising=False)
    monkeypatch.setattr(agent_memory_service, "load_injection_entries", failing_load)

    middleware = await agent_memory.create_agent_memory_middleware(_context())

    assert middleware is None
    assert len(warnings) == 1
    assert "db down" in warnings[0]
    assert "user-1" in warnings[0]
    assert "my-agent" in warnings[0]


async def test_query_failure_does_not_swallow_cancellation(monkeypatch: pytest.MonkeyPatch):
    async def cancelled_load(*, uid: str, agent_slug: str, limit: int = 20) -> list[str]:
        raise asyncio.CancelledError

    monkeypatch.setattr(agent_memory_service, "load_injection_entries", cancelled_load)

    with pytest.raises(asyncio.CancelledError):
        await agent_memory.create_agent_memory_middleware(_context())


@pytest.mark.parametrize(
    "context",
    [SimpleNamespace(uid="", agent_slug="my-agent"), SimpleNamespace(uid="user-1", agent_slug=None)],
    ids=["uid-empty", "agent-slug-missing"],
)
async def test_create_agent_memory_middleware_requires_identity(monkeypatch: pytest.MonkeyPatch, context):
    calls = _patch_entries(monkeypatch, ["条目"])

    assert await agent_memory.create_agent_memory_middleware(context) is None
    assert calls == []


async def test_awrap_model_call_appends_block_after_original_system_message(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_entries(monkeypatch, ["偏好中文回复"])
    middleware = await agent_memory.create_agent_memory_middleware(_context())
    seen: dict = {}

    async def handler(request: ModelRequest):
        seen["system_message"] = request.system_message
        return "ok"

    request = ModelRequest(
        model=ChatOpenAI(model="test-model", api_key="test-key", base_url="https://example.com/v1"),
        messages=[],
        system_message=SystemMessage(content="原始系统提示"),
    )

    assert await middleware.awrap_model_call(request, handler) == "ok"
    injected = seen["system_message"]
    assert injected.text == "原始系统提示\n\n" + middleware.system_prompt
    assert "<agent_memory>" in injected.text
    assert request.system_message.content == "原始系统提示"


async def test_agent_memory_independent_from_memory_md(monkeypatch: pytest.MonkeyPatch):
    """MEMORY.md 缺失时既有工厂返回 None，agent 条目注入仍独立成立。"""

    async def no_memory_md(_uid: str) -> None:
        return None

    monkeypatch.setattr(memory_middleware, "load_memory_prompt", no_memory_md)
    _patch_entries(monkeypatch, ["偏好中文回复"])

    assert await memory_middleware.create_memory_middleware(_context()) is None
    assert await agent_memory.create_agent_memory_middleware(_context()) is not None


async def test_chatbot_graph_mounts_agent_memory_middleware(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(chatbot_graph, "create_agent_filesystem_middleware", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(chatbot_graph, "create_summary_middleware_from_context", lambda _context, **_kwargs: object())

    async def no_optional_middleware(_context):
        return None

    monkeypatch.setattr(chatbot_graph, "create_memory_middleware", no_optional_middleware)
    monkeypatch.setattr(chatbot_graph, "create_subagent_task_middleware", no_optional_middleware)
    _patch_entries(monkeypatch, ["偏好中文回复"])
    context = SimpleNamespace(
        uid="user-1",
        agent_slug="my-agent",
        model="test-provider:test-model",
        tool_approval_mode="default",
        workdir_relative_path="projects/11111111-1111-4111-8111-111111111111",
        tool_token_limit=8,
    )

    middlewares = await chatbot_graph._build_middlewares(context, object())

    assert any(isinstance(item, agent_memory.YuxiAgentMemoryMiddleware) for item in middlewares)
