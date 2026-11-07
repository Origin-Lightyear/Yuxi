import json
import os
import time
from datetime import UTC, datetime

import pytest

from yuxi.services import saas_session
from yuxi.services.saas_session import SaasAgentSession, get_saas_session_ttl_seconds


def test_saas_agent_session_repr_hides_token():
    session = SaasAgentSession(
        token="sensitive-session-token",
        expires_at="1789842085",
        tenant_id=1,
        employee_id=1,
        mcp_instance_id="runtime-1",
    )

    assert "sensitive-session-token" not in repr(session)


def test_ttl_treats_naive_expires_at_as_utc(monkeypatch):
    """Tenant 裸 ISO 串按 UTC 解释，TTL 不得随进程本地时区偏移。"""
    now = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC).timestamp()
    monkeypatch.setattr(time, "time", lambda: now)

    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    try:
        assert get_saas_session_ttl_seconds("2026-10-11T12:00:00") == 86400
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


def test_ttl_accepts_epoch_seconds_and_offset_iso(monkeypatch):
    now = 1_760_000_000.0
    monkeypatch.setattr(time, "time", lambda: now)

    assert get_saas_session_ttl_seconds(str(now + 300)) == 300
    iso_z = datetime.fromtimestamp(now + 600, tz=UTC).isoformat().replace("+00:00", "Z")
    assert get_saas_session_ttl_seconds(iso_z) == 600


@pytest.mark.asyncio
async def test_get_saas_agent_session_tolerates_legacy_payload_without_mobile(monkeypatch):
    """升级前写入的会话没有 mobile 键，反序列化必须兼容并归一为 None。"""
    payload = json.dumps(
        {
            "token": "legacy-token",
            "expires_at": "1793974830",
            "tenant_id": 1,
            "employee_id": 2,
            "mcp_instance_id": "runtime-1",
        }
    )

    class FakeRedis:
        async def get(self, _key):
            return payload

    async def fake_client():
        return FakeRedis()

    monkeypatch.setattr(saas_session, "get_async_redis_client", fake_client)

    session = await saas_session.get_saas_agent_session("E001")

    assert session is not None
    assert session.mobile is None
