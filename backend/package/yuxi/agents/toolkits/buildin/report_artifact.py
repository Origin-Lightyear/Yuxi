"""write_report_artifact 报表写入工具。"""

import asyncio
import json
import os
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from typing import Annotated

from langchain.tools import InjectedToolCallId
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command

from yuxi.agents.backends.sandbox import ProvisionerSandboxBackend
from yuxi.agents.toolkits.registry import tool

from .tools import _resolve_runtime_sandbox_scope, _runtime_scope_value

SUPPORTED_REPORT_SCHEMA_VERSION = 1
_REPORT_OUTPUT_DIR_NAME = "reports"
_ALLOWED_CHART_TYPES = frozenset({"bar", "line", "pie"})

WRITE_REPORT_ARTIFACT_DESCRIPTION = """把结构化报表校验后写入当前 Project 的 outputs/reports/，
并登记为可展示的报表 artifact。

报表参数为 JSON 对象（也接受 JSON 字符串），必填字段：
1. schema_version：整数，当前支持 1
2. title：报表标题（非空字符串）
3. generated_at：生成时间（非空字符串）
4. summary：结论摘要（非空字符串）
5. metrics[]：指标对象数组，含 key/label/value/unit，可选 source 溯源说明
6. tables[]：明细表对象数组，含 title/columns/rows
7. charts[]：图表对象数组，type 仅支持 bar/line/pie

校验失败会返回错误且不写入任何文件（fail-closed）；文件名由服务端按 UTC 时间戳生成，
模型输入不参与路径拼接。成功后返回的路径会并入 state artifacts，供前端展示。
"""


@tool(
    category="buildin",
    tags=["报表", "交付物"],
    display_name="写入报表",
    description=WRITE_REPORT_ARTIFACT_DESCRIPTION,
)
async def write_report_artifact(
    report: Annotated[str | dict, "报表 JSON 字符串或对象，字段见工具说明"],
    runtime: ToolRuntime,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """校验报表并写入 outputs/reports/，把文件路径并入 state artifacts。"""
    try:
        payload = _validate_report_payload(report)
        workdir_path = str(_runtime_scope_value(runtime, "workdir_path") or "").rstrip("/")
        if not workdir_path:
            raise ValueError("当前运行时缺少 workdir_path")
    except ValueError as exc:
        return Command(update={"messages": [ToolMessage(content=f"Error: {exc}", tool_call_id=tool_call_id)]})

    runtime_scope_id, uid, workdir_relative_path = _resolve_runtime_sandbox_scope(runtime)
    backend = ProvisionerSandboxBackend(
        thread_id=runtime_scope_id,
        uid=uid,
        workdir_path=workdir_relative_path,
        create_if_missing=True,
    )
    rel_path = _next_report_path(backend, workdir_path)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(prefix="yuxi-report-", delete=False) as temp_file:
            temp_path = temp_file.name
            temp_file.write(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        await asyncio.to_thread(backend.upload_authorized_file_from_path, rel_path, temp_path)
    finally:
        if temp_path:
            with suppress(FileNotFoundError):
                await asyncio.to_thread(os.unlink, temp_path)

    return Command(
        update={
            "artifacts": [rel_path],
            "messages": [
                ToolMessage(
                    content=f"报表已写入 {rel_path}（schema_version={payload['schema_version']}）",
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )


def _validate_report_payload(report: str | dict) -> dict:
    """解析并校验报表负载，任何不符合 schema 的输入抛 ValueError。"""
    if isinstance(report, str):
        try:
            payload = json.loads(report)
        except json.JSONDecodeError as exc:
            raise ValueError(f"报表 JSON 解析失败: {exc}") from exc
    else:
        payload = report
    if not isinstance(payload, dict):
        raise ValueError("报表必须是 JSON 对象")

    schema_version = payload.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ValueError("schema_version 必须是整数")
    if schema_version > SUPPORTED_REPORT_SCHEMA_VERSION:
        raise ValueError(f"schema_version 超出当前支持版本 {SUPPORTED_REPORT_SCHEMA_VERSION}")

    for name in ("title", "generated_at", "summary"):
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"必填字段 {name} 必须是非空字符串")
    for name in ("metrics", "tables"):
        items = payload.get(name)
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValueError(f"必填字段 {name} 必须是对象数组")

    charts = payload.get("charts")
    if not isinstance(charts, list):
        raise ValueError("必填字段 charts 必须是数组")
    for index, chart in enumerate(charts):
        if not isinstance(chart, dict) or chart.get("type") not in _ALLOWED_CHART_TYPES:
            raise ValueError(f"charts[{index}].type 仅支持 bar/line/pie")
    return payload


def _next_report_path(backend, workdir_path: str) -> str:
    """选择不重名的报表路径；文件名只含 UTC 时间戳与消解后缀，模型输入不参与拼接。"""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = f"{workdir_path}/outputs/{_REPORT_OUTPUT_DIR_NAME}/report-{stamp}"
    candidate = f"{base}.json"
    index = 1
    while backend.regular_file_exists(candidate):
        candidate = f"{base}-{index}.json"
        index += 1
    return candidate
