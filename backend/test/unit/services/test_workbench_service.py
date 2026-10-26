from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from yuxi.repositories.work_item_repository import WorkItemRow
from yuxi.services.workbench_service import (
    parse_work_item_key,
    run_work_item_key,
    work_item_to_dto,
)

pytestmark = [pytest.mark.unit]


def _row(**overrides) -> WorkItemRow:
    values = {
        "run_id": "run-1",
        "thread_id": "thread-1",
        "conversation_id": 1,
        "agent_slug": "assistant",
        "agent_name": "执行助手",
        "title": "季度复盘",
        "input_text": "帮我整理季度复盘",
        "source": "chat",
        "run_status": "completed",
        "work_status": "completed",
        "error_type": None,
        "created_at": datetime(2026, 1, 1, 9, 0, 0),
        "updated_at": datetime(2026, 1, 1, 9, 1, 0),
    }
    values.update(overrides)
    return WorkItemRow(**values)


def test_work_item_dto_maps_scheduled_run_to_digital_employee() -> None:
    item = work_item_to_dto(
        _row(
            source="scheduled_agent",
            run_status="pending",
            work_status="queued",
        ),
        viewer=SimpleNamespace(uid="u1", username="张三"),
        unread=True,
    )

    assert item["initiator_type"] == "digital_employee"
    assert item["initiator_name"] == "执行助手"
    assert item["status"] == "queued"
    assert item["source_type"] == "agent_run"
    assert item["key"] == "run:run-1"
    assert item["unread"] is True


def test_work_item_dto_maps_approval_kind_and_human_initiator() -> None:
    tool_item = work_item_to_dto(
        _row(run_status="interrupted", work_status="waiting_approval", error_type="human_approval_required"),
        viewer=SimpleNamespace(uid="u1", username="张三"),
        unread=False,
    )
    question_item = work_item_to_dto(
        _row(run_status="interrupted", work_status="waiting_approval", error_type="ask_user_question_required"),
        viewer=SimpleNamespace(uid="u1", username="张三"),
        unread=False,
    )

    assert tool_item["approval_kind"] == "tool"
    assert question_item["approval_kind"] == "question"
    assert tool_item["initiator_type"] == "human"
    assert tool_item["initiator_name"] == "张三"
    assert tool_item["approval_summary"] == "等待工具审批"


def test_work_item_title_falls_back_to_truncated_input() -> None:
    item = work_item_to_dto(
        _row(title=None, input_text="一" * 100),
        viewer=SimpleNamespace(uid="u1", username="张三"),
        unread=True,
    )
    assert item["title"] == "一" * 80 + "…"


def test_work_item_key_round_trip_and_rejects_unknown_source() -> None:
    key = run_work_item_key("run-1")
    assert key == "run:run-1"
    assert parse_work_item_key(key) == ("run", "run-1")
    with pytest.raises(ValueError):
        parse_work_item_key("unknown:1")
    with pytest.raises(ValueError):
        parse_work_item_key("run:")
