from __future__ import annotations

import httpx
import pytest

from yuxi.services.saas_client import (
    SaasAPIError,
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


class _RecordingLogger:
    def __init__(self):
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        self.messages.append(str(message))

    def warning(self, message: str) -> None:
        self.messages.append(str(message))


@pytest.mark.asyncio
async def test_authenticate_employee_logs_masked_mobile(monkeypatch):
    import yuxi.services.saas_client as saas_client_module

    recorded = _RecordingLogger()
    monkeypatch.setattr(saas_client_module, "logger", recorded)

    async def fake_post(self, url, **kwargs):
        del self, kwargs
        assert url == "https://tenant.example.com/api/v1/agent/inner/auth"
        return _FakeResponse({"code": 0, "msg": "ok", "data": {"mobile": "13612345678", "tenants": [{}]}})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await SaasClient(tenant_url="https://tenant.example.com").authenticate_employee("13612345678", "password")

    assert result.mobile == "13612345678"
    logged = "\n".join(recorded.messages)
    assert "13612345678" not in logged
    assert "136****5678" in logged


@pytest.mark.asyncio
async def test_bind_channel_account_posts_employee_session(monkeypatch):
    """绑定接口用员工 AgentSession，并按 Tenant 约定提交大写 platform 与平台用户 ID。"""
    captured: dict = {}

    async def fake_post(self, url, **kwargs):
        del self
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _FakeResponse(
            {
                "code": 0,
                "msg": "ok",
                "data": {
                    "channelType": "FEISHU",
                    "bound": True,
                    "channelUserId": "ou_1",
                    "boundAt": "2026-11-06T10:20:30",
                },
            }
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    data = await SaasClient(tenant_url="https://tenant.example.com").bind_channel_account(
        "token-1", "FEISHU", "ou_1"
    )

    assert captured == {
        "url": "https://tenant.example.com/api/v1/agent/inner/employee/channel-bindings",
        "kwargs": {
            "headers": {"Authorization": "AgentSession token-1"},
            "json": {"channelType": "FEISHU", "channelUserId": "ou_1"},
        },
    }
    assert data["bound"] is True


@pytest.mark.asyncio
async def test_unbind_channel_account_deletes_platform_path(monkeypatch):
    """解绑走 DELETE + 平台类型路径参数，无请求体。"""
    captured: dict = {}

    async def fake_delete(self, url, **kwargs):
        del self
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _FakeResponse({"code": 0, "msg": "ok", "data": None})

    monkeypatch.setattr(httpx.AsyncClient, "delete", fake_delete)

    await SaasClient(tenant_url="https://tenant.example.com").unbind_channel_account("token-1", "DINGTALK")

    assert captured == {
        "url": "https://tenant.example.com/api/v1/agent/inner/employee/channel-bindings/DINGTALK",
        "kwargs": {"headers": {"Authorization": "AgentSession token-1"}},
    }


@pytest.mark.asyncio
async def test_list_channel_bindings_returns_tenant_array(monkeypatch):
    """查询绑定返回 Tenant 三个平台的数组，不做本地补全。"""
    rows = [{"channelType": "FEISHU", "bound": True, "channelUserId": "ou_1", "boundAt": "2026-11-06T10:20:30"}]

    async def fake_get(self, url, **kwargs):
        del self
        assert url == "https://tenant.example.com/api/v1/agent/inner/employee/channel-bindings"
        assert kwargs == {"headers": {"Authorization": "AgentSession token-1"}}
        return _FakeResponse({"code": 0, "msg": "ok", "data": rows})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await SaasClient(tenant_url="https://tenant.example.com").list_channel_bindings("token-1")

    assert result == rows


@pytest.mark.asyncio
async def test_bind_channel_account_raises_api_error_on_conflict(monkeypatch):
    """业务状态码非 0 必须抛出带 code 的异常，不能当作成功。"""

    async def fake_post(self, url, **kwargs):
        del self, url, kwargs
        return _FakeResponse({"code": 20316, "msg": "该平台账号已绑定其他员工，请先解绑", "data": None})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with pytest.raises(SaasAPIError) as exc_info:
        await SaasClient(tenant_url="https://tenant.example.com").bind_channel_account("token-1", "FEISHU", "ou_1")

    assert exc_info.value.code == 20316


@pytest.mark.asyncio
async def test_authenticate_channel_uses_admin_key(monkeypatch):
    """平台账号登录只用管理密钥，不发送员工 Token，并复用员工响应结构。"""
    import yuxi.services.saas_client as saas_client_module

    monkeypatch.setattr(saas_client_module.app_config, "tenant_admin_api_key", "admin-key")
    captured: dict = {}

    async def fake_post(self, url, **kwargs):
        del self
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _FakeResponse(
            {
                "code": 0,
                "msg": "ok",
                "data": {
                    "mobile": "13800000001",
                    "tenants": [
                        {
                            "tenantId": 1001,
                            "employeeId": 2001,
                            "employeeCode": "EMP001",
                            "agentSessionToken": "session-1",
                            "agentSessionExpiresAt": 1793974830,
                        }
                    ],
                },
            }
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = await SaasClient(tenant_url="https://tenant.example.com").authenticate_channel("FEISHU", "ou_1")

    assert captured == {
        "url": "https://tenant.example.com/api/v1/agent/inner/auth/channel",
        "kwargs": {
            "headers": {"X-Tenant-Admin-Key": "admin-key"},
            "json": {"channelType": "FEISHU", "channelUserId": "ou_1"},
        },
    }
    assert result.primary is not None
    assert result.primary.employee_code == "EMP001"


@pytest.mark.asyncio
async def test_authenticate_channel_fails_closed_without_admin_key(monkeypatch):
    """管理密钥未配置时不得发出请求，直接按 Tenant 认证失败处理。"""
    import yuxi.services.saas_client as saas_client_module

    monkeypatch.setattr(saas_client_module.app_config, "tenant_admin_api_key", "")

    async def fail_post(*_args, **_kwargs):
        raise AssertionError("缺少管理密钥时不应发起 HTTP 请求")

    monkeypatch.setattr(httpx.AsyncClient, "post", fail_post)

    with pytest.raises(SaasAPIError) as exc_info:
        await SaasClient(tenant_url="https://tenant.example.com").authenticate_channel("FEISHU", "ou_1")

    assert exc_info.value.code == 20317
