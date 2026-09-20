from __future__ import annotations

import httpx
import pytest

from yuxi.services.saas_client import SaasClient, _parse_tenant_config_data, _parse_tenant_employee


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


@pytest.mark.asyncio
async def test_list_mcp_servers_uses_tenant_endpoints_route(monkeypatch):
    captured: dict = {}

    async def fake_get(self, url, **kwargs):
        del self
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _FakeResponse(
            {
                "code": 0,
                "msg": "ok",
                "data": {
                    "tenantId": 1,
                    "mcpServers": [
                        {
                            "instanceId": "runtime-1",
                            "url": "https://mcp.example.com",
                        }
                    ],
                },
            }
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    servers = await SaasClient(tenant_url="https://tenant.example.com").list_mcp_servers(tenant_id=1)

    assert captured == {
        "url": "https://tenant.example.com/api/v1/agent/inner/system-settings/tenants/1/endpoints",
        "kwargs": {},
    }
    assert servers == [{"instanceId": "runtime-1", "url": "https://mcp.example.com"}]


def test_parse_tenant_config_accepts_null_optional_numbers():
    result = _parse_tenant_config_data(
        {
            "id": 1,
            "llmKey": "key",
            "llmUrl": "https://llm.example.com",
            "status": 1,
            "empNum": None,
            "authEndTime": "2027-09-03T04:35:24",
            "version": None,
        }
    )

    assert result.emp_num == 0
    assert result.version == 0


def test_parse_tenant_employee_repr_hides_agent_session_token():
    employee = _parse_tenant_employee(
        {
            "tenantId": 1,
            "employeeId": 2,
            "agentSessionToken": "sensitive-session-token",
        }
    )

    assert "sensitive-session-token" not in repr(employee)
