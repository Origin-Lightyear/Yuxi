"""知识库建档校验最终 MinIO 对象归属，而非信任客户端 hash。"""

import pytest
from fastapi import HTTPException

from server.routers.knowledge_router import _validate_uploaded_document_items


@pytest.mark.parametrize(
    "source",
    [
        "minio://knowledgebases/kb_other/upload/a.txt",
        "minio://knowledgebases/kb_current_other/upload/a.txt",
        "minio://knowledgebases/%6Bb_other/upload/a.txt",
        "minio://knowledgebases/kb_current/%2e%2e/kb_other/a.txt",
        "minio://chat-attachments/kb_current/upload/a.txt",
        "https://minio/knowledgebases/kb_other/upload/a.txt",
    ],
)
@pytest.mark.parametrize("override", [False, True])
def test_rejects_foreign_bucket_or_decoded_object_prefix(source, override):
    """直接地址与预处理覆盖地址都不能绕过目录和 bucket 边界。"""
    item = "minio://knowledgebases/kb_current/upload/a.txt" if override else source
    params = {"content_hashes": {item: "client-hash"}}
    if override:
        params["_preprocessed_map"] = {item: {"path": source, "content_hash": "client-hash"}}
    with pytest.raises(HTTPException) as error:
        _validate_uploaded_document_items("kb_current", [item], params)
    assert error.value.status_code == 403


@pytest.mark.parametrize(
    "source",
    [
        "minio://knowledgebases/kb_current/upload/a.txt",
        "https://minio/knowledgebases/kb_current/upload/%E6%B5%8B%E8%AF%95.txt",
    ],
)
@pytest.mark.parametrize("override", [False, True])
def test_allows_target_kb_uploaded_and_preprocessed_sources(source, override):
    """本库普通上传和预处理 HTML 使用相同归属规则。"""
    params = {"content_hashes": {source: "client-hash"}}
    if override:
        params["_preprocessed_map"] = {source: {"path": source, "content_hash": "client-hash"}}
    _validate_uploaded_document_items("kb_current", [source], params)
