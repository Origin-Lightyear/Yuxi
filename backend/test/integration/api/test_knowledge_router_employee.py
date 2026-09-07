"""SaaS 员工知识库端点集成测试：KB 级 share_config 语义、员工 MANAGE 边界、租户隔离。

权限模型（回归 share_config）：
- read_scope 命中 → 可检索/查看全库；
- manage_scope 命中 → 员工 MANAGE = 文档级管理（上传/删除文档），目录改删与 KB 结构操作仍 403；
- 空 user_uids 表达无权限。

环境要求：JWT_SECRET_KEY 与 YUXI_INSTANCE_ID 需为稳定值（与 api 服务进程一致）
才能铸发员工令牌；未配置时跳过。数据通过独立 asyncpg 连接直插。
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

GLOBAL_SCOPE = {"access_level": "global", "department_ids": [], "user_uids": []}


def _share_config(read_scope, manage_scope):
    return {"version": 2, "read_scope": read_scope, "manage_scope": manage_scope}


async def _db_conn():
    import asyncpg

    from yuxi.storage.postgres.manager import pg_manager

    dsn = os.environ[pg_manager.KB_DATABASE_URL_ENV].replace("postgresql+asyncpg://", "postgresql://")
    return await asyncpg.connect(dsn)


@pytest.fixture
async def employee_env():
    """准备员工用户与本租户两个知识库（可管理/只读）+ 他租户知识库；返回上下文。"""
    from yuxi.utils.auth_utils import AuthUtils

    if not os.environ.get("JWT_SECRET_KEY"):
        pytest.skip("JWT_SECRET_KEY 未配置，跳过员工端点集成测试")

    conn = await _db_conn()
    suffix = uuid.uuid4().hex[:8]
    uid = f"pytest_emp_{suffix}"
    kb_manage_id = f"kb_{uuid.uuid4().hex[:10]}"
    kb_readonly_id = f"kb_{uuid.uuid4().hex[:10]}"
    kb_foreign_id = f"kb_{uuid.uuid4().hex[:10]}"
    try:
        dept_id = await conn.fetchval(
            "INSERT INTO departments (name, description) VALUES ($1, $2) RETURNING id",
            f"pytest_dept_{suffix}",
            "employee test dept",
        )
        user_id = await conn.fetchval(
            "INSERT INTO users (username, uid, password_hash, role, department_id, login_failed_count, is_deleted) "
            "VALUES ($1, $2, $3, 'user', $4, 0, 0) RETURNING id",
            f"员工{suffix}",
            uid,
            AuthUtils.hash_password(f"Pw!{suffix}"),
            dept_id,
        )
        await conn.execute(
            "INSERT INTO user_config (uid, enable_memory, tenant_id, employee_id) VALUES ($1, FALSE, 100, 200)",
            uid,
        )
        kb_configs = [
            # 员工命中 manage_scope（user 列表含 uid）→ 文档级管理
            (
                kb_manage_id,
                _share_config(GLOBAL_SCOPE, {"access_level": "user", "department_ids": [], "user_uids": [uid]}),
            ),
            # 员工只读：manage 空 user 列表 = 无员工管理权
            (
                kb_readonly_id,
                _share_config(GLOBAL_SCOPE, {"access_level": "user", "department_ids": [], "user_uids": []}),
            ),
            # 他租户知识库（tenant 999）
            (kb_foreign_id, _share_config(GLOBAL_SCOPE, GLOBAL_SCOPE)),
        ]
        for target_kb_id, share_config in kb_configs:
            tenant_id = 999 if target_kb_id == kb_foreign_id else 100
            await conn.execute(
                """
                INSERT INTO knowledge_bases (kb_id, name, description, kb_type, query_params,
                                             additional_params, share_config, created_by, tenant_id)
                VALUES ($1, $2, $3, 'milvus', '{}', $4, $5, $6, $7)
                """,
                target_kb_id,
                f"pytest_kb_{uuid.uuid4().hex[:6]}",
                "employee kb",
                json.dumps({"stats": {}}),
                json.dumps(share_config),
                f"tenant:{tenant_id}",
                tenant_id,
            )
        # 可管理知识库下建一个目录（员工对目录结构无权）
        folder_id = await conn.fetchval(
            "INSERT INTO knowledge_files (file_id, kb_id, parent_id, filename, is_folder, status) "
            "VALUES ($1, $2, NULL, '现有目录', TRUE, 'done') RETURNING file_id",
            f"fld_{uuid.uuid4().hex[:10]}",
            kb_manage_id,
        )

        access_token = AuthUtils.create_access_token({"sub": str(user_id)})
        yield (
            {"Authorization": f"Bearer {access_token}"},
            {
                "uid": uid,
                "manage": kb_manage_id,
                "readonly": kb_readonly_id,
                "foreign": kb_foreign_id,
                "folder": folder_id,
            },
        )
    finally:
        for target_kb_id in (kb_manage_id, kb_readonly_id, kb_foreign_id):
            await conn.execute("DELETE FROM knowledge_files WHERE kb_id = $1", target_kb_id)
            await conn.execute("DELETE FROM knowledge_bases WHERE kb_id = $1", target_kb_id)
        await conn.execute("DELETE FROM user_config WHERE uid = $1", uid)
        await conn.execute("DELETE FROM users WHERE uid = $1", uid)
        await conn.execute("DELETE FROM departments WHERE id = $1", dept_id)
        await conn.close()


async def test_accessible_databases_filtered_by_tenant(test_client, employee_env):
    headers, kbs = employee_env
    response = await test_client.get("/api/knowledge/databases/accessible", headers=headers)
    assert response.status_code == 200, response.text
    visible_ids = {item["kb_id"] for item in response.json()["databases"]}
    assert kbs["manage"] in visible_ids
    assert kbs["readonly"] in visible_ids
    assert kbs["foreign"] not in visible_ids


async def test_employee_kb_detail_and_cross_tenant_404(test_client, employee_env):
    headers, kbs = employee_env

    response = await test_client.get(f"/api/knowledge/databases/{kbs['manage']}", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["effective_permission"] == "manage"

    readonly = await test_client.get(f"/api/knowledge/databases/{kbs['readonly']}", headers=headers)
    assert readonly.json()["effective_permission"] == "read"

    cross_tenant = await test_client.get(f"/api/knowledge/databases/{kbs['foreign']}", headers=headers)
    assert cross_tenant.status_code == 404


async def test_employee_cannot_create_or_delete_folders_even_with_manage(test_client, employee_env):
    headers, kbs = employee_env

    create_response = await test_client.post(
        f"/api/knowledge/databases/{kbs['manage']}/folders",
        json={"folder_name": "越权目录", "parent_id": None},
        headers=headers,
    )
    assert create_response.status_code == 403

    delete_folder = await test_client.delete(
        f"/api/knowledge/databases/{kbs['manage']}/documents/{kbs['folder']}", headers=headers
    )
    assert delete_folder.status_code == 403

    move_response = await test_client.put(
        f"/api/knowledge/databases/{kbs['manage']}/documents/{kbs['folder']}/move",
        json={"new_parent_id": None},
        headers=headers,
    )
    assert move_response.status_code == 403


async def test_employee_add_document_requires_kb_manage(test_client, employee_env):
    headers, kbs = employee_env

    # 只读知识库：403
    denied = await test_client.post(
        f"/api/knowledge/databases/{kbs['readonly']}/documents/add",
        json={
            "items": ["minio://documents/fake/upload/fake.pdf"],
            "params": {"content_type": "file"},
        },
        headers=headers,
    )
    assert denied.status_code == 403

    # 可管理知识库：权限放行（后续因 content_hash 缺失 400，但已过权限闸门）
    allowed = await test_client.post(
        f"/api/knowledge/databases/{kbs['manage']}/documents/add",
        json={
            "items": ["minio://documents/fake/upload/fake.pdf"],
            "params": {"content_type": "file"},
        },
        headers=headers,
    )
    assert allowed.status_code == 400


async def test_employee_delete_document_requires_kb_manage(test_client, employee_env):
    headers, kbs = employee_env

    missing = await test_client.delete(
        f"/api/knowledge/databases/{kbs['readonly']}/documents/no-such-file", headers=headers
    )
    # 只读知识库：权限不足优先于资源存在性
    assert missing.status_code == 403

    batch_delete_folder = await test_client.request(
        "DELETE",
        f"/api/knowledge/databases/{kbs['manage']}/documents/batch",
        json=[kbs["folder"]],
        headers=headers,
    )
    # 文件夹目标一律 403
    assert batch_delete_folder.status_code == 403


async def test_employee_search_and_list_unfiltered_within_read_scope(test_client, employee_env):
    headers, kbs = employee_env

    list_response = await test_client.get(f"/api/knowledge/databases/{kbs['manage']}/documents", headers=headers)
    assert list_response.status_code == 200, list_response.text
    items = {item["file_id"] for item in list_response.json()["items"]}
    assert kbs["folder"] in items

    query_response = await test_client.post(
        f"/api/knowledge/databases/{kbs['manage']}/query",
        json={"query": "测试", "meta": {}},
        headers=headers,
    )
    assert query_response.status_code == 200
