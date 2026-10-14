from __future__ import annotations

import copy
from datetime import date, datetime, UTC
from typing import Any, Final

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.agents.buildin import get_agent_backend
from yuxi.agents.context import (
    BaseContext,
    filter_config_by_role,
    filter_declared_config,
    resolve_agent_resource_options,
)
from yuxi.agents.presets import SCENES, AgentPreset, discover_agent_presets
from yuxi.repositories.agent_repository import AgentRepository, user_can_access_agent
from yuxi.storage.postgres.models_business import Agent, User


async def prepare_agent_config_write(
    config_json: dict[str, Any],
    *,
    context_schema: type[BaseContext] | None,
    db: AsyncSession,
    user: User,
) -> tuple[dict[str, Any], dict[str, set[str]]]:
    """过滤可写配置，并解析本次资源补丁对应的可访问键。"""
    filtered = filter_config_by_role(config_json, user.role, context_schema)
    context = filtered.get("context")
    if not isinstance(context, dict):
        return filtered, {}

    resource_fields = (context_schema or BaseContext).get_resource_fields()
    submitted_fields = {
        field_name
        for field_name in resource_fields.keys() & context.keys()
        if isinstance(context[field_name], list) and context[field_name]
    }
    if not submitted_fields:
        return filtered, {}

    option_fields = {resource_fields[name] for name in submitted_fields}
    options = await resolve_agent_resource_options(option_fields, db=db, user=user)

    resource_access: dict[str, set[str]] = {}
    for field_name in submitted_fields:
        option_field = resource_fields[field_name]
        if option_field not in options:
            raise RuntimeError(f"智能体资源字段 {field_name} 缺少权限解析结果")
        resource_access[field_name] = {option["key"] for option in options[option_field]}
    return filtered, resource_access


async def duplicate_agent(agent_id: str, *, user: User, db: AsyncSession) -> dict:
    """把可访问的主智能体另存为当前用户私有的副本，返回与创建端点同形的响应。"""
    repo = AgentRepository(db)
    source = await repo.get_by_id(agent_id)
    if source is None:
        raise HTTPException(status_code=404, detail="智能体不存在")
    if not user_can_access_agent(user, source):
        raise HTTPException(status_code=404, detail="智能体不存在")
    if source.is_subagent:
        raise HTTPException(status_code=400, detail="子智能体不能另存为副本")

    backend = get_agent_backend(source.backend_id)
    config_json, config_resource_access = await prepare_agent_config_write(
        copy.deepcopy(source.config_json),
        context_schema=backend.context_schema,
        db=db,
        user=user,
    )
    item = await repo.create(
        name=f"{source.name} 副本",
        slug=None,
        backend_id=source.backend_id,
        description=source.description,
        icon=source.icon,
        pics=source.pics,
        config_json=config_json,
        config_resource_access=config_resource_access,
        share_config={
            "version": 2,
            "read_scope": {"access_level": "user", "department_ids": [], "user_uids": [str(user.uid)]},
            "manage_scope": {"access_level": "user", "department_ids": [], "user_uids": [str(user.uid)]},
        },
        is_default=False,
        is_subagent=False,
        created_by=str(user.uid),
        creator=user,
    )
    data = await repo.serialize(item, user=user, include_configurable_items=True)
    data["config_json"] = filter_declared_config(data.get("config_json") or {}, context_schema=backend.context_schema)
    return {"agent": data}


async def initialize_agent_presets(db: AsyncSession) -> None:
    """确认所有角色后端存在，再按既有落库规则初始化。"""
    presets = discover_agent_presets()
    for preset in presets:
        get_agent_backend(preset.backend_id)
    repository = AgentRepository(db)
    for preset in presets:
        await repository.ensure_preset(preset)


async def get_scene_templates(db: AsyncSession) -> dict:
    """聚合场景模板目录与每日一技；未种子化与空场景预置不产出。"""
    presets = [preset for preset in discover_agent_presets() if preset.scene]
    repository = AgentRepository(db)
    agents = {agent.slug: agent for agent in await repository.list_by_slugs([preset.slug for preset in presets])}
    candidates = [(preset, agents[preset.slug]) for preset in presets if preset.slug in agents]

    scenes = []
    for scene_key, scene_label in SCENES:
        templates = [
            {
                "agent_id": agent.id,
                "slug": preset.slug,
                "name": preset.name,
                "description": preset.description,
                "icon": preset.icon,
                "example_questions": preset.example_questions,
            }
            for preset, agent in candidates
            if preset.scene == scene_key
        ]
        if templates:
            scenes.append({"scene": scene_key, "label": scene_label, "templates": templates})
    return {"scenes": scenes, "daily_tip": pick_daily_tip(candidates, datetime.now(UTC).date())}


DAILY_TIP_ANCHOR: Final = date(2026, 1, 1)


def pick_daily_tip(candidates: list[tuple[AgentPreset, Agent]], today: date) -> dict | None:
    """按 SCENES × 预置定义顺序轮转选择每日一技；池为空时返回 None。"""
    pool: list[tuple[AgentPreset, str, Agent]] = [
        (preset, tip, agent)
        for scene_key, _ in SCENES
        for preset, agent in candidates
        if preset.scene == scene_key
        for tip in preset.tips
    ]
    if not pool:
        return None
    preset, tip, agent = pool[(today - DAILY_TIP_ANCHOR).days % len(pool)]
    return {"tip": tip, "slug": preset.slug, "name": preset.name, "agent_id": agent.id}
