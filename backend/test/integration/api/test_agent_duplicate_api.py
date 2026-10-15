from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.agent_repository import AgentRepository

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_duplicate_template_creates_private_copy_for_current_user(test_client, admin_headers):
    """复制内置模板产出仅当前用户可见的私有副本，且副本不再是模板。"""
    me = await test_client.get("/api/auth/me", headers=admin_headers)
    assert me.status_code == 200, me.text
    uid = str(me.json()["uid"])

    detail = await test_client.get("/api/agent/default", headers=admin_headers)
    assert detail.status_code == 200, detail.text
    source = detail.json()["agent"]
    assert source["is_template"] is True

    response = await test_client.post(f"/api/agent/{source['slug']}/duplicate", headers=admin_headers)
    assert response.status_code == 200, response.text
    agent = response.json()["agent"]
    try:
        assert agent["name"].endswith(" 副本") and agent["is_default"] is False
        assert agent["share_config"] == {
            "version": 2,
            "read_scope": {"access_level": "user", "department_ids": [], "user_uids": [uid]},
            "manage_scope": {"access_level": "user", "department_ids": [], "user_uids": [uid]},
        }
        assert agent["is_template"] is False
    finally:
        cleanup = await test_client.delete(f"/api/agent/{agent['slug']}", headers=admin_headers)
        assert cleanup.status_code in {200, 404}, cleanup.text


async def test_duplicate_nonexistent_agent_returns_404(test_client, admin_headers):
    """不存在的 slug 直接 404。"""
    response = await test_client.post("/api/agent/no-such-agent-slug/duplicate", headers=admin_headers)
    assert response.status_code == 404, response.text


async def test_duplicate_other_private_agent_returns_404(test_client, standard_user):
    """他人私有智能体对当前用户不可见，复制请求按不存在处理。"""
    owner_uid = f"pytest-dup-owner-{uuid.uuid4().hex[:8]}"
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    created_slug: str | None = None
    try:
        async with session_factory() as db:
            repo = AgentRepository(db)
            source = await repo.create(
                name=f"Pytest 私有源 {uuid.uuid4().hex[:8]}",
                backend_id="ChatbotAgent",
                config_json={"context": {}},
                share_config={
                    "version": 2,
                    "read_scope": {"access_level": "user", "department_ids": [], "user_uids": [owner_uid]},
                    "manage_scope": {"access_level": "user", "department_ids": [], "user_uids": [owner_uid]},
                },
                created_by=owner_uid,
            )
            created_slug = source.slug

        response = await test_client.post(f"/api/agent/{created_slug}/duplicate", headers=standard_user["headers"])
        assert response.status_code == 404, response.text
    finally:
        if created_slug is not None:
            async with session_factory() as db:
                repo = AgentRepository(db)
                stale = await repo.get_by_slug(created_slug)
                if stale is not None:
                    await repo.delete(agent=stale)
        await engine.dispose()


async def test_duplicate_subagent_returns_400(test_client, admin_headers):
    """种子化的 fact-verifier 子智能体拒绝另存为副本。"""
    listing = await test_client.get("/api/agent", params={"include_subagents": "true"}, headers=admin_headers)
    assert listing.status_code == 200, listing.text
    subagents = [agent for agent in listing.json()["agents"] if agent["slug"] == "fact-verifier"]
    assert subagents, "fact-verifier 预置子智能体未落库"

    response = await test_client.post(f"/api/agent/{subagents[0]['slug']}/duplicate", headers=admin_headers)
    assert response.status_code == 400, response.text
