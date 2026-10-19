"""memory_extract_task 的抽取解析、开关短路、幂等写入与容量截断测试。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from yuxi.services import memory_extract_task as task
from yuxi.services.task_registry import get_task_definition

pytestmark = pytest.mark.unit

PAYLOAD = {
    "uid": "u1",
    "agent_slug": "assistant",
    "thread_id": "thread-1",
    "run_id": "run-1",
    "model_spec": "test/model-spec",
}

DIALOGUE = [
    {"role": "user", "content": "我负责财务报表"},
    {"role": "assistant", "content": "好的，我会记住"},
]


class FakeContext:
    """只提供 handler 实际使用的 payload 与进度、取消桩。"""

    def __init__(self, payload: dict):
        self.payload = payload
        self.task_id = "task-1"
        self.worker_id = "worker-1"
        self.progress: list[tuple[float, str | None]] = []

    async def set_progress(self, progress: float, message: str | None = None) -> None:
        self.progress.append((progress, message))

    async def raise_if_cancelled(self) -> None:
        return None


def _patch_session(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """替换任务自开的数据库会话，镜像真实上下文的退出提交并记录事务事件。"""
    events: list[str] = []

    class FakeDB:
        async def commit(self):
            events.append("commit")

        async def rollback(self):
            events.append("rollback")

    @asynccontextmanager
    async def fake_session():
        db = FakeDB()
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    monkeypatch.setattr(task, "pg_manager", SimpleNamespace(get_async_session_context=fake_session))
    return events


def _patch_switch(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    async def fake_is_memory_enabled(uid: str) -> bool:
        return enabled

    monkeypatch.setattr(task, "is_memory_enabled", fake_is_memory_enabled)


def _patch_conversations(monkeypatch: pytest.MonkeyPatch, messages: list[dict]) -> list[dict]:
    """替换 ConversationRepository 并记录读取参数。"""
    calls: list[dict] = []

    class FakeRepository:
        def __init__(self, _db):
            pass

        async def read_memory_messages(
            self, *, uid: str, thread_id: str, message_id=None, limit: int = 20, include_tools: bool = False
        ):
            calls.append({"uid": uid, "thread_id": thread_id, "limit": limit, "include_tools": include_tools})
            return {"thread_id": thread_id, "messages": list(messages)}

    monkeypatch.setattr(task, "ConversationRepository", FakeRepository)
    return calls


def _patch_memories(monkeypatch: pytest.MonkeyPatch, *, fail_add_at: int | None = None) -> list[tuple]:
    """替换 AgentMemoryRepository；fail_add_at 指定第几次 add 抛唯一冲突。"""
    calls: list[tuple] = []
    add_count = 0

    class FakeRepository:
        def __init__(self, _db):
            pass

        async def add(self, *, uid: str, agent_slug: str, content: str, source_run_id: str, extract_index: int) -> dict:
            nonlocal add_count
            add_count += 1
            calls.append(("add", uid, agent_slug, content, source_run_id, extract_index))
            if fail_add_at is not None and add_count == fail_add_at:
                raise IntegrityError("INSERT INTO agent_memories", {}, Exception("duplicate key"))
            return {"content": content}

        async def delete_oldest_over_limit(self, *, uid: str, agent_slug: str, limit: int = 50) -> int:
            calls.append(("trim", uid, agent_slug, limit))
            return 0

    monkeypatch.setattr(task, "AgentMemoryRepository", FakeRepository)
    return calls


def _patch_model(monkeypatch: pytest.MonkeyPatch, content: str) -> list[list]:
    """替换 select_model，记录发送给模型的消息并返回脚本化输出。"""
    sent: list[list] = []

    class FakeAdapter:
        async def call(self, messages, stream=False):
            sent.append(messages)
            return SimpleNamespace(content=content)

    monkeypatch.setattr(task, "select_model", lambda model_spec: FakeAdapter())
    return sent


def test_memory_extract_task_is_registered():
    definition = get_task_definition("memory_extract")
    assert definition.module == "yuxi.services.memory_extract_task"
    assert definition.function == "run_memory_extract"
    assert definition.load_handler() is task.run_memory_extract


async def test_extract_memory_items_parses_json_array(monkeypatch: pytest.MonkeyPatch):
    sent = _patch_model(monkeypatch, '["喜欢简洁回答", "负责财务报表"]')

    items = await task.extract_memory_items(messages=DIALOGUE, model_spec="test/model-spec")

    assert items == ["喜欢简洁回答", "负责财务报表"]
    assert len(sent) == 1
    assert len(sent[0]) == 2
    system_message, user_message = sent[0]
    assert system_message["role"] == "system"
    assert system_message["content"] == task.EXTRACT_SYSTEM_PROMPT
    assert user_message["role"] == "user"
    assert "我负责财务报表" in user_message["content"]


async def test_extract_memory_items_returns_empty_for_empty_array(monkeypatch: pytest.MonkeyPatch):
    _patch_model(monkeypatch, "[]")

    assert await task.extract_memory_items(messages=DIALOGUE, model_spec="test/model-spec") == []


async def test_extract_memory_items_returns_empty_for_non_json(monkeypatch: pytest.MonkeyPatch):
    _patch_model(monkeypatch, "ok")

    assert await task.extract_memory_items(messages=DIALOGUE, model_spec="test/model-spec") == []


async def test_extract_memory_items_truncates_overlong_item(monkeypatch: pytest.MonkeyPatch):
    _patch_model(monkeypatch, '["' + "很长的条目" * 21 + '"]')

    items = await task.extract_memory_items(messages=DIALOGUE, model_spec="test/model-spec")

    assert items == ["很长的条目" * 20]


async def test_run_memory_extract_skips_when_switch_disabled(monkeypatch: pytest.MonkeyPatch):
    _patch_session(monkeypatch)
    _patch_switch(monkeypatch, enabled=False)
    read_calls = _patch_conversations(monkeypatch, DIALOGUE)
    sent = _patch_model(monkeypatch, '["喜欢简洁回答"]')
    memory_calls = _patch_memories(monkeypatch)

    result = await task.run_memory_extract(FakeContext(PAYLOAD))

    assert result == {"skipped": True}
    assert sent == []
    assert read_calls == []
    assert memory_calls == []


async def test_run_memory_extract_skips_on_invisible_thread(monkeypatch: pytest.MonkeyPatch):
    """不可见线程（subagent/归档）应记为 skipped，而非 failed durable task。"""
    _patch_session(monkeypatch)
    _patch_switch(monkeypatch, enabled=True)

    class InvisibleRepository:
        def __init__(self, _db):
            pass

        async def read_memory_messages(self, **_kwargs):
            raise ValueError("历史线程不存在或不可见")

    monkeypatch.setattr(task, "ConversationRepository", InvisibleRepository)
    sent = _patch_model(monkeypatch, '["喜欢简洁回答"]')
    memory_calls = _patch_memories(monkeypatch)

    result = await task.run_memory_extract(FakeContext(PAYLOAD))

    assert result == {"skipped": True}
    assert sent == []
    assert memory_calls == []


async def test_run_memory_extract_skips_on_empty_conversation(monkeypatch: pytest.MonkeyPatch):
    _patch_session(monkeypatch)
    _patch_switch(monkeypatch, enabled=True)
    _patch_conversations(monkeypatch, [])
    sent = _patch_model(monkeypatch, '["喜欢简洁回答"]')
    memory_calls = _patch_memories(monkeypatch)

    result = await task.run_memory_extract(FakeContext(PAYLOAD))

    assert result == {"skipped": True}
    assert sent == []
    assert memory_calls == []


async def test_run_memory_extract_writes_items_then_trims_to_limit(monkeypatch: pytest.MonkeyPatch):
    events = _patch_session(monkeypatch)
    _patch_switch(monkeypatch, enabled=True)
    read_calls = _patch_conversations(monkeypatch, DIALOGUE)
    _patch_model(monkeypatch, '["喜欢简洁回答", "负责财务报表"]')
    memory_calls = _patch_memories(monkeypatch)

    result = await task.run_memory_extract(FakeContext(PAYLOAD))

    assert result == {"extracted": 2}
    assert read_calls == [{"uid": "u1", "thread_id": "thread-1", "limit": 20, "include_tools": False}]
    adds = [call for call in memory_calls if call[0] == "add"]
    assert [call[3] for call in adds] == ["喜欢简洁回答", "负责财务报表"]
    assert all(call[1] == "u1" and call[2] == "assistant" and call[4] == "run-1" for call in adds)
    assert [call[5] for call in adds] == [0, 1]
    assert task.AGENT_MEMORY_LIMIT == 50
    assert ("trim", "u1", "assistant", 50) in memory_calls
    assert "commit" in events


async def test_run_memory_extract_skips_conflicting_item_and_keeps_first(
    monkeypatch: pytest.MonkeyPatch,
):
    events = _patch_session(monkeypatch)
    _patch_switch(monkeypatch, enabled=True)
    _patch_conversations(monkeypatch, DIALOGUE)
    _patch_model(monkeypatch, '["第一条事实", "第二条事实"]')
    memory_calls = _patch_memories(monkeypatch, fail_add_at=2)

    result = await task.run_memory_extract(FakeContext(PAYLOAD))

    assert result == {"extracted": 1}
    adds = [call for call in memory_calls if call[0] == "add"]
    assert [call[3] for call in adds] == ["第一条事实", "第二条事实"]
    assert [call[4] for call in adds] == ["run-1", "run-1"]
    assert [call[5] for call in adds] == [0, 1]
    assert "rollback" in events
    assert events[-1] == "commit"
    assert ("trim", "u1", "assistant", 50) in memory_calls
