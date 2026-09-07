"""Agent 工具侧的知识库文档上传/删除服务。

上传走与 REST 管理端一致的链路：MinIO 上传 → add_file_record 建档 →
tasker 复合任务串行 parse → index。权限（KB 级 MANAGE 或目录 edit）由调用方
（Agent 工具 / REST 依赖）先行校验，本模块不再重复校验。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from yuxi.knowledge.runtime import knowledge_base
from yuxi.storage.minio.client import MinIOClient, aupload_file_to_minio
from yuxi.utils import logger
from yuxi.utils.upload_utils import MAX_UPLOAD_SIZE_BYTES, calculate_content_hash, is_supported_file_extension

EMPLOYEE_INGEST_TASK_TYPE = "knowledge_employee_ingest"


async def upload_employee_document(
    kb_id: str,
    source_path: Path,
    parent_id: str | None,
    operator_uid: str,
) -> dict[str, Any]:
    """把本地文件上传到知识库目录并提交解析入库任务。

    Raises:
        ValueError: 文件不合法或内容重复。
    """
    filename = source_path.name
    if not is_supported_file_extension(filename):
        raise ValueError(f"不支持的文件类型: {os.path.splitext(filename)[1]}")
    file_bytes = source_path.read_bytes()
    if len(file_bytes) > MAX_UPLOAD_SIZE_BYTES:
        raise ValueError("文件过大，当前仅支持 100 MB 以内的文件")

    content_hash = await calculate_content_hash(file_bytes)
    if await knowledge_base.file_existed_in_db(kb_id, content_hash):
        raise ValueError("知识库中已存在相同内容的文件")

    basename, ext = os.path.splitext(filename)
    minio_filename = f"{basename}_{int(time.time() * 1000)}{ext}"
    object_name = f"{kb_id}/upload/{minio_filename}"
    minio_url = await aupload_file_to_minio(MinIOClient.KB_BUCKETS["documents"], object_name, file_bytes)

    file_meta = await knowledge_base.add_file_record(
        kb_id,
        minio_url,
        params={
            "content_type": "file",
            "parent_id": parent_id,
            "content_hashes": {minio_url: content_hash},
            "file_sizes": {minio_url: len(file_bytes)},
        },
        operator_id=operator_uid,
    )
    file_id = file_meta["file_id"]
    task = await _enqueue_ingest_task(kb_id, file_id, operator_uid)
    return {
        "file_id": file_id,
        "filename": filename,
        "status": "queued",
        "task_id": task.id,
        "message": "已上传并提交解析入库任务",
    }


async def delete_employee_document(kb_id: str, file_id: str) -> dict[str, Any]:
    """删除知识库文档（权限由调用方校验；不处理文件夹）。"""
    await knowledge_base.delete_file(kb_id, file_id)
    return {"message": "删除成功"}


async def _enqueue_ingest_task(kb_id: str, file_id: str, operator_uid: str):
    """提交复合任务：解析成功后入库。"""
    from yuxi.services.task_service import TaskContext, tasker

    async def run_ingest(context: TaskContext):
        try:
            await context.set_progress(5.0, "开始解析文档")
            await knowledge_base.parse_file(kb_id, file_id, operator_uid)
            await context.set_progress(55.0, "解析完成，开始入库")
            await knowledge_base.index_file(kb_id, file_id, operator_uid)
            await context.set_progress(100.0, "入库完成")
            return {"file_id": file_id, "status": "completed"}
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"员工文档入库任务失败 kb_id={kb_id} file_id={file_id}: {exc}")
            raise

    return await tasker.enqueue(
        name=f"文档上传入库 ({file_id})",
        task_type=EMPLOYEE_INGEST_TASK_TYPE,
        payload={"kb_id": kb_id, "file_id": file_id},
        coroutine=run_ingest,
    )
