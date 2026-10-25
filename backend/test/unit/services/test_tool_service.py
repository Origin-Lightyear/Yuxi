from __future__ import annotations

from types import SimpleNamespace
from typing import Callable

from pydantic import BaseModel

from yuxi.agents.toolkits import service as tool_service


def test_get_tool_metadata_skips_runtime_only_args_that_cannot_generate_json_schema(monkeypatch):
    """运行时注入参数不可表达为 JSON Schema 时，只跳过参数列表，不阻断工具发现。"""
    tool_service._metadata_cache.clear()

    class RuntimeOnlyArgs(BaseModel):
        callback: Callable

    fake_tool = SimpleNamespace(
        name="runtime_tool",
        description="runtime tool",
        metadata={},
        args_schema=RuntimeOnlyArgs,
    )

    monkeypatch.setattr(
        "yuxi.agents.toolkits.registry.get_all_tool_instances",
        lambda: [fake_tool],
    )
    monkeypatch.setattr(
        "yuxi.agents.toolkits.registry.get_all_extra_metadata",
        lambda: {},
    )

    result = tool_service.get_tool_metadata()

    assert result[0]["slug"] == "runtime_tool"
    assert result[0]["args"] == []
    assert result[0]["category"] == "buildin"

    tool_service._metadata_cache.clear()


def test_get_tool_metadata_includes_config_guide(monkeypatch):
    tool_service._metadata_cache.clear()

    fake_tool = SimpleNamespace(
        name="demo_tool",
        description="demo description",
        metadata={},
        args_schema=None,
    )
    fake_extra = SimpleNamespace(
        category="buildin",
        tags=["demo"],
        display_name="演示工具",
        config_guide="请先配置 DEMO_API_KEY",
    )

    monkeypatch.setattr(
        "yuxi.agents.toolkits.registry.get_all_tool_instances",
        lambda: [fake_tool],
    )
    monkeypatch.setattr(
        "yuxi.agents.toolkits.registry.get_all_extra_metadata",
        lambda: {"demo_tool": fake_extra},
    )

    result = tool_service.get_tool_metadata()

    assert result == [
        {
            "slug": "demo_tool",
            "name": "演示工具",
            "description": "demo description",
            "metadata": {},
            "args": [],
            "category": "buildin",
            "tags": ["demo"],
            "config_guide": "请先配置 DEMO_API_KEY",
        }
    ]

    tool_service._metadata_cache.clear()
