"""Tenant 服务端调用的租户知识库管理 API。

鉴权：`X-Tenant-Admin-Key`（与 env TENANT_ADMIN_API_KEY 比对）+ `X-Tenant-Id` 请求头。
SaaS 未启用或 key 未配置时一律 404，不暴露路由存在性；跨租户访问同样 404。

范围（与 Tenant 平台约定）：
- 知识库 CRUD 与 KB 级 share_config（权限仅通过创建/更新接口的 share_config 表达，无独立权限接口）；
- 目录：创建、重命名、移动、删除（仅允许删除空目录，非空返回 409）；
- 文档：分页列表、元数据（basic）、删除（文档录入仍由 Yuxi 管理端/员工链路完成）。
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query

from yuxi.knowledge.base import KBNameConflictError
from yuxi.knowledge.runtime import knowledge_base
from yuxi.storage.postgres.models_knowledge import KnowledgeBase
from yuxi.utils import logger

tenant_knowledge = APIRouter(prefix="/tenant/knowledge", tags=["tenant-knowledge"])

TENANT_OPERATOR_ID = "tenant-admin"
DEFAULT_TENANT_SHARE_CONFIG = {
    "version": 2,
    "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
    "manage_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
}


@dataclass(frozen=True, slots=True)
class TenantAdminContext:
    tenant_id: int


def require_tenant_admin(
    x_tenant_admin_key: str | None = Header(None, alias="X-Tenant-Admin-Key"),
    x_tenant_id: int | None = Header(None, alias="X-Tenant-Id"),
) -> TenantAdminContext:
    """静态管理 Key + X-Tenant-Id 鉴权依赖。"""
    from yuxi.config import config as app_config

    if not app_config.is_saas_enabled or not app_config.tenant_admin_api_key:
        raise HTTPException(status_code=404, detail="Not Found")
    if not x_tenant_admin_key or not hmac.compare_digest(x_tenant_admin_key, app_config.tenant_admin_api_key):
        raise HTTPException(status_code=401, detail="无效的管理 API Key")
    if x_tenant_id is None:
        raise HTTPException(status_code=422, detail="缺少 X-Tenant-Id 请求头")
    return TenantAdminContext(tenant_id=x_tenant_id)


async def _load_tenant_kb(kb_id: str, tenant_id: int) -> KnowledgeBase:
    """加载知识库并校验归属租户；不存在或跨租户一律 404。"""
    from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository

    kb = await KnowledgeBaseRepository().get_by_kb_id(kb_id)
    if kb is None or (kb.tenant_id or None) != tenant_id:
        raise HTTPException(status_code=404, detail=f"知识库 {kb_id} 不存在")
    return kb


async def _load_tenant_folder(kb_id: str, folder_id: str) -> None:
    """校验目录存在且属于该知识库；否则 400。"""
    from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

    record = await KnowledgeFileRepository().get_by_file_id(folder_id)
    if record is None or record.kb_id != kb_id or not record.is_folder:
        raise HTTPException(status_code=400, detail=f"目录 {folder_id} 不存在或不属于该知识库")


# =============================================================================
# === 知识库 CRUD ===
# =============================================================================


@tenant_knowledge.get("/databases")
async def list_tenant_databases(ctx: TenantAdminContext = Depends(require_tenant_admin)):
    """本租户知识库列表。"""
    from server.utils.knowledge_response import serialize_knowledge_base_list

    databases = [db for db in await knowledge_base.get_databases() if (db.tenant_id or None) == ctx.tenant_id]
    return serialize_knowledge_base_list(databases)


@tenant_knowledge.post("/databases", status_code=201)
async def create_tenant_database(
    database_name: str = Body(...),
    description: str = Body(...),
    embedding_model_spec: str | None = Body(None),
    kb_type: str = Body("milvus"),
    additional_params: dict | None = Body(None),
    llm_model_spec: str | None = Body(None),
    share_config: dict | None = Body(None),
    ctx: TenantAdminContext = Depends(require_tenant_admin),
):
    """创建本租户知识库（tenant_id 自动注入，share_config 缺省为租户内全局可读）。"""
    from server.utils.knowledge_response import serialize_knowledge_base

    logger.info(f"Tenant {ctx.tenant_id} create database: {database_name}")
    try:
        database_info = await knowledge_base.create_database(
            database_name,
            description,
            kb_type=kb_type,
            embedding_model_spec=embedding_model_spec,
            llm_model_spec=llm_model_spec,
            share_config=share_config or DEFAULT_TENANT_SHARE_CONFIG,
            created_by=f"tenant:{ctx.tenant_id}",
            tenant_id=ctx.tenant_id,
            **(additional_params or {}),
        )
        from yuxi.agents.buildin import agent_manager

        await agent_manager.reload_all()
        return serialize_knowledge_base(database_info)
    except KBNameConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@tenant_knowledge.get("/databases/{kb_id}")
async def get_tenant_database(kb_id: str, ctx: TenantAdminContext = Depends(require_tenant_admin)):
    """知识库详情。"""
    from server.utils.knowledge_response import serialize_knowledge_base

    await _load_tenant_kb(kb_id, ctx.tenant_id)
    database = await knowledge_base.get_database_info(kb_id)
    return serialize_knowledge_base(database)


@tenant_knowledge.put("/databases/{kb_id}")
async def update_tenant_database(
    kb_id: str,
    name: str = Body(...),
    description: str = Body(...),
    llm_model_spec: str | None = Body(None),
    additional_params: dict | None = Body(None),
    share_config: dict | None = Body(None),
    ctx: TenantAdminContext = Depends(require_tenant_admin),
):
    """更新知识库基本信息与 share_config（share_config 即 KB 级权限设置入口）。"""
    from server.utils.knowledge_response import serialize_knowledge_base

    await _load_tenant_kb(kb_id, ctx.tenant_id)
    update_llm_model_spec = llm_model_spec is not None
    database = await knowledge_base.update_database(
        kb_id,
        name,
        description,
        llm_model_spec,
        update_llm_model_spec=update_llm_model_spec,
        additional_params=additional_params,
        share_config=share_config,
        operator_uid=TENANT_OPERATOR_ID,
        operator_department_id=None,
    )
    return {"message": "更新成功", "database": serialize_knowledge_base(database)}


@tenant_knowledge.delete("/databases/{kb_id}")
async def delete_tenant_database(kb_id: str, ctx: TenantAdminContext = Depends(require_tenant_admin)):
    """删除知识库。"""
    await _load_tenant_kb(kb_id, ctx.tenant_id)
    await knowledge_base.delete_database(kb_id)
    return {"message": "知识库已删除"}


# =============================================================================
# === 目录管理 ===
# =============================================================================


@tenant_knowledge.post("/databases/{kb_id}/folders")
async def create_tenant_folder(
    kb_id: str,
    folder_name: str = Body(..., embed=True),
    parent_id: str | None = Body(None, embed=True),
    ctx: TenantAdminContext = Depends(require_tenant_admin),
):
    """创建目录。"""
    await _load_tenant_kb(kb_id, ctx.tenant_id)
    return await knowledge_base.create_folder(kb_id, folder_name, parent_id)


@tenant_knowledge.put("/databases/{kb_id}/folders/{folder_id}/rename")
async def rename_tenant_folder(
    kb_id: str,
    folder_id: str,
    folder_name: str = Body(..., embed=True),
    ctx: TenantAdminContext = Depends(require_tenant_admin),
):
    """重命名目录。"""
    from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

    await _load_tenant_kb(kb_id, ctx.tenant_id)
    await _load_tenant_folder(kb_id, folder_id)
    normalized_name = folder_name.strip()
    if not normalized_name:
        raise HTTPException(status_code=400, detail="目录名称不能为空")
    await KnowledgeFileRepository().update_fields(
        file_id=folder_id,
        kb_id=kb_id,
        data={"filename": normalized_name},
    )
    return {"message": "目录已重命名", "file_id": folder_id, "filename": normalized_name}


@tenant_knowledge.put("/databases/{kb_id}/folders/{folder_id}/move")
async def move_tenant_folder(
    kb_id: str,
    folder_id: str,
    new_parent_id: str | None = Body(None, embed=True),
    ctx: TenantAdminContext = Depends(require_tenant_admin),
):
    """移动目录（null 或不传 = 移到根目录）。"""
    await _load_tenant_kb(kb_id, ctx.tenant_id)
    await _load_tenant_folder(kb_id, folder_id)
    return await knowledge_base.move_file(kb_id, folder_id, new_parent_id)


@tenant_knowledge.delete("/databases/{kb_id}/folders/{folder_id}")
async def delete_tenant_folder(kb_id: str, folder_id: str, ctx: TenantAdminContext = Depends(require_tenant_admin)):
    """删除目录（仅允许删除空目录；有子目录或文档时返回 409）。"""
    from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

    await _load_tenant_kb(kb_id, ctx.tenant_id)
    await _load_tenant_folder(kb_id, folder_id)
    children = await KnowledgeFileRepository().list_children(kb_id=kb_id, parent_id=folder_id)
    if children:
        raise HTTPException(status_code=409, detail="目录不为空，无法删除（请先删除其中的子目录与文档）")
    await knowledge_base.delete_folder(kb_id, folder_id)
    return {"message": "目录已删除"}


# =============================================================================
# === 文档管理 ===
# =============================================================================


@tenant_knowledge.get("/databases/{kb_id}/documents")
async def list_tenant_documents(
    kb_id: str,
    parent_id: str | None = Query(None),
    status: str = Query("all"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    ctx: TenantAdminContext = Depends(require_tenant_admin),
):
    """分页文档列表（管理员视角全量）。"""
    await _load_tenant_kb(kb_id, ctx.tenant_id)
    return await knowledge_base.list_document_files(
        kb_id, parent_id=parent_id, status=status, page=page, page_size=page_size
    )


@tenant_knowledge.get("/databases/{kb_id}/documents/{file_id}/basic")
async def get_tenant_document_basic(kb_id: str, file_id: str, ctx: TenantAdminContext = Depends(require_tenant_admin)):
    """文档元数据（仅基本信息，不含正文）。"""
    from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

    await _load_tenant_kb(kb_id, ctx.tenant_id)
    record = await KnowledgeFileRepository().get_by_file_id(file_id)
    if record is None or record.kb_id != kb_id:
        raise HTTPException(status_code=404, detail=f"文件 {file_id} 不存在")
    return await knowledge_base.get_file_basic_info(kb_id, file_id)


@tenant_knowledge.delete("/databases/{kb_id}/documents/{file_id}")
async def delete_tenant_document(kb_id: str, file_id: str, ctx: TenantAdminContext = Depends(require_tenant_admin)):
    """删除文档或空目录。"""
    from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

    await _load_tenant_kb(kb_id, ctx.tenant_id)
    record = await KnowledgeFileRepository().get_by_file_id(file_id)
    if record is None or record.kb_id != kb_id:
        raise HTTPException(status_code=404, detail=f"文件 {file_id} 不存在")
    if record.is_folder:
        children = await KnowledgeFileRepository().list_children(kb_id=kb_id, parent_id=file_id)
        if children:
            raise HTTPException(status_code=409, detail="目录不为空，无法删除（请先删除其中的子目录与文档）")
        await knowledge_base.delete_folder(kb_id, file_id)
        return {"message": "目录已删除"}
    await knowledge_base.delete_file(kb_id, file_id)
    return {"message": "删除成功"}
