"""预置角色定义；每个模块导出一个 PRESET。"""

from dataclasses import dataclass, field
from functools import lru_cache
from importlib import import_module
from pathlib import Path
from typing import Any

SCENES: tuple[tuple[str, str], ...] = (
    ("general", "通用助手"),
    ("writing", "写作与办公"),
    ("research", "研究与分析"),
)


@dataclass(frozen=True)
class AgentPreset:
    """复用既有执行后端的角色配置。"""

    slug: str
    name: str
    description: str
    backend_id: str = "ChatbotAgent"
    context: dict[str, Any] = field(default_factory=dict)
    scene: str = ""
    icon: str = ""
    example_questions: tuple[str, ...] = ()
    tips: tuple[str, ...] = ()


def discover_agent_presets() -> list[AgentPreset]:
    """递归发现角色，拒绝重复标识、无效场景和无效定义。"""
    scene_keys = {key for key, _ in SCENES}
    presets = {}
    root = Path(__file__).parent
    for path in sorted(root.rglob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = ".".join(path.relative_to(root).with_suffix("").parts)
        module = import_module(f"{__name__}.{module_name}")
        preset = module.PRESET
        if not isinstance(preset, AgentPreset):
            raise TypeError(f"{module.__name__}.PRESET 必须是 AgentPreset")
        if preset.slug in presets:
            raise ValueError(f"重复的预置 Agent slug: {preset.slug}")
        if preset.scene and preset.scene not in scene_keys:
            raise ValueError(f"预置 Agent {preset.slug} 的 scene 不在 SCENES 中: {preset.scene}")
        presets[preset.slug] = preset
    return list(presets.values())


@lru_cache(maxsize=1)
def preset_slug_set() -> frozenset[str]:
    """已发现预置角色的 slug 集合，供场景模板判定缓存使用。"""
    return frozenset(preset.slug for preset in discover_agent_presets())
