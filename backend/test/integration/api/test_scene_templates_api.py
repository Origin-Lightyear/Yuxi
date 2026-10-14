from __future__ import annotations

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_scene_templates_endpoint_returns_catalog(test_client, admin_headers):
    """场景模板端点返回按 SCENES 排序的目录与每日一技。"""
    response = await test_client.get("/api/agent/scene-templates", headers=admin_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [s["scene"] for s in body["scenes"]] == ["general", "writing", "research"]
    slugs = {t["slug"] for s in body["scenes"] for t in s["templates"]}
    assert {"default-chatbot", "deep-research", "writing-assistant"} <= slugs
    assert body["daily_tip"] is None or {"tip", "slug", "name", "agent_id"} <= set(body["daily_tip"])
