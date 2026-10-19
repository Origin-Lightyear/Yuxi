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

    with pytest.raises(RuntimeError):
        await adapter._post_webhook(_WEBHOOK, {"msgtype": "text", "text": {"content": "hi"}})

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

    with pytest.raises(RuntimeError):
        await adapter._post_webhook(_WEBHOOK, {"msgtype": "text", "text": {"content": "hi"}})

    await adapter.stop()
