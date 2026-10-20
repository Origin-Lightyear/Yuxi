from __future__ import annotations

import os
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


def _payload(uid: str, agent_slug: str) -> dict:
    return {
        "platform": "feishu",
        "name": f"pytest-bot-{uuid.uuid4().hex[:8]}",
        "owner_uid": uid,
        "agent_slug": agent_slug,
        "credentials": {"app_id": "cli_test", "app_secret": "secret_test"},
        "allowed_users": ["ou_a", "ou_a", " ou_b "],
        "require_mention": True,
        "group_sessions_per_user": True,
    }


async def test_create_list_update_delete_im_bot(test_client, admin_headers):
    uid = await _admin_uid(test_client, admin_headers)
    slug = await _an_agent_slug(test_client, admin_headers)

    created = await test_client.post("/api/system/im-bots", json=_payload(uid, slug), headers=admin_headers)
    assert created.status_code == 200, created.text
    bot = created.json()["data"]
    assert bot["api_key_id"] is not None
    assert bot["allowed_users"] == ["ou_a", "ou_b"]
    assert "credentials_encrypted" not in bot
    bot_id = bot["id"]

    listed = await test_client.get("/api/system/im-bots", headers=admin_headers)
    assert listed.status_code == 200, listed.text
    assert any(item["id"] == bot_id for item in listed.json()["data"])

    updated = await test_client.put(
        f"/api/system/im-bots/{bot_id}",
        json={"agent_slug": slug, "enabled": False},
        headers=admin_headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["enabled"] is False
    assert updated.json()["data"]["api_key_id"] == bot["api_key_id"]

    deleted = await test_client.delete(f"/api/system/im-bots/{bot_id}", headers=admin_headers)
    assert deleted.status_code == 200, deleted.text


async def test_gateway_config_requires_token(test_client, admin_headers):
    token = os.environ.get("IM_GATEWAY_TOKEN", "")
    if not token:
        pytest.skip("IM_GATEWAY_TOKEN 未配置，跳过网关配置接口集成测试")

    uid = await _admin_uid(test_client, admin_headers)
    slug = await _an_agent_slug(test_client, admin_headers)
    created = await test_client.post("/api/system/im-bots", json=_payload(uid, slug), headers=admin_headers)
    assert created.status_code == 200, created.text
    bot_id = created.json()["data"]["id"]

    denied = await test_client.get("/api/internal/im-gateway/config", headers={"X-IM-Gateway-Token": "wrong"})
    assert denied.status_code == 401

    ok = await test_client.get("/api/internal/im-gateway/config", headers={"X-IM-Gateway-Token": token})
    assert ok.status_code == 200, ok.text
    entry = next(item for item in ok.json()["data"] if item["id"] == bot_id)
    assert entry["api_key"].startswith("yxkey_")
    assert entry["credentials"]["app_id"] == "cli_test"

    await test_client.delete(f"/api/system/im-bots/{bot_id}", headers=admin_headers)
