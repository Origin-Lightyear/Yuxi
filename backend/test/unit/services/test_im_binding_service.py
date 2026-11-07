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


def test_normalize_mobile_strips_country_code_and_separators():
    """平台资料与本地用户手机号可能格式不同，比较前统一归一化。"""
    from yuxi.services.im_binding_service import _normalize_mobile

    assert _normalize_mobile("+86 138-0013-8000") == "13800138000"
    assert _normalize_mobile("8613800138000") == "13800138000"
    assert _normalize_mobile("13800138000") == "13800138000"
    assert _normalize_mobile(None) == ""


@pytest.mark.asyncio
async def test_feishu_exchange_reads_profile_mobile(monkeypatch):
    """飞书换取身份后必须读取用户资料手机号，供绑定归属核验。"""
    from yuxi.services import im_binding_service

    seen: list[tuple[str, str, dict]] = []

    async def fake_request_json(method, url, **kwargs):
        seen.append((method, url, kwargs))
        if url == im_binding_service.FEISHU_TENANT_ACCESS_TOKEN_URL:
            return {"tenant_access_token": "tenant-token"}
        if url == im_binding_service.FEISHU_USER_ACCESS_TOKEN_URL:
            return {"data": {"open_id": "ou-1", "union_id": "un-1", "access_token": "user-token"}}
        if url == im_binding_service.FEISHU_USER_INFO_URL:
            return {"data": {"mobile": "+8613800138000"}}
        raise AssertionError(url)

    monkeypatch.setattr(im_binding_service, "_request_json", fake_request_json)

    identity = await im_binding_service._exchange_code("feishu", FEISHU_CREDENTIALS, "code", None)

    assert identity == ("ou-1", "un-1", "+8613800138000")
    method, url, kwargs = seen[-1]
    assert (method, url) == ("GET", im_binding_service.FEISHU_USER_INFO_URL)
    assert kwargs["headers"] == {"Authorization": "Bearer user-token"}


def test_platform_account_must_match_local_mobile():
    """手机号缺失或不一致时拒绝绑定，且不泄露具体号码。"""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from yuxi.services.im_binding_service import _ensure_platform_account_matches_user

    user = SimpleNamespace(uid="EMP001", phone_number="13800138000")

    with pytest.raises(HTTPException) as exc:
        _ensure_platform_account_matches_user(user, "13900000000")
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "im_binding_identity_mismatch"
    assert "13800138000" not in exc.value.detail["message"]

    with pytest.raises(HTTPException) as exc:
        _ensure_platform_account_matches_user(user, None)
    assert exc.value.detail["code"] == "im_binding_identity_mismatch"

    _ensure_platform_account_matches_user(user, "+86 138-0013-8000")
