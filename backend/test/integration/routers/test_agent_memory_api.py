"""记忆条目查看与删除两端点的真实 HTTP 集成测试。"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.repositories.agent_memory_repository import AgentMemoryRepository
from yuxi.repositories.agent_repository import AgentRepository
from yuxi.storage.postgres.models_business import AgentMemory

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _session_factory():
    engine = create_async_engine(os.environ["POSTGRES_URL"])
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _create_agent_visible_to(session_factory, uid: str) -> str:
    async with session_factory() as db:
        agent = await AgentRepository(db).create(
            name=f"Pytest 记忆 {uuid.uuid4().hex[:8]}",
            backend_id="ChatbotAgent",
            config_json={"context": {}},
            share_config={
                "version": 2,
                "read_scope": {"access_level": "user", "department_ids": [], "user_uids": [uid]},
                "manage_scope": {"access_level": "user", "department_ids": [], "user_uids": [uid]},
            },
            created_by=uid,
        )
        return agent.slug


async def _add_memory(session_factory, *, uid: str, slug: str, content: str) -> int:
    async with session_factory() as db:
        created = await AgentMemoryRepository(db).add(
            uid=uid, agent_slug=slug, content=content, source_run_id=uuid.uuid4().hex
        )
        await db.commit()
        return int(created["id"])


async def _cleanup_agent(session_factory, slug: str) -> None:
    async with session_factory() as db:
        await db.execute(sa_delete(AgentMemory).where(AgentMemory.agent_slug == slug))
        agent = await AgentRepository(db).get_by_slug(slug)
        if agent is not None:
            await AgentRepository(db).delete(agent=agent)
        await db.commit()


async def test_get_memories_returns_owned_items_in_desc_order(test_client, admin_headers):
    """本人可见智能体的两条记忆按倒序返回。"""
    me = await test_client.get("/api/auth/me", headers=admin_headers)
    assert me.status_code == 200, me.text
    uid = str(me.json()["uid"])

    engine, session_factory = _session_factory()
    slug = None
    try:
        slug = await _create_agent_visible_to(session_factory, uid)
        await _add_memory(session_factory, uid=uid, slug=slug, content="较早记忆")
        await _add_memory(session_factory, uid=uid, slug=slug, content="较新记忆")

        response = await test_client.get(f"/api/agent/{slug}/memories", headers=admin_headers)
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert [item["content"] for item in items] == ["较新记忆", "较早记忆"]
    finally:
        if slug is not None:
            await _cleanup_agent(session_factory, slug)
        await engine.dispose()


async def test_get_memories_unknown_or_invisible_slug_returns_404(test_client, admin_headers):
    """不存在的 slug 与他人私有 slug 一律 404，不泄露存在性。"""
    unknown = await test_client.get(f"/api/agent/no-such-{uuid.uuid4().hex[:8]}/memories", headers=admin_headers)
    assert unknown.status_code == 404, unknown.text

    owner_uid = f"pytest-mem-owner-{uuid.uuid4().hex[:8]}"
    engine, session_factory = _session_factory()
    slug = None
    try:
        slug = await _create_agent_visible_to(session_factory, owner_uid)

        response = await test_client.get(f"/api/agent/{slug}/memories", headers=admin_headers)
        assert response.status_code == 404, response.text
    finally:
        if slug is not None:
            await _cleanup_agent(session_factory, slug)
        await engine.dispose()


async def test_delete_memory_removes_own_row(test_client, admin_headers):
    """本人条目删除返回 200 且数据库行消失。"""
    me = await test_client.get("/api/auth/me", headers=admin_headers)
    assert me.status_code == 200, me.text
    uid = str(me.json()["uid"])

    engine, session_factory = _session_factory()
    slug = None
    try:
        slug = await _create_agent_visible_to(session_factory, uid)
        memory_id = await _add_memory(session_factory, uid=uid, slug=slug, content="待删记忆")

        response = await test_client.delete(f"/api/agent/{slug}/memories/{memory_id}", headers=admin_headers)
        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True}

        async with session_factory() as db:
            remaining = await AgentMemoryRepository(db).list_by_agent(uid=uid, agent_slug=slug)
        assert remaining == []
    finally:
        if slug is not None:
            await _cleanup_agent(session_factory, slug)
        await engine.dispose()


async def test_delete_other_users_memory_returns_404_and_keeps_row(test_client, admin_headers):
    """同 slug 下他人 uid 的条目删除返回 404，且行保持不变。"""
    me = await test_client.get("/api/auth/me", headers=admin_headers)
    assert me.status_code == 200, me.text
    uid = str(me.json()["uid"])
    other_uid = f"pytest-mem-other-{uuid.uuid4().hex[:8]}"

    engine, session_factory = _session_factory()
    slug = None
    try:
        slug = await _create_agent_visible_to(session_factory, uid)
        memory_id = await _add_memory(session_factory, uid=other_uid, slug=slug, content="他人记忆")

        response = await test_client.delete(f"/api/agent/{slug}/memories/{memory_id}", headers=admin_headers)
        assert response.status_code == 404, response.text

        async with session_factory() as db:
            rows = await AgentMemoryRepository(db).list_by_agent(uid=other_uid, agent_slug=slug)
        assert [row["id"] for row in rows] == [memory_id]
    finally:
        if slug is not None:
            await _cleanup_agent(session_factory, slug)
        await engine.dispose()
