"""AgentMemory 模型的序列化契约测试。"""

import pytest

from yuxi.storage.postgres.models_business import AgentMemory

pytestmark = pytest.mark.unit


def test_to_dict_exposes_all_seven_fields() -> None:
    memory = AgentMemory(uid="u1", agent_slug="a1", content="条目", source_run_id="r1")

    data = memory.to_dict()

    assert set(data) == {
        "id",
        "uid",
        "agent_slug",
        "content",
        "source_run_id",
        "created_at",
        "updated_at",
    }
    assert data["uid"] == "u1"
    assert data["agent_slug"] == "a1"
    assert data["content"] == "条目"
    assert data["source_run_id"] == "r1"
