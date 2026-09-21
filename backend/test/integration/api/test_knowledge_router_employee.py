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
import time
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
    from yuxi.storage.redis import create_sync_redis_client

    if not os.environ.get("JWT_SECRET_KEY"):
        pytest.skip("JWT_SECRET_KEY 未配置，跳过员工端点集成测试")

    conn = await _db_conn()
    suffix = uuid.uuid4().hex[:8]
    uid = f"pytest_emp_{suffix}"
    kb_manage_id = f"kb_{uuid.uuid4().hex[:10]}"
    kb_readonly_id = f"kb_{uuid.uuid4().hex[:10]}"
    kb_foreign_id = f"kb_{uuid.uuid4().hex[:10]}"
    kb_hidden_id = f"kb_{uuid.uuid4().hex[:10]}"
    redis = None
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
            (kb_hidden_id, _share_config({"access_level": "user", "user_uids": []}, None)),
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
        doc_id = await conn.fetchval(
            "INSERT INTO knowledge_files (file_id, kb_id, filename, is_folder, status) "
            "VALUES ($1, $2, '员工文档.txt', FALSE, 'uploaded') RETURNING file_id",
            f"file_{uuid.uuid4().hex[:10]}",
            kb_manage_id,
        )

        access_token = AuthUtils.create_access_token({"sub": str(user_id)})
        # 真实认证还检查 Redis 会话；与 JWT 一起准备，避免所有权限断言退化为 401。
        redis = create_sync_redis_client()
        redis.set(
            f"saas:agent-session:{uid}",
            json.dumps(
                {
                    "token": "pytest-session",
                    "expires_at": str(time.time() + 300),
                    "tenant_id": 100,
                    "employee_id": 200,
                    "mcp_instance_id": "pytest",
                }
            ),
            ex=300,
        )
        yield (
            {"Authorization": f"Bearer {access_token}"},
            {
                "uid": uid,
                "manage": kb_manage_id,
                "readonly": kb_readonly_id,
                "foreign": kb_foreign_id,
                "hidden": kb_hidden_id,
                "folder": folder_id,
                "document": doc_id,
            },
        )
    finally:
        try:
            if redis is not None:
                try:
                    redis.delete(f"saas:agent-session:{uid}")
                finally:
                    redis.close()
        finally:
            try:
                for target_kb_id in (kb_manage_id, kb_readonly_id, kb_foreign_id, kb_hidden_id):
                    await conn.execute("DELETE FROM knowledge_files WHERE kb_id = $1", target_kb_id)
                    await conn.execute("DELETE FROM knowledge_bases WHERE kb_id = $1", target_kb_id)
                await conn.execute("DELETE FROM user_config WHERE uid = $1", uid)
                await conn.execute("DELETE FROM users WHERE uid = $1", uid)
                await conn.execute("DELETE FROM departments WHERE id = $1", dept_id)
            finally:
                await conn.close()


async def test_accessible_databases_filtered_by_tenant(test_client, employee_env):
    headers, kbs = employee_env
    response = await test_client.get("/api/knowledge/databases/accessible", headers=headers)
    assert response.status_code == 200, response.text
    visible_ids = {item["kb_id"] for item in response.json()["databases"]}
    assert kbs["manage"] in visible_ids
    assert kbs["readonly"] in visible_ids
    assert kbs["foreign"] not in visible_ids
    assert kbs["hidden"] not in visible_ids
    databases = {item["kb_id"]: item for item in response.json()["databases"]}
    assert databases[kbs["manage"]]["can_manage"] is True
    assert databases[kbs["readonly"]]["can_manage"] is False
    assert databases[kbs["readonly"]]["can_read"] is True
    assert databases[kbs["manage"]]["tenant_id"] == 100
    assert "stats" in databases[kbs["manage"]]


async def test_employee_opens_and_moves_document_but_not_folder(test_client, employee_env):
    """员工文档管理必须能完成移动并从目录中读取实际结果。"""
    headers, kbs = employee_env
    base = f"/api/knowledge/databases/{kbs['manage']}"
    basic = await test_client.get(f"{base}/documents/{kbs['document']}/basic", headers=headers)
    assert basic.status_code == 200, basic.text
    assert basic.json()["meta"]["filename"] == "员工文档.txt"
    moved = await test_client.put(
        f"{base}/documents/{kbs['document']}/move",
        json={"new_parent_id": kbs["folder"]},
        headers=headers,
    )
    assert moved.status_code == 200, moved.text
    listed = await test_client.get(f"{base}/documents", params={"parent_id": kbs["folder"]}, headers=headers)
    assert [item["file_id"] for item in listed.json()["items"]] == [kbs["document"]]
    deleted = await test_client.delete(f"{base}/documents/{kbs['document']}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    listed = await test_client.get(f"{base}/documents", params={"parent_id": kbs["folder"]}, headers=headers)
    assert listed.json()["items"] == []


@pytest.mark.parametrize("scope", [GLOBAL_SCOPE, {"access_level": "user", "user_uids": []}])
async def test_cross_tenant_is_hidden_before_scope_check(test_client, employee_env, scope):
    """跨租户的读取和文档写入统一 404，不通过 403 暴露存在性。"""
    headers, kbs = employee_env
    conn = await _db_conn()
    try:
        await conn.execute(
            "UPDATE knowledge_bases SET share_config = $1 WHERE kb_id = $2",
            json.dumps(_share_config(scope, scope)),
            kbs["foreign"],
        )
    finally:
        await conn.close()
    base = f"/api/knowledge/databases/{kbs['foreign']}"
    for method, suffix, payload in [
        ("GET", "", None),
        ("GET", "/documents/no-file/basic", None),
        ("DELETE", "/documents/no-file", None),
        ("PUT", "/documents/no-file/move", {"new_parent_id": None}),
    ]:
        response = await test_client.request(method, base + suffix, json=payload, headers=headers)
        assert response.status_code == 404, (method, suffix, response.text)


async def test_employee_upload_and_kb_structure_permissions(test_client, employee_env):
    """只读员工不能上传、移动文档，文档管理员也不能修改知识库结构。"""
    headers, kbs = employee_env
    denied = await test_client.post(
        "/api/knowledge/files/upload",
        params={"kb_id": kbs["readonly"]},
        files={"file": ("denied.txt", b"denied", "text/plain")},
        headers=headers,
    )
    assert denied.status_code == 403
    moved = await test_client.put(
        f"/api/knowledge/databases/{kbs['readonly']}/documents/no-file/move",
        json={"new_parent_id": None},
        headers=headers,
    )
    assert moved.status_code == 403
    for method, path, payload in [
        ("POST", "/api/knowledge/databases", {"database_name": "forbidden", "description": "", "kb_type": "milvus"}),
        ("PUT", f"/api/knowledge/databases/{kbs['manage']}", {"name": "forbidden"}),
        ("DELETE", f"/api/knowledge/databases/{kbs['manage']}", None),
    ]:
        response = await test_client.request(method, path, json=payload, headers=headers)
        assert response.status_code == 403, response.text


async def test_employee_upload_download_and_delete_document(test_client, employee_env):
    """真实上传、建档、下载和删除验证员工文档管理闭环。"""
    headers, kbs = employee_env
    content = f"employee document {uuid.uuid4().hex}".encode()
    uploaded = await test_client.post(
        "/api/knowledge/files/upload",
        params={"kb_id": kbs["manage"]},
        files={"file": ("employee.txt", content, "text/plain")},
        headers=headers,
    )
    assert uploaded.status_code == 200, uploaded.text
    item = uploaded.json()
    invalid_parent = await test_client.post(
        f"/api/knowledge/databases/{kbs['manage']}/documents/add",
        json={
            "items": [item["file_path"]],
            "params": {
                "content_type": "file",
                "content_hashes": {item["file_path"]: item["content_hash"]},
                "parent_id": "foreign-or-missing-folder",
            },
        },
        headers=headers,
    )
    assert invalid_parent.status_code == 404, invalid_parent.text
    added = await test_client.post(
        f"/api/knowledge/databases/{kbs['manage']}/documents/add",
        json={
            "items": [item["file_path"]],
            "params": {
                "content_type": "file",
                "content_hashes": {item["file_path"]: item["content_hash"]},
                "parent_id": kbs["folder"],
            },
        },
        headers=headers,
    )
    assert added.status_code == 200, added.text
    assert added.json()["status"] == "success", added.text
    files = await test_client.get(
        f"/api/knowledge/databases/{kbs['manage']}/documents", params={"parent_id": kbs["folder"]}, headers=headers
    )
    doc_id = files.json()["items"][0]["file_id"]
    base = f"/api/knowledge/databases/{kbs['manage']}/documents/{doc_id}"
    downloaded = await test_client.get(f"{base}/download", headers=headers)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == content
    deleted = await test_client.delete(base, headers=headers)
    assert deleted.status_code == 200, deleted.text


async def test_local_admin_still_manages_kb_and_folders(test_client, employee_env):
    """临时本地管理员通过真实 HTTP 保留知识库与目录管理能力。"""
    headers, kbs = employee_env
    conn = await _db_conn()
    try:
        await conn.execute("DELETE FROM user_config WHERE uid = $1", kbs["uid"])
        await conn.execute("UPDATE users SET role = 'admin' WHERE uid = $1", kbs["uid"])
        response = await test_client.post(
            "/api/knowledge/databases",
            json={
                "database_name": f"pytest_local_{uuid.uuid4().hex[:8]}",
                "description": "local regression",
                "kb_type": "milvus",
            },
            headers=headers,
        )
        assert response.status_code == 200, response.text
        kb_id = response.json()["kb_id"]
        try:
            created = await test_client.post(
                f"/api/knowledge/databases/{kb_id}/folders", json={"folder_name": "目录"}, headers=headers
            )
            assert created.status_code == 200, created.text
            folder_id = created.json()["file_id"]
            moved = await test_client.put(
                f"/api/knowledge/databases/{kb_id}/documents/{folder_id}/move",
                json={"new_parent_id": None},
                headers=headers,
            )
            assert moved.status_code == 200, moved.text
            deleted = await test_client.delete(
                f"/api/knowledge/databases/{kb_id}/documents/{folder_id}", headers=headers
            )
            assert deleted.status_code == 200, deleted.text
        finally:
            deleted_kb = await test_client.delete(f"/api/knowledge/databases/{kb_id}", headers=headers)
            assert deleted_kb.status_code == 200, deleted_kb.text
    finally:
        await conn.close()


async def test_employee_kb_detail_and_cross_tenant_404(test_client, employee_env):
    headers, kbs = employee_env

    response = await test_client.get(f"/api/knowledge/databases/{kbs['manage']}", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["effective_permission"] == "manage"

    readonly = await test_client.get(f"/api/knowledge/databases/{kbs['readonly']}", headers=headers)
    assert readonly.json()["effective_permission"] == "read"

    cross_tenant = await test_client.get(f"/api/knowledge/databases/{kbs['foreign']}", headers=headers)
    assert cross_tenant.status_code == 404


@pytest.mark.parametrize("suffix", ["", "/basic", "/content", "/download"])
async def test_document_from_another_kb_returns_404(test_client, employee_env, suffix):
    """即使两个库均可读，也不能借另一库的 URL 打开文档。"""
    headers, kbs = employee_env
    response = await test_client.get(
        f"/api/knowledge/databases/{kbs['readonly']}/documents/{kbs['document']}{suffix}", headers=headers
    )
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("endpoint", ["documents", "documents/add"])
@pytest.mark.parametrize("source", ["foreign", "readonly", "bucket", "override"])
async def test_ingest_rejects_objects_outside_target_kb(test_client, employee_env, endpoint, source):
    """客户端提供 hash 不能授权跨库对象或预处理路径覆盖。"""
    headers, kbs = employee_env
    item = f"minio://knowledgebases/{kbs['manage']}/upload/test.txt"
    source_path = (
        f"minio://knowledgebases/{kbs['foreign' if source == 'override' else source]}/upload/test.txt"
        if source in {"foreign", "readonly", "override"}
        else f"minio://chat-attachments/{kbs['manage']}/upload/test.txt"
    )
    if source != "override":
        item = source_path
    params = {"content_type": "file", "content_hashes": {item: "fake-hash"}, "file_sizes": {item: 1}}
    if source == "override":
        params["_preprocessed_map"] = {item: {"path": source_path, "content_hash": "fake-hash", "file_size": 1}}
    response = await test_client.post(
        f"/api/knowledge/databases/{kbs['manage']}/{endpoint}",
        json={"items": [item], "params": params},
        headers=headers,
    )
    assert response.status_code == 403, response.text
    conn = await _db_conn()
    try:
        assert await conn.fetchval("SELECT COUNT(*) FROM knowledge_files WHERE kb_id = $1", kbs["manage"]) == 2
    finally:
        await conn.close()


@pytest.mark.parametrize(
    "method,suffix,payload",
    [
        ("POST", "documents/add", {"items": [], "params": {}}),
        ("POST", "documents/parse", ["file"]),
        ("POST", "documents/index", {"file_ids": ["file"]}),
        ("DELETE", "documents/file", None),
        ("DELETE", "documents/batch", ["file"]),
    ],
)
async def test_foreign_connector_authorization_precedes_type_check(test_client, employee_env, method, suffix, payload):
    """外租户只读连接器不得通过文档写接口暴露名称或类型。"""
    headers, kbs = employee_env
    conn = await _db_conn()
    try:
        await conn.execute(
            "UPDATE knowledge_bases SET kb_type = 'dify', additional_params = $2 WHERE kb_id = $1",
            kbs["foreign"],
            json.dumps({"dify_api_url": "https://example.com/v1", "dify_token": "test", "dify_dataset_id": "test"}),
        )
    finally:
        await conn.close()
    response = await test_client.request(
        method, f"/api/knowledge/databases/{kbs['foreign']}/{suffix}", json=payload, headers=headers
    )
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("source_kb", ["foreign", "readonly"])
async def test_rejected_ingest_preserves_real_source_object(test_client, employee_env, source_kb):
    """跨租户和只读库真实对象不得被重新建档，且源字节保持不变。"""
    from yuxi.storage.minio.client import get_minio_client

    headers, kbs = employee_env
    storage = get_minio_client()
    object_name = f"{kbs[source_kb]}/upload/round1-{uuid.uuid4().hex}.txt"
    source = f"minio://knowledgebases/{object_name}"
    content = b"protected source object"
    storage.upload_file("knowledgebases", object_name, content)
    try:
        for endpoint in ("documents", "documents/add"):
            for override in (False, True):
                item = f"minio://knowledgebases/{kbs['manage']}/upload/valid.txt" if override else source
                params = {"content_type": "file", "content_hashes": {item: "untrusted-hash"}}
                if override:
                    params["_preprocessed_map"] = {item: {"path": source, "content_hash": "untrusted-hash"}}
                response = await test_client.post(
                    f"/api/knowledge/databases/{kbs['manage']}/{endpoint}",
                    json={"items": [item], "params": params},
                    headers=headers,
                )
                assert response.status_code == 403, response.text
        assert storage.download_file("knowledgebases", object_name) == content
    finally:
        storage.delete_file("knowledgebases", object_name)


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

    # 上传弹窗当前使用的解析入口也必须接受文档管理员。
    queued = await test_client.post(
        f"/api/knowledge/databases/{kbs['manage']}/documents",
        json={"items": [], "params": {"content_type": "file"}},
        headers=headers,
    )
    assert queued.status_code == 400, queued.text


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
