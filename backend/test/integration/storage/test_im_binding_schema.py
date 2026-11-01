"""真实 PostgreSQL 上的 IM 账号绑定 schema 演进与唯一约束测试。"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.storage.postgres.models_business import (
    Base,
    IMAccountBinding,
    IMBindingSession,
    IMBotConfig,
    User,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture()
async def binding_engine():
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "ALTER TABLE IF EXISTS im_bot_configs "
                "ADD COLUMN IF NOT EXISTS require_binding BOOLEAN NOT NULL DEFAULT false"
            )
        )
        # 新表依赖 users / im_bot_configs；用 metadata（checkfirst）建表既覆盖干净库也不改动既有表。
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _create_user(session, uid: str) -> int:
    """创建临时用户并返回其自增 id。"""
    user = User(username=uid, uid=uid, password_hash="pytest")
    session.add(user)
    await session.commit()
    return user.id


async def _delete_users(session, user_uids: list[str]) -> None:
    """按 uid 清理临时用户，确保失败的用例不残留已提交行。"""
    await session.execute(delete(User).where(User.uid.in_(user_uids)))
    await session.commit()


async def test_binding_unique_constraints(binding_engine):
    """同一 platform_user_id 只能绑定一次（跨用户唯一）。"""
    uid = uuid.uuid4().hex[:8]
    user_uids = [f"bind-{uid}-a", f"bind-{uid}-b"]
    try:
        async with binding_engine() as session:
            user_a_id = await _create_user(session, user_uids[0])
            user_b_id = await _create_user(session, user_uids[1])
        async with binding_engine() as session:
            session.add(IMAccountBinding(user_id=user_a_id, platform="dingtalk", platform_user_id=f"u-{uid}"))
            session.add(IMAccountBinding(user_id=user_b_id, platform="dingtalk", platform_user_id=f"u-{uid}"))
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()
    finally:
        async with binding_engine() as session:
            await _delete_users(session, user_uids)


async def test_binding_same_user_same_platform_unique(binding_engine):
    """同一用户同一平台只能绑定一次。"""
    uid = uuid.uuid4().hex[:8]
    user_uid = f"bind-{uid}"
    try:
        async with binding_engine() as session:
            user_id = await _create_user(session, user_uid)
        async with binding_engine() as session:
            session.add(IMAccountBinding(user_id=user_id, platform="feishu", platform_user_id=f"f-{uid}-1"))
            session.add(IMAccountBinding(user_id=user_id, platform="feishu", platform_user_id=f"f-{uid}-2"))
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()
    finally:
        async with binding_engine() as session:
            await _delete_users(session, [user_uid])


async def test_bot_config_require_binding_default_false():
    """require_binding 元数据默认值必须为 False（无需连库）。"""
    column = IMBotConfig.__table__.c.require_binding
    assert column.default.arg is False


async def test_binding_unique_constraints_present_in_metadata():
    """唯一约束必须在模型 metadata 中声明，避免建表后因已迁移而无法被负向测试发现。"""
    binding_constraint_names = {constraint.name for constraint in IMAccountBinding.__table__.constraints}
    assert {"uq_im_binding_platform_user", "uq_im_binding_user_platform"} <= binding_constraint_names

    session_unique_indexes = {index.name: index for index in IMBindingSession.__table__.indexes}
    assert session_unique_indexes["ix_im_binding_sessions_state_hash"].unique is True
