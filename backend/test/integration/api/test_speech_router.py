import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

WAV_HEADER = b"RIFF$\x00\x00\x00WAVEfmt "


async def test_transcribe_rejects_non_wav(test_client, admin_headers):
    response = await test_client.post(
        "/api/chat/speech/transcribe",
        content=b"not audio",
        headers={**admin_headers, "Content-Type": "audio/wav"},
    )
    assert response.status_code == 400


async def test_transcribe_rejects_oversized_body(test_client, admin_headers):
    response = await test_client.post(
        "/api/chat/speech/transcribe",
        content=WAV_HEADER + b"\x00" * (6 * 1024 * 1024),
        headers={**admin_headers, "Content-Type": "audio/wav"},
    )
    assert response.status_code == 413


async def test_transcribe_requires_auth(test_client):
    response = await test_client.post(
        "/api/chat/speech/transcribe",
        content=WAV_HEADER,
        headers={"Content-Type": "audio/wav"},
    )
    assert response.status_code == 401


async def test_speech_health_reports_availability(test_client, admin_headers):
    response = await test_client.get("/api/system/speech/health", headers=admin_headers)
    assert response.status_code == 200
    assert isinstance(response.json()["available"], bool)
