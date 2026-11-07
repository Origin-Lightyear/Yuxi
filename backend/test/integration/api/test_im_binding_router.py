"""IM 账号绑定用户接口与公开回调的真实 HTTP 集成测试。"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _an_agent_slug(test_client, headers) -> str:
    resp = await test_client.get("/api/agent", headers=headers)
    assert resp.status_code == 200, resp.text
    agent = resp.json()["agents"][0]
    return agent.get("slug") or agent.get("agent_id")


async def _admin_uid(test_client, headers) -> str:
    resp = await test_client.get("/api/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["uid"]


async def test_list_bindings_requires_auth(test_client):
    """未登录访问绑定列表返回 401。"""
    resp = await test_client.get("/api/user/im-bindings")
    assert resp.status_code == 401


async def test_create_session_returns_bind_url_with_state(test_client, admin_headers):
    """发起绑定会话返回 bind_url，且授权 URL 含 state=。"""
    uid = await _admin_uid(test_client, admin_headers)
    slug = await _an_agent_slug(test_client, admin_headers)

    created = await test_client.post(
        "/api/system/im-bots",
        json={
            "platform": "feishu",
            "name": f"pytest-bind-{uuid.uuid4().hex[:8]}",
            "owner_uid": uid,
            "agent_slug": slug,
            "credentials": {"app_id": "cli_test", "app_secret": "secret_test"},
        },
        headers=admin_headers,
    )
    assert created.status_code == 200, created.text
    bot_id = created.json()["data"]["id"]

    try:
        resp = await test_client.post("/api/user/im-bindings/feishu/sessions", headers=admin_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["bind_url"].startswith("https://open.feishu.cn/open-apis/authen/v1/index")
        assert "state=" in data["bind_url"]
        assert data["expires_at"]
    finally:
        await test_client.delete(f"/api/system/im-bots/{bot_id}", headers=admin_headers)


async def test_callback_with_invalid_state_returns_failure_page(test_client):
    """带错误 state 的回调返回 200 且页面含「绑定失败」。"""
    resp = await test_client.get(
        "/api/im-bindings/callback/feishu", params={"code": "whatever", "state": "not-a-real-state"}
    )
    assert resp.status_code == 200, resp.text
    assert "绑定失败" in resp.text


async def test_delete_binding_requires_auth(test_client):
    """未登录解绑返回 401；真实 Tenant 解绑由服务层集成用例覆盖。"""
    resp = await test_client.delete("/api/user/im-bindings/feishu")
    assert resp.status_code == 401
