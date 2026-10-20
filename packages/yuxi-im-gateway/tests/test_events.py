import json

from yuxi_im_gateway.events import reduce_run_events


def _ev(event: str, payload: dict) -> dict:
    return {"event": event, "data": json.dumps({"thread_id": "t1", "payload": payload})}


def test_text_deltas_are_concatenated():
    events = [
        _ev("messages", {"items": [{"stream_event": {"type": "message_delta", "content": "hello "}}]}),
        _ev("messages", {"items": [{"stream_event": {"type": "message_delta", "content": "world"}}]}),
        _ev("end", {"status": "completed"}),
    ]
    outcome = reduce_run_events(events, thread_id="t1")
    assert outcome.status == "completed"
    assert outcome.text == "hello world"


def test_artifacts_collected_from_agent_state():
    events = [
        _ev(
            "custom",
            {"name": "yuxi.agent_state", "agent_state": {"artifacts": ["/home/gem/user-data/outputs/a.xlsx"]}},
        ),
        _ev("end", {"status": "completed"}),
    ]
    outcome = reduce_run_events(events, thread_id="t1")
    assert outcome.artifact_paths == ["/home/gem/user-data/outputs/a.xlsx"]


def test_human_approval_interrupt_detected():
    events = [
        _ev("messages", {"items": [{"status": "human_approval_required"}]}),
        _ev("interrupt", {"reason": "human_approval", "chunk": {"status": "human_approval_required"}}),
        _ev("end", {"status": "interrupted", "chunk": {"status": "human_approval_required"}}),
    ]
    outcome = reduce_run_events(events, thread_id="t1")
    assert outcome.status == "approval_required"


def test_non_approval_interrupt_is_error():
    events = [
        _ev("interrupt", {"reason": "ask_user_question_required", "chunk": {"status": "ask_user_question_required"}}),
        _ev("end", {"status": "interrupted"}),
    ]
    outcome = reduce_run_events(events, thread_id="t1")
    assert outcome.status == "error"


def test_error_event_surfaces_message():
    events = [_ev("error", {"chunk": {"error_message": "boom", "retryable": False}})]
    outcome = reduce_run_events(events, thread_id="t1")
    assert outcome.status == "error"
    assert outcome.error_message == "boom"


def test_events_from_other_threads_are_dropped():
    events = [
        _ev("messages", {"items": [{"stream_event": {"type": "message_delta", "content": "main"}}]}),
        {
            "event": "messages",
            "data": json.dumps(
                {
                    "thread_id": "sub",
                    "payload": {"items": [{"stream_event": {"type": "message_delta", "content": "ignored"}}]},
                }
            ),
        },
        _ev("end", {"status": "completed"}),
    ]

    outcome = reduce_run_events(events, thread_id="t1")

    assert outcome.status == "completed"
    assert outcome.text == "main"
