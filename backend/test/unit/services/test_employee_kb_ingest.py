"""Agent 工具侧知识库文档入库服务单元测试。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


class _FakeKnowledgeBase:
    def __init__(self, calls: dict):
        self._calls = calls

    async def file_existed_in_db(self, kb_id: str, content_hash: str) -> bool:
        self._calls["existed"] = (kb_id, content_hash)
        return False

    async def add_file_record(self, kb_id: str, minio_url: str, *, params: dict, operator_id: str):
        self._calls["record"] = {"kb_id": kb_id, "minio_url": minio_url, "params": params, "operator_id": operator_id}
        return {"file_id": "f-1"}

    async def delete_file(self, kb_id: str, file_id: str):
        self._calls["deleted"] = (kb_id, file_id)


class _FakePgManager:
    def get_async_session_context(self):
        @asynccontextmanager
        async def _cm():
            yield object()

        return _cm()


async def test_upload_employee_document_runs_full_ingest_chain(tmp_path, monkeypatch):
    import yuxi.services.employee_kb_ingest as ingest
    from yuxi.storage.minio.client import MinIOClient

    source = tmp_path / "报告.md"
    source.write_bytes("# 报告内容\n".encode())

    calls: dict = {}

    async def fake_upload(bucket: str, object_name: str, data: bytes) -> str:
        calls["minio"] = (bucket, object_name, data)
        return "https://minio.example/kb-1/upload/doc.md"

    async def fake_employee_context(_session, _operator_uid):
        return SimpleNamespace(employee_id=42)

    async def fake_enqueue(kb_id: str, file_id: str, operator_uid: str):
        calls["enqueue"] = (kb_id, file_id, operator_uid)
        return SimpleNamespace(id="task-9")

    monkeypatch.setattr(ingest, "knowledge_base", _FakeKnowledgeBase(calls))
    monkeypatch.setattr(ingest, "aupload_file_to_minio", fake_upload)
    monkeypatch.setattr("yuxi.storage.postgres.manager.pg_manager", _FakePgManager())
    monkeypatch.setattr("yuxi.services.saas_identity.get_saas_employee_context", fake_employee_context)
    monkeypatch.setattr(ingest, "_enqueue_ingest_task", fake_enqueue)

    result = await ingest.upload_employee_document("kb-1", source, "folder-1", "u1")

    assert result == {
        "file_id": "f-1",
        "filename": "报告.md",
        "status": "queued",
        "task_id": "task-9",
        "message": "已上传并提交解析入库任务",
    }
    assert calls["record"]["kb_id"] == "kb-1"
    assert calls["record"]["operator_id"] == "u1"
    assert calls["record"]["params"]["parent_id"] == "folder-1"
    assert calls["record"]["params"]["uploader_id"] == "42"
    assert calls["enqueue"] == ("kb-1", "f-1", "u1")
    assert calls["minio"][0] == MinIOClient.KB_BUCKETS["documents"]
    assert calls["minio"][1].startswith("kb-1/upload/")


async def test_delete_employee_document_removes_record(monkeypatch):
    import yuxi.services.employee_kb_ingest as ingest

    calls: dict = {}
    monkeypatch.setattr(ingest, "knowledge_base", _FakeKnowledgeBase(calls))

    result = await ingest.delete_employee_document("kb-1", "f-2")

    assert result == {"message": "删除成功"}
    assert calls["deleted"] == ("kb-1", "f-2")
