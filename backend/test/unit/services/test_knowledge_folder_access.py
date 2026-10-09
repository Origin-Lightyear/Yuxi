"""目录边界校验不允许把员工文档挂载到其它知识库。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from server.utils.employee_kb_permissions import ensure_kb_folder
from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository


@pytest.mark.parametrize(
    ("record", "expected_status"),
    [
        (None, 404),
        (SimpleNamespace(kb_id="foreign", is_folder=True), 404),
        (SimpleNamespace(kb_id="current", is_folder=False), 400),
    ],
)
async def test_invalid_target_folder_fails_before_document_write(monkeypatch, record, expected_status):
    """跨库、不存在和非目录目标均不得成为文档父目录。"""
    monkeypatch.setattr(KnowledgeFileRepository, "get_by_file_id", AsyncMock(return_value=record))
    with pytest.raises(HTTPException) as error:
        await ensure_kb_folder("current", "target")
    assert error.value.status_code == expected_status


async def test_same_kb_folder_and_root_are_allowed(monkeypatch):
    """本库目录和根目录都是合法的上传、移动目标。"""
    monkeypatch.setattr(
        KnowledgeFileRepository,
        "get_by_file_id",
        AsyncMock(return_value=SimpleNamespace(kb_id="current", is_folder=True)),
    )
    await ensure_kb_folder("current", "target")
    await ensure_kb_folder("current", None)
