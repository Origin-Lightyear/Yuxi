import os
import time
from datetime import UTC, datetime

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
