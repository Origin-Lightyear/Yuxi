"""嵌入模型降级机制单元测试：未注册 spec 解析降级与调用失败降级。"""

from __future__ import annotations

import pytest

from yuxi.models import embed
from yuxi.models.embed import OtherEmbedding


def _patch_cache(monkeypatch, registered: set[str]):
    def fake_get_model_info(spec):
        if spec in registered:
            from types import SimpleNamespace

            return SimpleNamespace(
                spec=spec,
                model_id=spec.split(":")[-1],
                model_type="embedding",
                provider_type="openai",
                base_url=f"http://base/{spec}",
                api_key="",
                dimension=1024,
                batch_size=40,
                display_name=spec,
            )
        return None

    monkeypatch.setattr(embed.model_cache, "get_model_info", fake_get_model_info)


def test_resolve_keeps_registered_spec(monkeypatch):
    _patch_cache(monkeypatch, {"siliconflow-cn:Pro/BAAI/bge-m3", "ollama:bge-m3"})
    assert embed.resolve_embedding_model_spec("siliconflow-cn:Pro/BAAI/bge-m3") == "siliconflow-cn:Pro/BAAI/bge-m3"


def test_resolve_falls_back_for_unregistered_spec(monkeypatch):
    _patch_cache(monkeypatch, {"ollama:bge-m3"})
    from yuxi.config.app import config as app_config

    monkeypatch.setattr(app_config, "embed_fallback_model", "ollama:bge-m3")
    assert embed.resolve_embedding_model_spec("unknown:model") == "ollama:bge-m3"


def test_select_fallback_model_returns_none_when_same_spec(monkeypatch):
    model = OtherEmbedding(
        model="bge-m3", spec="ollama:bge-m3", base_url="http://x", api_key="", fallback_spec="ollama:bge-m3"
    )
    _patch_cache(monkeypatch, {"ollama:bge-m3"})
    assert model._select_fallback_model() is None


class _FakeFallback:
    spec = "ollama:bge-m3"

    def encode(self, message):
        return [[0.1, 0.2]]

    async def aencode(self, message):
        return [[0.1, 0.2]]


def test_encode_falls_back_to_fallback_model_on_request_error(monkeypatch):
    monkeypatch.setattr(embed, "select_embedding_model", lambda spec: _FakeFallback())

    def boom(*args, **kwargs):
        raise __import__("requests").ConnectionError("down")

    monkeypatch.setattr(embed.requests, "post", boom)
    model = OtherEmbedding(
        model="Pro/BAAI/bge-m3",
        spec="siliconflow-cn:Pro/BAAI/bge-m3",
        base_url="http://x",
        api_key="",
        fallback_spec="ollama:bge-m3",
    )
    result = model.encode(["text"])
    assert result == [[0.1, 0.2]]


def test_encode_raises_without_fallback_model(monkeypatch):
    def boom(*args, **kwargs):
        raise __import__("requests").ConnectionError("down")

    monkeypatch.setattr(embed.requests, "post", boom)
    model = OtherEmbedding(
        model="Pro/BAAI/bge-m3", spec="siliconflow-cn:Pro/BAAI/bge-m3", base_url="http://x", api_key=""
    )
    with pytest.raises(ValueError, match="Embedding request failed"):
        model.encode(["text"])
