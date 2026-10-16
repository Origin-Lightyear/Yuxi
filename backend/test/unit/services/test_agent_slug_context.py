"""BaseContext 的 agent_slug 身份字段单元测试。"""

from dataclasses import fields

import pytest

from yuxi.agents.context import BaseContext

pytestmark = pytest.mark.unit


def test_agent_slug_defaults_to_none():
    context = BaseContext()

    assert context.agent_slug is None


def test_update_sets_agent_slug_and_keeps_it_non_configurable():
    context = BaseContext()

    context.update({"agent_slug": "my-agent"})

    assert context.agent_slug == "my-agent"
    agent_slug_field = next(item for item in fields(context) if item.name == "agent_slug")
    assert agent_slug_field.metadata["configurable"] is False
    configurable_fields = {item.name for item in fields(context) if item.metadata.get("configurable", True)}
    assert "agent_slug" not in configurable_fields


def test_update_config_ignores_external_agent_slug():
    context = BaseContext()

    context.update_config({"agent_slug": "spoofed-agent"})

    assert context.agent_slug is None
