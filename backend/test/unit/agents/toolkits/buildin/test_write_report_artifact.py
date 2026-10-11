"""测试内置 write_report_artifact 报表写入工具的校验与落盘契约。"""

import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from yuxi.agents.toolkits.buildin import report_artifact

_REPORT_PATH_PATTERN = re.compile(r"^u1/project/outputs/reports/report-\d{8}T\d{6}Z\.json$")


class FakeSandboxBackend:
    """按可控集合判定文件存在并记录上传字节的假沙盒后端。"""

    instances: list["FakeSandboxBackend"] = []
    existing_paths: set[str] = set()

    def __init__(self, thread_id=None, uid=None, workdir_path=None, create_if_missing=False):
        self.thread_id = thread_id
        self.uid = uid
        self.workdir_path = workdir_path
        self.create_if_missing = create_if_missing
        self.uploaded: dict[str, bytes] = {}
        FakeSandboxBackend.instances.append(self)

    def regular_file_exists(self, path: str) -> bool:
        return path in FakeSandboxBackend.existing_paths

    def upload_authorized_file_from_path(self, dest_rel_path: str, local_path: str) -> None:
        with open(local_path, "rb") as handle:
            self.uploaded[dest_rel_path] = handle.read()


@pytest.fixture(autouse=True)
def fake_backend(monkeypatch):
    """重置假后端状态，并把工具内的沙盒后端替换为假实现。"""
    FakeSandboxBackend.instances.clear()
    FakeSandboxBackend.existing_paths.clear()
    monkeypatch.setattr(report_artifact, "ProvisionerSandboxBackend", FakeSandboxBackend)
    return FakeSandboxBackend


def _runtime() -> SimpleNamespace:
    """构造带完整 workdir scope 的假 ToolRuntime。"""
    return SimpleNamespace(
        config={
            "configurable": {
                "runtime_scope_id": "t1",
                "uid": "u1",
                "workdir_relative_path": "u1/project",
                "workdir_path": "u1/project",
            }
        }
    )


def _minimal_report() -> dict:
    """构造最小合法报表。"""
    return {
        "schema_version": 1,
        "title": "经营月报",
        "generated_at": "2026-10-10T08:00:00Z",
        "summary": "本月收入环比回升。",
        "validation_status": "agent_declared",
        "metrics": [
            {
                "key": "revenue",
                "label": "收入",
                "value": 120.5,
                "unit": "万元",
                "source": "SELECT SUM(amount) FROM orders",
            }
        ],
        "tables": [{"title": "渠道收入", "columns": ["渠道", "金额"], "rows": [["直营", 80.5]]}],
        "charts": [
            {
                "type": "bar",
                "xKey": "month",
                "series": [{"name": "收入", "dataKey": "revenue"}],
                "data": [{"month": "10月", "revenue": 120.5}],
            }
        ],
    }


async def _call_tool(report):
    """以固定调用参数执行工具。"""
    return await report_artifact.write_report_artifact.coroutine(
        report=report,
        runtime=_runtime(),
        tool_call_id="call-1",
    )


def _assert_fail_closed(result, backend_cls):
    """断言失败路径不更新 artifacts 且没有产生任何上传。"""
    assert isinstance(result, Command)
    assert "artifacts" not in result.update
    message = result.update["messages"][0]
    assert isinstance(message, ToolMessage)
    assert message.content.startswith("Error:")
    assert all(not instance.uploaded for instance in backend_cls.instances)


async def test_writes_report_and_updates_artifacts(fake_backend):
    report = _minimal_report()

    result = await _call_tool(report)

    assert isinstance(result, Command)
    paths = result.update["artifacts"]
    assert len(paths) == 1
    assert _REPORT_PATH_PATTERN.match(paths[0])
    message = result.update["messages"][0]
    assert isinstance(message, ToolMessage)
    assert paths[0] in message.content

    uploaded = fake_backend.instances[0].uploaded
    assert list(uploaded) == paths
    payload = json.loads(uploaded[paths[0]])
    assert payload == report
    assert payload["schema_version"] == 1
    assert payload["validation_status"] == "agent_declared"


async def test_missing_required_field_fails_closed(fake_backend):
    report = _minimal_report()
    del report["title"]

    _assert_fail_closed(await _call_tool(report), fake_backend)


async def test_unsupported_schema_version_fails_closed(fake_backend):
    report = _minimal_report()
    report["schema_version"] = 2

    _assert_fail_closed(await _call_tool(report), fake_backend)


async def test_invalid_chart_type_fails_closed(fake_backend):
    report = _minimal_report()
    report["charts"][0]["type"] = "scatter"

    _assert_fail_closed(await _call_tool(report), fake_backend)


async def test_report_json_string_accepted(fake_backend):
    report = _minimal_report()

    result = await _call_tool(json.dumps(report, ensure_ascii=False))

    assert _REPORT_PATH_PATTERN.match(result.update["artifacts"][0])
    payload = json.loads(next(iter(fake_backend.instances[0].uploaded.values())))
    assert payload == report


async def test_collision_appends_numeric_suffix(fake_backend, monkeypatch):
    fixed = datetime(2026, 10, 10, 8, 30, 0, tzinfo=UTC)

    class _FixedDateTime:
        """把模块内 datetime.now 固定为指定 UTC 时刻，令首个候选路径可预期。"""

        @staticmethod
        def now(tz=None):
            return fixed

    monkeypatch.setattr(report_artifact, "datetime", _FixedDateTime)
    existing = "u1/project/outputs/reports/report-20261010T083000Z.json"
    fake_backend.existing_paths.add(existing)

    result = await _call_tool(_minimal_report())

    assert result.update["artifacts"] == ["u1/project/outputs/reports/report-20261010T083000Z-1.json"]
