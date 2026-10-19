import json

import httpx
import pytest
import respx

from yuxi_im_gateway.config import GatewaySettings
from yuxi_im_gateway.models import BotConfig
from yuxi_im_gateway.yuxi_client import YuxiClient


def _settings() -> GatewaySettings:
    return GatewaySettings(api_base="http://yuxi", gateway_token="tok", poll_interval=30)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_bot_configs_sends_token_header():
    route = respx.get("http://yuxi/api/internal/im-gateway/config").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "data": [{"id": 1, "platform": "feishu", "api_key": "yxkey_x", "agent_slug": "a"}],
            },
        )
    )
    client = YuxiClient(_settings())
    bots = await client.fetch_bot_configs()
    assert route.calls.last.request.headers["X-IM-Gateway-Token"] == "tok"
    assert bots[0].id == 1


@pytest.mark.asyncio
@respx.mock
async def test_submit_message_uses_bot_api_key():
    route = respx.post("http://yuxi/api/agent-invocation/channel/messages").mock(
        return_value=httpx.Response(
            200,
            json={"kind": "run", "run_id": "r1", "stream_url": "/api/agent/runs/r1/events"},
        )
    )
    client = YuxiClient(_settings())
    bot = BotConfig(1, "feishu", "b", {}, "yxkey_z", "a")
    result = await client.submit_message(
        bot=bot,
        thread_id=None,
        chat_id="c1",
        sender_id="ou_a",
        message_id="m1",
        text="hi",
        request_id="m1",
    )
    assert result["run_id"] == "r1"
    auth = route.calls.last.request.headers["Authorization"]
    assert auth == "Bearer yxkey_z"
    body = route.calls.last.request.content
    assert json.loads(body)["channel"] == "feishu"


@pytest.mark.asyncio
@respx.mock
async def test_stream_run_yields_frames_from_shared_parser():
    route = respx.get("http://yuxi/api/agent/runs/r1/events").mock(
        return_value=httpx.Response(
            200,
            content=(
                'id: 1-0\nevent: messages\ndata: {"payload":\n'
                'data: {"status":"loading"}}\n\n'
                ": heartbeat\n\n"
                'event: end\ndata: {"payload":{"status":"completed"}}\n\n'
            ),
        )
    )
    client = YuxiClient(_settings())
    batches = [batch async for batch in client.stream_run("/api/agent/runs/r1/events", "yxkey_z")]
    assert batches == [
        [{"id": "1-0", "event": "messages", "data": '{"payload":\n{"status":"loading"}}'}],
        [{"event": "end", "data": '{"payload":{"status":"completed"}}'}],
    ]
    assert route.calls.last.request.headers["Authorization"] == "Bearer yxkey_z"
