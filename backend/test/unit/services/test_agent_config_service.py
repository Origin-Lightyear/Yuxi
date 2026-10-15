from __future__ import annotations

import copy
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from yuxi.agents.context import BaseContext, ResourceSelection
from yuxi.services import agent_config_service

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_prepare_agent_config_write_resolves_only_submitted_resource_fields(monkeypatch):
    """保存补丁为每个提交资源返回对应的真实权限集合。"""
    resource_fields = {"tools", "knowledges", "mcps", "skills", "subagents"}
    resolver = AsyncMock(
        return_value={
            field_name: [{"key": f"visible-{field_name}"}, {"key": f"also-visible-{field_name}"}]
            for field_name in resource_fields
        }
    )

    monkeypatch.setattr(agent_config_service, "resolve_agent_resource_options", resolver)
    db = object()
    user = SimpleNamespace(role="user")

    config, resource_access = await agent_config_service.prepare_agent_config_write(
        {
            "context": {
                "title": "renamed",
                "tools": ["visible-tools"],
                "knowledges": ["visible-knowledges"],
                "mcps": ["visible-mcps"],
                "skills": ["visible-skills"],
                "subagents": ["visible-subagents"],
                "preload_skills": ["visible-skills"],
            }
        },
        context_schema=ConfigContext,
        db=db,
        user=user,
    )

    assert config["context"]["title"] == "renamed"
    assert resource_access == {
        "tools": {"visible-tools", "also-visible-tools"},
        "knowledges": {"visible-knowledges", "also-visible-knowledges"},
        "mcps": {"visible-mcps", "also-visible-mcps"},
        "skills": {"visible-skills", "also-visible-skills"},
        "subagents": {"visible-subagents", "also-visible-subagents"},
        "preload_skills": {"visible-skills", "also-visible-skills"},
    }
    resolver.assert_awaited_once_with(resource_fields, db=db, user=user)


@pytest.mark.asyncio
async def test_prepare_agent_config_write_does_not_load_resources_for_unrelated_patch(monkeypatch):
    """无关配置修改不依赖资源列表的可用性。"""
    monkeypatch.setattr(
        agent_config_service,
        "resolve_agent_resource_options",
        AsyncMock(side_effect=AssertionError("resource options should not be loaded")),
    )

    config, resource_access = await agent_config_service.prepare_agent_config_write(
        {"context": {"title": "renamed"}},
        context_schema=ConfigContext,
        db=object(),
        user=SimpleNamespace(role="user"),
    )

    assert config == {"context": {"title": "renamed"}}
    assert resource_access == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("subagents", [[], ["undeclared-subagent"]])
@pytest.mark.parametrize("selection", [[], "all"])
async def test_prepare_agent_config_write_does_not_load_resources_for_strategy_switch(
    monkeypatch, subagents, selection
):
    """显式策略切换保留原值，未声明的字段在资源解析前被移除。"""
    monkeypatch.setattr(
        agent_config_service,
        "resolve_agent_resource_options",
        AsyncMock(side_effect=AssertionError("resource options should not be loaded")),
    )

    config, resource_access = await agent_config_service.prepare_agent_config_write(
        {"context": {"skills": selection, "mcps": selection, "preload_skills": selection, "subagents": subagents}},
        context_schema=None,
        db=object(),
        user=SimpleNamespace(role="user"),
    )

    assert config == {"context": {"skills": selection, "mcps": selection, "preload_skills": selection}}
    assert resource_access == {}


@pytest.mark.asyncio
async def test_duplicate_agent_missing_source_returns_404(monkeypatch):
    """按 slug 读不到源智能体时按不存在拒绝，且不触发创建。"""
    repo = _DuplicateAgentRepo(source=None)
    monkeypatch.setattr(agent_config_service, "AgentRepository", lambda db: repo)

    with pytest.raises(HTTPException) as exc:
        await agent_config_service.duplicate_agent("missing-template", user=_dup_user(), db=object())

    assert exc.value.status_code == 404
    assert repo.created_kwargs is None
    assert repo.looked_up_slug == "missing-template"


@pytest.mark.asyncio
async def test_duplicate_agent_inaccessible_source_returns_404(monkeypatch):
    """他人私有源在访问判定处按不存在拒绝，且不触发创建。"""
    repo = _DuplicateAgentRepo(source=_dup_source())
    monkeypatch.setattr(agent_config_service, "AgentRepository", lambda db: repo)
    monkeypatch.setattr(agent_config_service, "user_can_access_agent", lambda user, agent: False)

    with pytest.raises(HTTPException) as exc:
        await agent_config_service.duplicate_agent("research-template", user=_dup_user(), db=object())

    assert exc.value.status_code == 404
    assert repo.created_kwargs is None


@pytest.mark.asyncio
async def test_duplicate_agent_inaccessible_subagent_returns_404(monkeypatch):
    """无权源先于子智能体判定拒绝为 404，守卫顺序不可调换。"""
    repo = _DuplicateAgentRepo(source=_dup_source(is_subagent=True))
    monkeypatch.setattr(agent_config_service, "AgentRepository", lambda db: repo)
    monkeypatch.setattr(agent_config_service, "user_can_access_agent", lambda user, agent: False)

    with pytest.raises(HTTPException) as exc:
        await agent_config_service.duplicate_agent("secret-subagent", user=_dup_user(), db=object())

    assert exc.value.status_code == 404
    assert repo.created_kwargs is None


@pytest.mark.asyncio
async def test_duplicate_agent_subagent_returns_400(monkeypatch):
    """可访问的子智能体在创建前被拒绝。"""
    repo = _DuplicateAgentRepo(source=_dup_source(is_subagent=True))
    monkeypatch.setattr(agent_config_service, "AgentRepository", lambda db: repo)
    monkeypatch.setattr(agent_config_service, "user_can_access_agent", lambda user, agent: True)

    with pytest.raises(HTTPException) as exc:
        await agent_config_service.duplicate_agent("fact-verifier", user=_dup_user(), db=object())

    assert exc.value.status_code == 400
    assert repo.created_kwargs is None


@pytest.mark.asyncio
async def test_duplicate_agent_creates_private_copy_for_current_user(monkeypatch):
    """副本以当前用户私有共享配置落库，复制面与源一致且配置互相独立。"""
    source = _dup_source()
    source_snapshot = copy.deepcopy(source.config_json)
    repo = _DuplicateAgentRepo(source=source)
    monkeypatch.setattr(agent_config_service, "AgentRepository", lambda db: repo)
    monkeypatch.setattr(agent_config_service, "user_can_access_agent", lambda user, agent: True)

    result = await agent_config_service.duplicate_agent("research-template", user=_dup_user(), db=object())

    assert set(result) == {"agent"}
    assert repo.looked_up_slug == "research-template"
    kwargs = repo.created_kwargs
    assert kwargs["name"] == "研究模板 副本"
    assert kwargs["slug"] is None
    assert kwargs["backend_id"] == "ChatbotAgent"
    assert kwargs["share_config"] == {
        "version": 2,
        "read_scope": {"access_level": "user", "department_ids": [], "user_uids": ["u1"]},
        "manage_scope": {"access_level": "user", "department_ids": [], "user_uids": ["u1"]},
    }
    assert kwargs["is_default"] is False
    assert kwargs["is_subagent"] is False
    assert kwargs["created_by"] == "u1"

    assert kwargs["description"] == source.description
    assert kwargs["icon"] == source.icon
    assert kwargs["pics"] == source.pics
    assert kwargs["config_json"] == source.config_json
    assert kwargs["config_json"] is not source.config_json

    kwargs["config_json"]["context"]["system_prompt"] = "副本提示词"
    assert source.config_json == source_snapshot
    assert source.config_json == {"context": {"system_prompt": "源提示词"}}


def _dup_user() -> SimpleNamespace:
    return SimpleNamespace(uid="u1", role="admin")


def _dup_source(*, is_subagent: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        name="研究模板",
        backend_id="ChatbotAgent",
        description="源描述",
        icon="telescope",
        pics=["src.png"],
        config_json={"context": {"system_prompt": "源提示词"}},
        is_subagent=is_subagent,
    )


class _DuplicateAgentRepo:
    """仅覆盖 duplicate_agent 读写面的内存替身。"""

    def __init__(self, source):
        self.source = source
        self.created_kwargs = None
        self.looked_up_slug = None

    async def get_by_slug(self, slug: str):
        self.looked_up_slug = slug
        return self.source

    async def create(self, **kwargs):
        self.created_kwargs = kwargs
        return SimpleNamespace(id=9)

    async def serialize(self, item, *, user, include_configurable_items: bool = False):
        return {"id": item.id, "config_json": {}}


@dataclass(kw_only=True)
class ConfigContext(BaseContext):
    """覆盖保存资源字段的最小测试 Schema。"""

    subagents: ResourceSelection = "all"
    title: str = ""
