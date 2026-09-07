"""Tenant 管理 API 集成测试：鉴权、KB CRUD、目录（重命名/移动/仅空目录删除）、文档元数据与跨租户隔离。

环境说明：本环境未注册 embedding 模型，知识库行通过 asyncpg 直插
（绕过模型校验，专注路由层的租户隔离与目录/文档语义）；创建端点本身用
400（模型校验失败）断言鉴权链路已放行。
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

DEFAULT_SHARE_CONFIG = {
    "version": 2,
    "read_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
    "manage_scope": {"access_level": "global", "department_ids": [], "user_uids": []},
}


@pytest.fixture
def tenant_api():
    """管理 API 请求头：密钥与 api 服务进程共用 TENANT_ADMIN_API_KEY 环境变量。"""
    key = os.environ.get("TENANT_ADMIN_API_KEY", "")
    if not key:
        pytest.skip("TENANT_ADMIN_API_KEY 未配置，跳过租户管理 API 集成测试")
    return {"X-Tenant-Admin-Key": key, "X-Tenant-Id": "100"}


async def _db_conn():
    """每测试独立 asyncpg 连接（不绑定 pg_manager 引擎，避开跨事件循环问题）。"""
    import asyncpg

    from yuxi.storage.postgres.manager import pg_manager

    dsn = os.environ[pg_manager.KB_DATABASE_URL_ENV].replace("postgresql+asyncpg://", "postgresql://")
    return await asyncpg.connect(dsn)


async def _create_tenant_kb_row(tenant_id: int = 100, name: str | None = None) -> str:
    conn = await _db_conn()
    try:
        kb_id = f"kb_{uuid.uuid4().hex[:10]}"
        await conn.execute(
            """
            INSERT INTO knowledge_bases (kb_id, name, description, kb_type, embedding_model_spec,
                                         llm_model_spec, query_params, additional_params, share_config,
                                         created_by, tenant_id)
            VALUES ($1, $2, $3, 'milvus', NULL, NULL, $4, $5, $6, $7, $8)
            """,
            kb_id,
            name or f"pytest_tenant_{uuid.uuid4().hex[:8]}",
            "tenant kb",
            json.dumps({}),
            json.dumps({"stats": {}}),
            json.dumps(DEFAULT_SHARE_CONFIG),
            f"tenant:{tenant_id}",
            tenant_id,
        )
        return kb_id
    finally:
        await conn.close()


async def test_missing_key_returns_401(test_client, tenant_api):
    response = await test_client.get(
        "/api/tenant/knowledge/databases",
        headers={"X-Tenant-Admin-Key": "wrong", "X-Tenant-Id": "100"},
    )
    assert response.status_code == 401


async def test_missing_tenant_id_returns_422(test_client, tenant_api):
    response = await test_client.get(
        "/api/tenant/knowledge/databases",
        headers={"X-Tenant-Admin-Key": tenant_api["X-Tenant-Admin-Key"]},
    )
    assert response.status_code == 422


async def test_create_endpoint_reaches_validation(test_client, tenant_api):
    """未注册 embedding 模型时创建返回 400，证明鉴权与租户注入已放行。"""
    response = await test_client.post(
        "/api/tenant/knowledge/databases",
        json={
            "database_name": f"pytest_tenant_{uuid.uuid4().hex[:8]}",
            "description": "tenant kb",
            "embedding_model_spec": "unknown-provider:unknown-model",
            "kb_type": "milvus",
        },
        headers=tenant_api,
    )
    assert response.status_code == 400


async def test_kb_crud_with_tenant_isolation(test_client, tenant_api):
    kb_id = await _create_tenant_kb_row(tenant_id=100)

    list_response = await test_client.get("/api/tenant/knowledge/databases", headers=tenant_api)
    assert list_response.status_code == 200
    assert any(item["kb_id"] == kb_id for item in list_response.json()["databases"])
    assert any(item["tenant_id"] == 100 for item in list_response.json()["databases"])

    # 跨租户访问 404（不暴露存在性）
    other_headers = {**tenant_api, "X-Tenant-Id": "999"}
    assert (await test_client.get(f"/api/tenant/knowledge/databases/{kb_id}", headers=other_headers)).status_code == 404
    assert (
        await test_client.delete(f"/api/tenant/knowledge/databases/{kb_id}", headers=other_headers)
    ).status_code == 404

    detail_response = await test_client.get(f"/api/tenant/knowledge/databases/{kb_id}", headers=tenant_api)
    assert detail_response.status_code == 200
    assert detail_response.json()["tenant_id"] == 100

    delete_response = await test_client.delete(f"/api/tenant/knowledge/databases/{kb_id}", headers=tenant_api)
    assert delete_response.status_code == 200
    assert (await test_client.get(f"/api/tenant/knowledge/databases/{kb_id}", headers=tenant_api)).status_code == 404


async def test_folder_rename_move_and_empty_only_delete(test_client, tenant_api):
    kb_id = await _create_tenant_kb_row(tenant_id=100)

    # 创建目录
    parent_response = await test_client.post(
        f"/api/tenant/knowledge/databases/{kb_id}/folders",
        json={"folder_name": "原始名称", "parent_id": None},
        headers=tenant_api,
    )
    assert parent_response.status_code == 200, parent_response.text
    parent_id = parent_response.json()["file_id"]

    # 重命名
    rename_response = await test_client.put(
        f"/api/tenant/knowledge/databases/{kb_id}/folders/{parent_id}/rename",
        json={"folder_name": "新名称"},
        headers=tenant_api,
    )
    assert rename_response.status_code == 200, rename_response.text
    assert rename_response.json()["filename"] == "新名称"

    # 目录下放一个子目录后：非空目录删除 409
    child_response = await test_client.post(
        f"/api/tenant/knowledge/databases/{kb_id}/folders",
        json={"folder_name": "子目录", "parent_id": parent_id},
        headers=tenant_api,
    )
    child_id = child_response.json()["file_id"]
    non_empty_delete = await test_client.delete(
        f"/api/tenant/knowledge/databases/{kb_id}/folders/{parent_id}", headers=tenant_api
    )
    assert non_empty_delete.status_code == 409

    # 移走子目录后：空目录删除成功
    move_response = await test_client.put(
        f"/api/tenant/knowledge/databases/{kb_id}/folders/{child_id}/move",
        json={"new_parent_id": None},
        headers=tenant_api,
    )
    assert move_response.status_code == 200, move_response.text
    empty_delete = await test_client.delete(
        f"/api/tenant/knowledge/databases/{kb_id}/folders/{parent_id}", headers=tenant_api
    )
    assert empty_delete.status_code == 200

    # 清理：删除子目录与知识库
    await test_client.delete(f"/api/tenant/knowledge/databases/{kb_id}/folders/{child_id}", headers=tenant_api)
    await test_client.delete(f"/api/tenant/knowledge/databases/{kb_id}", headers=tenant_api)


async def test_documents_list_basic_and_delete(test_client, tenant_api):
    kb_id = await _create_tenant_kb_row(tenant_id=100)

    list_response = await test_client.get(f"/api/tenant/knowledge/databases/{kb_id}/documents", headers=tenant_api)
    assert list_response.status_code == 200, list_response.text
    assert list_response.json()["total"] == 0

    # 不存在的文件 basic 404
    missing_basic = await test_client.get(
        f"/api/tenant/knowledge/databases/{kb_id}/documents/no-such-file/basic", headers=tenant_api
    )
    assert missing_basic.status_code == 404

    # 不存在的文件删除 404
    missing_delete = await test_client.delete(
        f"/api/tenant/knowledge/databases/{kb_id}/documents/no-such-file", headers=tenant_api
    )
    assert missing_delete.status_code == 404

    await test_client.delete(f"/api/tenant/knowledge/databases/{kb_id}", headers=tenant_api)


async def test_removed_pipeline_endpoints_return_404(test_client, tenant_api):
    """上传/建档/解析/入库/下载/目录权限配置已从管理 API 移除。

    说明：`POST documents/add|parse|index` 路径与 `DELETE documents/{file_id}` 的
    `{file_id}` 段碰撞，FastAPI 返回 405（方法不允许）；其余路径无路由返回 404。
    """
    kb_id = await _create_tenant_kb_row(tenant_id=100)
    probes = [
        ("GET", f"/api/tenant/knowledge/databases/{kb_id}/documents/file-1/download", 404),
        ("POST", f"/api/tenant/knowledge/databases/{kb_id}/documents/add", 405),
        ("POST", f"/api/tenant/knowledge/databases/{kb_id}/documents/parse", 405),
        ("POST", f"/api/tenant/knowledge/databases/{kb_id}/documents/index", 405),
        ("GET", f"/api/tenant/knowledge/databases/{kb_id}/folder-permissions", 404),
        ("PUT", f"/api/tenant/knowledge/databases/{kb_id}/folder-permissions", 404),
    ]
    for method, path, expected in probes:
        response = await test_client.request(method, path, headers=tenant_api)
        assert response.status_code == expected, f"{method} {path} 应 {expected}，实际 {response.status_code}"

    await test_client.delete(f"/api/tenant/knowledge/databases/{kb_id}", headers=tenant_api)
