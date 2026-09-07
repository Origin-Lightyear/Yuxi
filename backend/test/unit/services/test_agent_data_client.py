"""Agent 数据客户端单元测试：请求 URL、员工上下文头与统一响应体解析。"""

from __future__ import annotations

import pytest
import httpx

from yuxi.services.agent_data_client import AgentDataClient
from yuxi.services.saas_client import SaasAPIError


def _client() -> AgentDataClient:
    return AgentDataClient(tenant_id=100, employee_id=200, base_url="http://tenant.test")


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def captured():
    """记录最近一次请求并返回预设响应的 monkeypatch 替身。"""
    holder = {"request": None, "response": {"code": 0, "msg": "ok", "data": {"id": 1}}}

    async def fake_request(self, method, url, json=None, params=None, headers=None):
        holder["request"] = {"method": method, "url": url, "json": json, "params": params, "headers": headers}
        return _FakeResponse(holder["response"])

    return holder, fake_request


@pytest.mark.asyncio
async def test_append_message_sends_context_headers_and_payload(monkeypatch, captured):
    holder, fake_request = captured
    holder["response"] = {
        "code": 0,
        "msg": "ok",
        "data": {"message": {"id": 33, "sequenceNo": 2}, "conversationVersion": 5},
    }
    monkeypatch.setattr("httpx.AsyncClient.request", fake_request)

    client = _client()
    data = await client.append_message(
        conversation_id=11,
        client_message_id="id-1",
        role="ASSISTANT",
        message_type="TEXT",
        content="回复内容",
        reply_to_message_id=10,
        consumed_score=12,
    )

    req = holder["request"]
    assert req["method"] == "POST"
    assert req["url"] == "http://tenant.test/api/v1/agent/conversations/11/messages"
    assert req["headers"]["X-Tenant-Id"] == "100"
    assert req["headers"]["X-Employee-Id"] == "200"
    assert req["json"]["clientMessageId"] == "id-1"
    assert req["json"]["replyToMessageId"] == 10
    assert req["json"]["consumedScore"] == 12
    assert data["conversationVersion"] == 5


@pytest.mark.asyncio
async def test_delete_endpoints_pass_version_as_query(monkeypatch, captured):
    holder, fake_request = captured
    monkeypatch.setattr("httpx.AsyncClient.request", fake_request)

    client = _client()
    await client.delete_conversation(9, version=4)

    req = holder["request"]
    assert req["method"] == "DELETE"
    assert req["url"] == "http://tenant.test/api/v1/agent/conversations/9"
    assert req["params"] == {"version": 4}


@pytest.mark.asyncio
async def test_nonzero_code_raises_saas_api_error(monkeypatch, captured):
    holder, fake_request = captured
    holder["response"] = {"code": 20309, "msg": "消息幂等冲突", "data": None}
    monkeypatch.setattr("httpx.AsyncClient.request", fake_request)

    with pytest.raises(SaasAPIError) as exc_info:
        await _client().get_timezones()
    assert exc_info.value.code == 20309


@pytest.mark.asyncio
async def test_create_schedule_payload_shape(monkeypatch, captured):
    holder, fake_request = captured
    monkeypatch.setattr("httpx.AsyncClient.request", fake_request)

    await _client().create_schedule(
        name="每日销售汇总",
        prompt="查询昨天销售订单",
        cron_expression="0 9 * * *",
        timezone="Asia/Shanghai",
    )

    req = holder["request"]
    assert req["url"] == "http://tenant.test/api/v1/agent/schedules"
    assert req["json"]["cronExpression"] == "0 9 * * *"
    assert req["json"]["timezone"] == "Asia/Shanghai"
    # enabled 未提供时不写入，由服务端默认 true
    assert "enabled" not in req["json"]
