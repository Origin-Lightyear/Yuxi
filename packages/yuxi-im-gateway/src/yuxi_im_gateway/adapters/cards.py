"""审批交互卡片的构造与回调解析（平台无关的中间结构）。"""

from __future__ import annotations

import json


def approval_card(thread_id: str, run_id: str, requester_id: str) -> dict:
    """构造含批准 / 拒绝两个按钮的审批卡片描述。"""
    base = {"thread_id": thread_id, "run_id": run_id, "requester_id": requester_id}
    return {
        "title": "工具审批",
        "text": "Agent 请求执行一个需要审批的工具操作，请选择：",
        "actions": [
            {"label": "批准", "decision": "approve", "value": {**base, "decision": "approve"}},
            {"label": "拒绝", "decision": "reject", "value": {**base, "decision": "reject"}},
        ],
    }


def parse_card_action(value) -> tuple[str, str, str, str]:
    """解析按钮回调 value，返回 (decision, thread_id, run_id, requester_id)。"""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("卡片回调 value 格式无效")
    return (
        str(value.get("decision") or ""),
        str(value.get("thread_id") or ""),
        str(value.get("run_id") or ""),
        str(value.get("requester_id") or ""),
    )
