"""IM 账号绑定服务：纯函数单元测试（授权 URL 构建与 state 哈希，不连库）。"""

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
