"""AgentMemory 模型的序列化契约与复合唯一约束测试。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from yuxi.storage.postgres.models_business import AgentMemory, Base

pytestmark = pytest.mark.unit


def test_to_dict_exposes_all_eight_fields() -> None:
    memory = AgentMemory(uid="u1", agent_slug="a1", content="条目", source_run_id="r1")

    data = memory.to_dict()

    assert set(data) == {
        "id",
        "uid",
        "agent_slug",
        "content",
        "source_run_id",
        "extract_index",
        "created_at",
        "updated_at",
    }
    assert data["uid"] == "u1"
    assert data["agent_slug"] == "a1"
    assert data["content"] == "条目"
    assert data["source_run_id"] == "r1"
    assert data["extract_index"] == 0


def test_same_run_with_distinct_extract_index_is_allowed_and_duplicate_conflicts() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[AgentMemory.__table__])
    try:
        with Session(engine) as session:
            session.add(AgentMemory(uid="u1", agent_slug="a1", content="第一条", source_run_id="r1", extract_index=0))
            session.add(AgentMemory(uid="u1", agent_slug="a1", content="第二条", source_run_id="r1", extract_index=1))
            session.commit()

            session.add(AgentMemory(uid="u1", agent_slug="a1", content="重复序号", source_run_id="r1", extract_index=1))
            with pytest.raises(IntegrityError):
                session.commit()
    finally:
        engine.dispose()
