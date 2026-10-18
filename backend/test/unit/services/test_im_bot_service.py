"""IM 机器人配置服务：纯逻辑部分的单元测试（凭证加解密、网关配置）。"""

from types import SimpleNamespace

import pytest

import yuxi.services.im_bot_service as im_bot_service
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


def _fake_bot(**overrides) -> SimpleNamespace:
    """构造仅含网关配置所需字段的轻量机器人替身。"""
    fields = {
        "id": 1,
        "platform": "feishu",
        "name": "demo",
        "enabled": True,
        "api_key_id": 10,
        "api_key_cipher": "cipher",
        "credentials_encrypted": "enc",
        "agent_slug": "default-chatbot",
        "allowed_users": [],
        "require_mention": True,
        "group_sessions_per_user": True,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.mark.asyncio
async def test_gateway_configs_isolates_one_bad_cipher(monkeypatch):
    """单个机器人密文解密失败时只跳过该机器人；停用与已吊销 Key 的机器人同样被过滤。"""
    good = _fake_bot(id=1, name="good")
    bad = _fake_bot(id=2, name="bad", credentials_encrypted="enc-bad")
    disabled = _fake_bot(id=3, name="disabled", enabled=False)
    revoked = _fake_bot(id=4, name="revoked", api_key_id=None)

    async def fake_list(db):
        return [good, bad, disabled, revoked]

    def fake_decrypt_credentials(platform, cipher):
        if cipher == "enc-bad":
            raise ValueError("corrupted cipher")
        return {"app_id": "a", "app_secret": "b"}

    monkeypatch.setattr(im_bot_service, "_list", fake_list)
    monkeypatch.setattr(im_bot_service, "decrypt_credentials", fake_decrypt_credentials)
    monkeypatch.setattr(im_bot_service, "decrypt_secret", lambda cipher: "plain-key")

    configs = await im_bot_service.get_gateway_configs(object())

    assert [config["id"] for config in configs] == [1]
    assert configs[0]["name"] == "good"
    assert configs[0]["api_key"] == "plain-key"
