"""把 Yuxi Agent Run 的 SSE 事件归并为一次运行的结果。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field


@dataclass(slots=True)
class RunOutcome:
    """一次 Agent 运行的归并结果。"""

    status: str  # completed / approval_required / error
    text: str = ""
    artifact_paths: list[str] = field(default_factory=list)
    error_message: str | None = None


def iter_sse_events(lines: Iterable[str]) -> Iterator[dict[str, str]]:
    """解析 SSE 文本行，忽略 heartbeat，保留 event / data / id。"""
    event: dict[str, str] = {}
    data_lines: list[str] = []
    for line in lines:
        if not line:
            if data_lines:
                event["data"] = "\n".join(data_lines)
                yield event
            event = {}
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if not separator:
            value = ""
        elif value.startswith(" "):
            value = value[1:]
        if field == "data":
            data_lines.append(value)
        elif field in {"event", "id"}:
            event[field] = value
    if data_lines:
        event["data"] = "\n".join(data_lines)
        yield event


def _chunks(payload: dict) -> list[dict]:
    """取出事件 payload 中的 chunk 列表（items 优先）。"""
    items = payload.get("items")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    chunk = payload.get("chunk")
    return [chunk] if isinstance(chunk, dict) else []


def reduce_run_events(events: Iterable[dict], thread_id: str | None = None) -> RunOutcome:
    """把 SSE 事件流归并为正文、交付物路径与终态。"""
    text_parts: list[str] = []
    artifacts: list[str] = []
    approval = False
    text = ""

    for event in events:
        try:
            data = json.loads(event.get("data") or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if thread_id and data.get("thread_id") not in {None, thread_id}:
            continue

        event_type = event.get("event") or "message"
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}

        for chunk in _chunks(payload):
            if chunk.get("status") == "human_approval_required":
                approval = True
            stream_event = chunk.get("stream_event")
            if isinstance(stream_event, dict) and stream_event.get("type") == "message_delta":
                content = stream_event.get("content")
                if isinstance(content, str) and content:
                    text_parts.append(content)
            state = chunk.get("agent_state")
            if isinstance(state, dict) and isinstance(state.get("artifacts"), list):
                artifacts.extend(str(path) for path in state["artifacts"])

        if isinstance(payload.get("agent_state"), dict):
            state = payload["agent_state"]
            if isinstance(state.get("artifacts"), list):
                artifacts.extend(str(path) for path in state["artifacts"])

        if event_type == "error":
            chunk = payload.get("chunk") if isinstance(payload.get("chunk"), dict) else {}
            if chunk.get("retryable") is True or payload.get("retryable") is True:
                continue
            message = chunk.get("error_message") or chunk.get("message") or "运行失败"
            return RunOutcome(status="error", text="".join(text_parts), error_message=str(message))

        if event_type == "end":
            text = "".join(text_parts)
            status = str(payload.get("status") or "completed")
            if status == "interrupted":
                return RunOutcome(
                    status="approval_required" if approval else "error",
                    text=text,
                    artifact_paths=list(dict.fromkeys(artifacts)),
                    error_message=None if approval else "运行被中断",
                )
            if status != "completed":
                return RunOutcome(status="error", text=text, error_message=f"运行结束：{status}")
            return RunOutcome(status="completed", text=text, artifact_paths=list(dict.fromkeys(artifacts)))

    return RunOutcome(
        status="approval_required" if approval else "error",
        text="".join(text_parts),
        artifact_paths=list(dict.fromkeys(artifacts)),
        error_message=None if approval else "运行事件流在终态前断开",
    )
