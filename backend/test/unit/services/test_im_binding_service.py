"""IM 账号绑定服务：纯函数与缓存预热单元测试（不连库）。"""

import pytest

from yuxi.services.im_binding_service import _build_authorize_url, _hash_state

DINGTALK_CREDENTIALS = {"client_id": "ding-client", "client_secret": "ding-secret"}
FEISHU_CREDENTIALS = {"app_id": "fei-app", "app_secret": "fei-secret"}


def test_build_authorize_url_dingtalk():
    url = _build_authorize_url(
        "dingtalk", DINGTALK_CREDENTIALS, "https://yuxi.example/api/im-bindings/callback/dingtalk", "state-123"
    )
    assert url.startswith("https://login.dingtalk.com/oauth2/auth")
    assert "client_id=ding-client" in url
    assert "state=state-123" in url
    assert "scope=openid" in url
    assert "response_type=code" in url
    assert "prompt=consent" in url


def test_build_authorize_url_feishu():
    url = _build_authorize_url(
        "feishu", FEISHU_CREDENTIALS, "https://yuxi.example/api/im-bindings/callback/feishu", "state-123"
    )
    assert url.startswith("https://open.feishu.cn/open-apis/authen/v1/index")
    assert "app_id=fei-app" in url
    assert "state=state-123" in url


def test_hash_state_is_stable_and_not_plaintext():
    digest = _hash_state("abc")
    assert digest == _hash_state("abc")
    assert digest != "abc"
    assert len(digest) == 64  # sha256 hexdigest


@pytest.mark.asyncio
async def test_preheat_binding_cache_writes_bound_platforms_and_clears_stale(monkeypatch):
    """登录预热只写 Tenant 已绑定的支持平台，并清掉本平台旧映射。"""
    from yuxi.config import config as app_config
    from yuxi.services import im_binding_service
    from yuxi.services.saas_session import SaasAgentSession

    monkeypatch.setattr(app_config, "saas_enabled", True)
    monkeypatch.setattr(app_config, "platform_service_url", "https://tenant.example.com")

    remembered: list[dict] = []
    forgotten: list[str] = []
    seen_expires_at: list[str] = []

    class _FakeClient:
        async def list_channel_bindings(self, token):
            assert token == "session-1"
            return [
                {"channelType": "FEISHU", "bound": True, "channelUserId": "ou-1", "boundAt": "2026-11-06T10:20:30"},
                {"channelType": "DINGTALK", "bound": False, "channelUserId": None, "boundAt": None},
                {"channelType": "WECOM", "bound": True, "channelUserId": "wc-1", "boundAt": "2026-11-05T09:08:07"},
            ]

    async def fake_remember(**kwargs):
        remembered.append(kwargs)

    async def fake_forget(*, uid, platform):
        forgotten.append(platform)

    monkeypatch.setattr(im_binding_service, "get_saas_client", lambda: _FakeClient())
    monkeypatch.setattr(im_binding_service.im_binding_cache, "remember_binding", fake_remember)
    monkeypatch.setattr(im_binding_service.im_binding_cache, "forget_binding", fake_forget)
    monkeypatch.setattr(
        im_binding_service,
        "get_saas_session_ttl_seconds",
        lambda expires_at: seen_expires_at.append(expires_at) or 600,
    )

    count = await im_binding_service.preheat_binding_cache(
        "emp-1",
        SaasAgentSession(
            token="session-1",
            expires_at="1793974830",
            tenant_id=1,
            employee_id=2,
            mcp_instance_id="runtime-1",
        ),
    )

    assert count == 1
    assert seen_expires_at == ["1793974830"]
    assert forgotten == ["dingtalk", "feishu"]
    assert len(remembered) == 1
    assert remembered[0]["uid"] == "emp-1"
    assert remembered[0]["platform"] == "feishu"
    assert remembered[0]["channel_user_id"] == "ou-1"
    assert remembered[0]["ttl_seconds"] == 600
