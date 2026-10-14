from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuxi.agents.presets import SCENES, AgentPreset, discover_agent_presets
from yuxi.services import agent_config_service

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_get_scene_templates_groups_by_scene(monkeypatch):
    """分组按 SCENES 顺序，agent_id 使用种子行 id，未种子化与空场景预置跳过。"""
    scene_presets = [preset for preset in discover_agent_presets() if preset.scene]
    assert scene_presets, "场景模板测试要求至少存在一个带 scene 的预置"
    dropped_slugs = {preset.slug for preset in scene_presets if preset.scene == "research"}
    assert dropped_slugs, "当前 SCENES 含 research 场景；变更场景集合时需同步调整本测试"
    seeded_presets = [preset for preset in scene_presets if preset.slug not in dropped_slugs]
    seeded_slugs = {preset.slug for preset in seeded_presets}
    assert seeded_slugs

    list_by_slugs = AsyncMock(
        return_value=[
            SimpleNamespace(id=index + 1, slug=preset.slug) for index, preset in enumerate(seeded_presets)
        ]
    )
    monkeypatch.setattr(
        agent_config_service,
        "AgentRepository",
        lambda db: SimpleNamespace(list_by_slugs=list_by_slugs),
    )

    result = await agent_config_service.get_scene_templates(db=object())

    expected_scenes = [key for key, _ in SCENES if key != "research"]
    assert [scene["scene"] for scene in result["scenes"]] == expected_scenes
    labels = dict(SCENES)
    for scene in result["scenes"]:
        assert scene["label"] == labels[scene["scene"]]

    templates = [template for scene in result["scenes"] for template in scene["templates"]]
    assert {template["slug"] for template in templates} == seeded_slugs
    assert {template["agent_id"] for template in templates} == set(range(1, len(seeded_presets) + 1))
    by_slug = {template["slug"]: template for template in templates}
    for preset in seeded_presets:
        template = by_slug[preset.slug]
        assert template["name"] == preset.name
        assert template["description"] == preset.description
        assert template["icon"] == preset.icon
        assert template["example_questions"] == preset.example_questions

    requested = list_by_slugs.await_args.args[0]
    assert set(requested) == {preset.slug for preset in scene_presets}

    daily_tip = result["daily_tip"]
    assert daily_tip is None or {"tip", "slug", "name", "agent_id"} <= set(daily_tip)


def test_pick_daily_tip_deterministic():
    """锚点±1 天按池长度取模连续；负向偏移遵循 Python 取模语义。"""
    preset_a = AgentPreset(slug="tip-a", name="A", description="", scene="general", tips=("t1", "t2"))
    preset_b = AgentPreset(slug="tip-b", name="B", description="", scene="writing", tips=("t3",))
    agent_a = SimpleNamespace(id=11)
    agent_b = SimpleNamespace(id=22)
    candidates = [(preset_a, agent_a), (preset_b, agent_b)]

    on_anchor = agent_config_service.pick_daily_tip(candidates, date(2026, 1, 1))
    day_after = agent_config_service.pick_daily_tip(candidates, date(2026, 1, 2))
    day_before = agent_config_service.pick_daily_tip(candidates, date(2025, 12, 31))

    assert on_anchor == {"tip": "t1", "slug": "tip-a", "name": "A", "agent_id": 11}
    assert day_after == {"tip": "t2", "slug": "tip-a", "name": "A", "agent_id": 11}
    # 池按 SCENES 顺序展平：general(t1, t2) → writing(t3)，负向一天落在池尾。
    assert day_before == {"tip": "t3", "slug": "tip-b", "name": "B", "agent_id": 22}

    reversed_order = agent_config_service.pick_daily_tip(list(reversed(candidates)), date(2026, 1, 1))
    assert reversed_order == on_anchor


def test_pick_daily_tip_empty_pool_returns_none():
    """无候选或候选均无 tips 时不产出每日一技。"""
    assert agent_config_service.pick_daily_tip([], date(2026, 1, 1)) is None
    preset = AgentPreset(slug="tip-none", name="N", description="", scene="general", tips=())
    assert agent_config_service.pick_daily_tip([(preset, SimpleNamespace(id=1))], date(2026, 1, 1)) is None
