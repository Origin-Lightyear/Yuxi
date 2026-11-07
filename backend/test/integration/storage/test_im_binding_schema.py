"""IM 绑定会话 schema 测试：本地只保留一次性 state，绑定关系归 Tenant。"""

from __future__ import annotations

import pytest

import yuxi.storage.postgres.models_business as models_business
from yuxi.storage.postgres.models_business import Base, IMBindingSession, IMBotConfig

pytestmark = [pytest.mark.integration]


async def test_bot_config_require_binding_default_false():
    """require_binding 元数据默认值必须为 False（无需连库）。"""
    column = IMBotConfig.__table__.c.require_binding
    assert column.default.arg is False


async def test_binding_session_state_hash_unique_index():
    """一次性 state 必须唯一，重放才可能在建表层被拒绝。"""
    session_unique_indexes = {index.name: index for index in IMBindingSession.__table__.indexes}
    assert session_unique_indexes["ix_im_binding_sessions_state_hash"].unique is True


async def test_local_account_binding_table_is_gone():
    """绑定关系由 Tenant 唯一保存；业务库不得再声明本地绑定表或模型。"""
    assert "im_account_bindings" not in Base.metadata.tables
    assert not hasattr(models_business, "IMAccountBinding")
