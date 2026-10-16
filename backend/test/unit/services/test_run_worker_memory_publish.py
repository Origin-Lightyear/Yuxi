"""run_worker completed 终态 best-effort 发布 memory_extract 抽取任务的单元测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import yuxi.services.run_worker as run_worker


class _CaptureTaskRepository:
    """记录 TaskRepository().create 收到的 task_id 与 data。"""

    def __init__(self) -> None:
        self.task_id: str | None = None
        self.data: dict | None = None

    async def create(self, task_id: str, data: dict) -> tuple:
        self.task_id = task_id
        self.data = data
        return SimpleNamespace(id=task_id), True


@pytest.fixture
def deps(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """替换 helper 的三个外部依赖：开关、任务仓储、队列发布与系统配置。"""
    capture = _CaptureTaskRepository()
    enabled = AsyncMock(return_value=True)
    publish = AsyncMock()
    options = SimpleNamespace(get=AsyncMock(return_value={"default_model": "system:model"}))
    monkeypatch.setattr(run_worker, "is_memory_enabled", enabled)
    monkeypatch.setattr(run_worker, "TaskRepository", lambda: capture)
    monkeypatch.setattr(run_worker, "publish_task", publish)
    monkeypatch.setattr(run_worker, "system_options", options)
    return SimpleNamespace(enabled=enabled, capture=capture, publish=publish, options=options)


def _completed_run(input_payload: dict) -> SimpleNamespace:
    return SimpleNamespace(id="run-123", status="completed", input_payload=input_payload)


async def test_switch_off_skips_create_and_publish(deps):
    """开关关闭时不创建任务也不发布，更不读系统配置。"""
    deps.enabled.return_value = False

    await run_worker._publish_memory_extract(
        uid="u1",
        agent_slug="agent-x",
        thread_id="thread-1",
        final_run=_completed_run({"model_spec": "p:model"}),
    )

    assert deps.capture.data is None
    deps.publish.assert_not_called()
    deps.options.get.assert_not_called()


async def test_payload_model_spec_creates_five_key_task(deps):
    """payload 带 model_spec 时任务 data 形状正确，且不再查询系统配置。"""
    await run_worker._publish_memory_extract(
        uid="u1",
        agent_slug="agent-x",
        thread_id="thread-1",
        final_run=_completed_run({"model_spec": "p:model"}),
    )

    assert deps.capture.data["type"] == "memory_extract"
    assert deps.capture.data["handler_version"] == 1
    assert deps.capture.data["payload"] == {
        "uid": "u1",
        "agent_slug": "agent-x",
        "thread_id": "thread-1",
        "run_id": "run-123",
        "model_spec": "p:model",
    }
    deps.options.get.assert_not_called()
    deps.publish.assert_awaited_once_with(deps.capture.task_id)


async def test_falls_back_to_system_default_model(deps):
    """payload 缺 model_spec 时回退到系统 default_model。"""
    await run_worker._publish_memory_extract(
        uid="u1",
        agent_slug="agent-x",
        thread_id="thread-1",
        final_run=_completed_run({}),
    )

    assert deps.capture.data["payload"]["model_spec"] == "system:model"
    deps.options.get.assert_awaited_once()


async def test_both_model_spec_empty_skips(deps):
    """payload 与系统 default_model 皆空时不创建任务、不发布。"""
    deps.options.get.return_value = {"default_model": ""}

    await run_worker._publish_memory_extract(
        uid="u1",
        agent_slug="agent-x",
        thread_id="thread-1",
        final_run=_completed_run({}),
    )

    assert deps.capture.data is None
    deps.publish.assert_not_called()


async def test_publish_failure_does_not_raise(deps):
    """publish_task 抛异常时 helper 不向上抛，调用方 Run 结果不受影响。"""
    deps.publish.side_effect = RuntimeError("redis unavailable")

    await run_worker._publish_memory_extract(
        uid="u1",
        agent_slug="agent-x",
        thread_id="thread-1",
        final_run=_completed_run({"model_spec": "p:model"}),
    )

    assert deps.capture.data["type"] == "memory_extract"
