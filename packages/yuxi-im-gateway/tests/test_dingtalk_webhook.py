import httpx
import pytest
import respx

from yuxi_im_gateway.adapters.dingtalk import DingTalkAdapter
from yuxi_im_gateway.models import BotConfig

_WEBHOOK = "https://example.com/session-webhook"


def _adapter() -> DingTalkAdapter:
    bot = BotConfig(1, "dingtalk", "b", {"client_id": "1", "client_secret": "s"}, "k", "a")
    adapter = DingTalkAdapter(bot, on_inbound=lambda m: None)
    adapter._http = httpx.AsyncClient()
    return adapter


@pytest.mark.asyncio
@respx.mock
async def test_post_webhook_raises_on_nonzero_errcode_with_http_200():
    adapter = _adapter()
    respx.post(_WEBHOOK).mock(
        return_value=httpx.Response(200, json={"errcode": 310000, "errmsg": "webhook expired"})
    )

    with pytest.raises(RuntimeError) as excinfo:
        await adapter._post_webhook(_WEBHOOK, {"msgtype": "text", "text": {"content": "hi"}})

    assert "310000" in str(excinfo.value)
    await adapter.stop()


@pytest.mark.asyncio
@respx.mock
async def test_post_webhook_succeeds_on_zero_errcode():
    adapter = _adapter()
    respx.post(_WEBHOOK).mock(return_value=httpx.Response(200, json={"errcode": 0, "errmsg": "ok"}))

    await adapter._post_webhook(_WEBHOOK, {"msgtype": "text", "text": {"content": "hi"}})

    await adapter.stop()


@pytest.mark.asyncio
@respx.mock
async def test_post_webhook_treats_non_json_2xx_as_failure():
    adapter = _adapter()
    respx.post(_WEBHOOK).mock(return_value=httpx.Response(200, text="<html>gateway</html>"))

    with pytest.raises(RuntimeError) as excinfo:
        await adapter._post_webhook(_WEBHOOK, {"msgtype": "text", "text": {"content": "hi"}})

    assert "非 JSON" in str(excinfo.value)
    await adapter.stop()


@pytest.mark.asyncio
@respx.mock
async def test_post_webhook_http_error_does_not_leak_session_secret():
    adapter = _adapter()
    secret_url = f"{_WEBHOOK}?session=WEBHOOK_SECRET"
    respx.post(secret_url).mock(return_value=httpx.Response(500, text="upstream error"))

    with pytest.raises(RuntimeError) as excinfo:
        await adapter._post_webhook(secret_url, {"msgtype": "text", "text": {"content": "hi"}})

    message = str(excinfo.value)
    assert "500" in message
    assert "WEBHOOK_SECRET" not in message
    assert _WEBHOOK not in message
    await adapter.stop()


@pytest.mark.asyncio
@respx.mock
async def test_upload_media_failure_does_not_leak_access_token():
    adapter = _adapter()
    adapter._token = "SECRET_TOKEN"
    adapter._token_expire = float("inf")
    respx.post("https://oapi.dingtalk.com/media/upload").mock(return_value=httpx.Response(500, text="boom"))

    with pytest.raises(RuntimeError) as excinfo:
        await adapter._upload_media("a.xlsx", b"data")

    assert "SECRET_TOKEN" not in str(excinfo.value)
    assert "500" in str(excinfo.value)
    await adapter.stop()
