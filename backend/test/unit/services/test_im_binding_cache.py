"""IM 绑定 Redis 缓存原语的单元测试。"""

from __future__ import annotations

import pytest

import yuxi.services.im_binding_cache as im_binding_cache


class _FakeRedis:
    """记录写入 TTL 的内存 Redis 替身。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value
        self.ttls[key] = ex

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)
        self.ttls.pop(key, None)


@pytest.fixture()
def fake_redis(monkeypatch):
    """让缓存模块使用内存 Redis 替身。"""
    redis = _FakeRedis()

    async def _get_redis():
        return redis

    monkeypatch.setattr(im_binding_cache, "get_async_redis_client", _get_redis)
    return redis


@pytest.mark.asyncio
async def test_remember_binding_writes_forward_and_reverse_with_ttl(fake_redis):
    """绑定缓存同时支持按平台身份查 uid 与按 uid 清理，TTL 取 AgentSession 剩余时间。"""
    await im_binding_cache.remember_binding(
        uid="emp-1", platform="feishu", channel_user_id="ou_1", ttl_seconds=120
    )

    assert fake_redis.values == {
        "saas:im-binding:feishu:ou_1": "emp-1",
        "saas:im-binding:uid:emp-1:feishu": "ou_1",
    }
    assert fake_redis.ttls == {
        "saas:im-binding:feishu:ou_1": 120,
        "saas:im-binding:uid:emp-1:feishu": 120,
    }


@pytest.mark.asyncio
async def test_remember_binding_failure_on_forward_write_leaves_no_forward_mapping(fake_redis):
    """第二次写入（正向键）失败时不能留下消息侧会命中的映射。"""
    original_set = fake_redis.set

    async def failing_forward_set(key, value, ex=None):
        if key.startswith("saas:im-binding:feishu:"):
            raise RuntimeError("redis down")
        await original_set(key, value, ex)

    fake_redis.set = failing_forward_set

    with pytest.raises(RuntimeError, match="redis down"):
        await im_binding_cache.remember_binding(
            uid="emp-1", platform="feishu", channel_user_id="ou_1", ttl_seconds=60
        )

    assert "saas:im-binding:feishu:ou_1" not in fake_redis.values
    assert fake_redis.values == {"saas:im-binding:uid:emp-1:feishu": "ou_1"}


@pytest.mark.asyncio
async def test_bind_uid_returns_none_when_missing(fake_redis):
    """未命中返回 None，调用方据此回查 Tenant。"""
    assert await im_binding_cache.bind_uid("feishu", "ou-missing") is None


@pytest.mark.asyncio
async def test_forget_binding_deletes_forward_key_from_reverse(fake_redis):
    """解绑只拿 uid 与平台，必须借助反向键删除正向映射。"""
    await im_binding_cache.remember_binding(
        uid="emp-1", platform="dingtalk", channel_user_id="ding-1", ttl_seconds=60
    )

    await im_binding_cache.forget_binding(uid="emp-1", platform="dingtalk")

    assert fake_redis.values == {}


@pytest.mark.asyncio
async def test_forget_user_bindings_clears_all_platforms(fake_redis):
    """登出或会话失效时清理该用户全部平台的缓存。"""
    await im_binding_cache.remember_binding(uid="emp-1", platform="feishu", channel_user_id="ou_1", ttl_seconds=60)
    await im_binding_cache.remember_binding(uid="emp-1", platform="dingtalk", channel_user_id="ding-1", ttl_seconds=60)

    await im_binding_cache.forget_user_bindings("emp-1")

    assert fake_redis.values == {}
