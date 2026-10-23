import httpx
import pytest

from yuxi.services import speech_service
from yuxi.services.speech_service import SpeechError, strip_rich_tags, transcribe_speech


def test_strip_rich_tags_removes_sensevoice_tags():
    assert strip_rich_tags("<|zh|><|NEUTRAL|><|Speech|><|withitn|>今天几号？") == "今天几号？"


def test_strip_rich_tags_keeps_plain_text():
    assert strip_rich_tags("今天几号？") == "今天几号？"


@pytest.mark.asyncio
async def test_transcribe_speech_returns_stripped_text(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["content-type"] == "audio/wav"
        return httpx.Response(200, json={"text": "<|zh|><|NEUTRAL|><|Speech|><|withitn|>你好"})

    monkeypatch.setattr(
        speech_service,
        "build_asr_client",
        lambda url: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=url),
    )
    assert await transcribe_speech(b"\x00" * 128, server_url="http://asr:8100") == "你好"


@pytest.mark.asyncio
async def test_transcribe_speech_maps_timeout(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(
        speech_service,
        "build_asr_client",
        lambda url: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=url),
    )
    with pytest.raises(SpeechError) as exc:
        await transcribe_speech(b"\x00" * 128, server_url="http://asr:8100")
    assert exc.value.reason == "timeout"


@pytest.mark.asyncio
async def test_transcribe_speech_maps_unavailable(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    monkeypatch.setattr(
        speech_service,
        "build_asr_client",
        lambda url: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=url),
    )
    with pytest.raises(SpeechError) as exc:
        await transcribe_speech(b"\x00" * 128, server_url="http://asr:8100")
    assert exc.value.reason == "unavailable"


@pytest.mark.asyncio
async def test_transcribe_speech_maps_empty_result(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "<|zh|><|NEUTRAL|><|Speech|><|withitn|>"})

    monkeypatch.setattr(
        speech_service,
        "build_asr_client",
        lambda url: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=url),
    )
    with pytest.raises(SpeechError) as exc:
        await transcribe_speech(b"\x00" * 128, server_url="http://asr:8100")
    assert exc.value.reason == "empty"
