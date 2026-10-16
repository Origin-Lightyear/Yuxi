"""agent_memory_service 的开关判定、注入读取与删除语义测试。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from yuxi.services import agent_memory_service as service

pytestmark = pytest.mark.unit


def _patch_session(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    """把 service 自开的会话替换为可观测假会话，避免触碰真实 PostgreSQL。"""

    class FakeDB:
        async def commit(self):
            events.append("commit")

    @asynccontextmanager
    async def fake_session():
        yield FakeDB()

    monkeypatch.setattr(service, "pg_manager", SimpleNamespace(get_async_session_context=fake_session))


def _patch_config(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> list[str]:
    """替换 UserConfig.load 并返回被加载 uid 的记录列表。"""
    loads: list[str] = []

    async def fake_load(_db, uid: str):
        loads.append(uid)
        return SimpleNamespace(schema=SimpleNamespace(enable_memory=enabled))

    monkeypatch.setattr(service.UserConfig, "load", fake_load)
    return loads


def _patch_repository(monkeypatch: pytest.MonkeyPatch, *, rows=None, delete_hits=True) -> list[tuple]:
    """替换 AgentMemoryRepository 并记录调用；rows/delete_hits 为脚本化返回值。"""
    calls: list[tuple] = []

    class FakeRepository:
        def __init__(self, _db):
            pass

        async def list_by_agent(self, *, uid: str, agent_slug: str):
            calls.append(("list", uid, agent_slug))
            return list(rows or [])

        async def delete(self, *, uid: str, agent_slug: str, memory_id: int):
            calls.append(("delete", uid, agent_slug, memory_id))
            return delete_hits

    monkeypatch.setattr(service, "AgentMemoryRepository", FakeRepository)
    return calls


async def test_is_memory_enabled_maps_user_config_value(monkeypatch: pytest.MonkeyPatch):
    events: list[str] = []
    _patch_session(monkeypatch, events)

    _patch_config(monkeypatch, enabled=False)
    assert await service.is_memory_enabled("user-1") is False

    _patch_config(monkeypatch, enabled=True)
    assert await service.is_memory_enabled("user-1") is True


async def test_is_memory_enabled_short_circuits_on_blank_uid(monkeypatch: pytest.MonkeyPatch):
    events: list[str] = []
    _patch_session(monkeypatch, events)
    loads = _patch_config(monkeypatch, enabled=True)

    assert await service.is_memory_enabled("") is False
    assert await service.is_memory_enabled("   ") is False
    assert loads == []


async def test_load_injection_entries_skips_repository_when_switch_disabled(monkeypatch: pytest.MonkeyPatch):
    events: list[str] = []
    _patch_session(monkeypatch, events)
    _patch_config(monkeypatch, enabled=False)

    class RepositoryMustNotBeTouched:
        def __init__(self, _db):
            raise AssertionError("开关关闭时不得访问记忆库")

    monkeypatch.setattr(service, "AgentMemoryRepository", RepositoryMustNotBeTouched)

    assert await service.load_injection_entries(uid="user-1", agent_slug="agent-1") == []


async def test_load_injection_entries_returns_contents_in_repository_order(monkeypatch: pytest.MonkeyPatch):
    events: list[str] = []
    _patch_session(monkeypatch, events)
    _patch_config(monkeypatch, enabled=True)
    calls = _patch_repository(
        monkeypatch,
        rows=[
            {"content": "记忆A", "id": 3},
            {"content": "记忆B", "id": 2},
            {"content": "记忆C", "id": 1},
        ],
    )

    entries = await service.load_injection_entries(uid="user-1", agent_slug="agent-1")

    assert entries == ["记忆A", "记忆B", "记忆C"]
    assert calls == [("list", "user-1", "agent-1")]

    limited = await service.load_injection_entries(uid="user-1", agent_slug="agent-1", limit=2)
    assert limited == ["记忆A", "记忆B"]


async def test_delete_memory_miss_raises_value_error(monkeypatch: pytest.MonkeyPatch):
    events: list[str] = []
    _patch_session(monkeypatch, events)
    calls = _patch_repository(monkeypatch, delete_hits=False)

    with pytest.raises(ValueError, match="记忆不存在"):
        await service.delete_memory(uid="user-1", agent_slug="agent-1", memory_id=7)

    assert calls == [("delete", "user-1", "agent-1", 7)]


async def test_delete_memory_hit_returns_normally(monkeypatch: pytest.MonkeyPatch):
    events: list[str] = []
    _patch_session(monkeypatch, events)
    calls = _patch_repository(monkeypatch, delete_hits=True)

    assert await service.delete_memory(uid="user-1", agent_slug="agent-1", memory_id=7) is None
    assert calls == [("delete", "user-1", "agent-1", 7)]
