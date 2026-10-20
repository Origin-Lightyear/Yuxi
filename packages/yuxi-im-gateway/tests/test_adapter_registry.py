import pytest

from yuxi_im_gateway.adapters.base import Adapter
from yuxi_im_gateway.adapters.registry import adapter_fingerprint, build_adapter
from yuxi_im_gateway.models import BotConfig


def _bot(platform: str, creds: dict) -> BotConfig:
    return BotConfig(1, platform, "b", creds, "k", "a")


def test_fingerprint_changes_with_credentials():
    assert adapter_fingerprint(_bot("feishu", {"app_id": "1"})) != adapter_fingerprint(
        _bot("feishu", {"app_id": "2"})
    )


def test_build_adapter_rejects_unknown_platform():
    with pytest.raises(ValueError):
        build_adapter(_bot("slack", {}), on_inbound=lambda m: None)


def test_build_adapter_returns_adapter_for_feishu():
    adapter = build_adapter(
        _bot("feishu", {"app_id": "1", "app_secret": "s"}), on_inbound=lambda m: None
    )
    assert isinstance(adapter, Adapter)


def test_build_adapter_returns_adapter_for_dingtalk():
    adapter = build_adapter(
        _bot("dingtalk", {"client_id": "1", "client_secret": "s"}), on_inbound=lambda m: None
    )
    assert isinstance(adapter, Adapter)
