from __future__ import annotations

import httpx
import pytest

from yuxi.services.saas_client import (
    SaasClient,
    _parse_employee_permission_department_id,
    _parse_tenant_config_data,
    _parse_tenant_employee,
)


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
            "departmentId": 1,
            "agentSessionToken": "sensitive-session-token",
        }
    )

    assert employee.department_id == 1
    assert "sensitive-session-token" not in repr(employee)


def test_parse_employee_permission_reads_department_from_permission_config():
    assert _parse_employee_permission_department_id({"permissionConfig": {"departmentId": 1}}) == 1
    assert _parse_employee_permission_department_id({"departmentId": 2}) == 2
    assert _parse_employee_permission_department_id({}) is None


@pytest.mark.asyncio
async def test_get_employee_permission_uses_inner_endpoint(monkeypatch):
    captured: dict = {}

    async def fake_get(self, url, **kwargs):
        del self
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _FakeResponse({"permissionConfig": {"departmentId": 1}})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    payload = await SaasClient(tenant_url="https://tenant.example.com").get_employee_permission(1, 2)

    assert captured == {
        "url": "https://tenant.example.com/api/v1/agent/inner/employee-permission",
        "kwargs": {"params": {"tenantId": 1, "employeeId": 2}},
    }
    assert _parse_employee_permission_department_id(payload) == 1
