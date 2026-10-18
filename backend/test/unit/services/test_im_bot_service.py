"""IM 机器人配置服务：纯逻辑部分的单元测试（凭证加解密）。"""

import pytest

from yuxi.services.im_bot_service import decrypt_credentials, encrypt_credentials


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    monkeypatch.setenv("IM_CREDENTIAL_ENCRYPTION_KEY", "unit-test-key")


def test_dingtalk_credentials_round_trip():
    cipher = encrypt_credentials("dingtalk", {"client_id": "a", "client_secret": "b"})
    assert decrypt_credentials("dingtalk", cipher) == {"client_id": "a", "client_secret": "b"}


def test_feishu_credentials_default_domain():
    cipher = encrypt_credentials("feishu", {"app_id": "a", "app_secret": "b"})
    assert decrypt_credentials("feishu", cipher)["domain"] == "feishu"


def test_credentials_require_fields():
    with pytest.raises(ValueError):
        encrypt_credentials("feishu", {"app_id": "a"})
    with pytest.raises(ValueError):
        encrypt_credentials("dingtalk", {"client_id": "a"})


def test_unknown_platform_rejected():
    with pytest.raises(ValueError):
        encrypt_credentials("slack", {})
