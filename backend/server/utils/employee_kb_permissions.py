"""SaaS 员工的知识库权限依赖（KB 级 share_config 语义）。

员工边界（与需求对齐）：
- 检索/查看：KB 级 READ（read_scope 命中）+ 本租户；
- 上传/删除/移动文档：KB 级 MANAGE（manage_scope 命中）+ 本租户；
- 目录（文件夹）增删改、KB CRUD、share_config 修改：永远拒绝员工（管理员门槛）。

非 SaaS 的本地用户不走这些依赖；管理员路径由 require_knowledge_base_read/manage 覆盖。
"""

from fastapi import Depends, HTTPException

from server.utils.auth_middleware import get_required_user
from server.utils.knowledge_permissions import ensure_knowledge_base_permission
from yuxi.knowledge.runtime import knowledge_base
from yuxi.permissions import ResourcePermission, ResourcePermissionDenied, require_knowledge_base_permission
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import User

ADMIN_ROLES = ("admin", "superadmin")


def is_employee(current_user: User) -> bool:
    """本地普通角色（role=user）走员工权限路径。"""
    return current_user.role not in ADMIN_ROLES


async def ensure_employee_kb(kb_id: str, current_user: User):
    """校验 SaaS 员工身份 + KB 级 READ + 本租户归属；返回知识库信息。"""
    from yuxi.services.saas_identity import get_saas_employee_context

    db_info = await ensure_knowledge_base_permission(kb_id, current_user, ResourcePermission.READ)
    async with pg_manager.get_async_session_context() as session:
        saas_ctx = await get_saas_employee_context(session, current_user.uid)
    if saas_ctx is None:
        raise HTTPException(status_code=403, detail="当前用户不是 SaaS 员工")
    if (db_info.tenant_id or None) != saas_ctx.tenant_id:
        # 跨租户访问不暴露资源存在性
        raise HTTPException(status_code=404, detail=f"知识库 {kb_id} 不存在")
    return db_info


async def require_kb_read_or_employee(kb_id: str, current_user: User = Depends(get_required_user)) -> User:
    """管理员或 SaaS 员工均可通过的 KB 读取依赖（原 require_knowledge_base_read 的放开版）。"""
    if current_user.role in ADMIN_ROLES:
        await ensure_knowledge_base_permission(kb_id, current_user, ResourcePermission.READ)
        return current_user
    await ensure_employee_kb(kb_id, current_user)
    return current_user


async def _require_kb_document_manage(kb_id: str, current_user: User) -> None:
    """KB 级 MANAGE（管理员或 manage_scope 命中的员工）+ 本租户校验；否则 403。"""
    db_info = await knowledge_base.get_database_info(kb_id)
    if not db_info:
        raise HTTPException(status_code=404, detail=f"知识库 {kb_id} 不存在")
    try:
        require_knowledge_base_permission(current_user, db_info, ResourcePermission.MANAGE)
    except ResourcePermissionDenied:
        raise HTTPException(status_code=403, detail="无权编辑该知识库的文档") from None
    if is_employee(current_user):
        await ensure_employee_kb(kb_id, current_user)


async def require_kb_manage_for_documents(kb_id: str, current_user: User) -> None:
    """文档建档（add）的权限：KB 级 MANAGE（员工为 manage_scope 命中的文档级管理）。"""
    await _require_kb_document_manage(kb_id, current_user)


async def require_documents_edit_or_kb_manage(
    kb_id: str,
    file_ids: list[str],
    current_user: User,
) -> None:
    """文档批量操作的权限：KB 级 MANAGE；文件夹目标一律拒绝员工。"""
    from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

    await _require_kb_document_manage(kb_id, current_user)
    if current_user.role in ADMIN_ROLES:
        return

    records = {str(record.file_id): record for record in await KnowledgeFileRepository().list_by_file_ids(file_ids)}
    for file_id in file_ids:
        record = records.get(str(file_id))
        if record is None or record.kb_id != kb_id:
            raise HTTPException(status_code=404, detail=f"文件 {file_id} 不存在")
        if record.is_folder:
            raise HTTPException(status_code=403, detail="无权修改或删除目录")
