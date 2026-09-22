"""SaaS Platform 与 Tenant 内部 API 客户端。

对接 AGENT_CLIENT_API.md（内部接口统一前缀 /api/v1/agent/inner）：
- §2.1 POST /api/v1/agent/inner/auth                 员工鉴权 → 返回多租户 + llmKey/llmUrl
- §2.4 POST /api/v1/agent/inner/employee-llm-config  员工 LLM 配置
- Platform GET /inner/tenant/config/{id}             租户配置 → 返回 mcpUrl

全部接口使用统一响应体 {code, msg, data}，code=0 表示成功。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from yuxi.utils.logging_config import logger

DEFAULT_TIMEOUT_SECONDS = 15.0


class SaasAPIError(Exception):
    """SaaS API 调用异常。"""

    def __init__(self, code: int, msg: str, status_code: int | None = None):
        self.code = code
        self.msg = msg
        self.status_code = status_code
        super().__init__(f"[{code}] {msg}")


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _check_response(payload: dict[str, Any]) -> dict[str, Any]:
    code = payload.get("code")
    if code != 0:
        msg = payload.get("msg", "unknown error")
        raise SaasAPIError(code=code, msg=msg)
    return payload.get("data") or {}


def _build_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}{path}"


@dataclass(frozen=True, slots=True)
class TenantEmployee:
    """员工在单个租户下的信息。"""

    tenant_id: int
    tenant_name: str
    employee_id: int
    employee_code: str
    employee_name: str
    department_name: str
    enabled: int
    llm_key: str
    llm_url: str
    agent_session_token: str = field(repr=False)
    agent_session_expires_at: str
    department_id: int | None = None


@dataclass(frozen=True, slots=True)
class EmployeeAuthResult:
    """4.2 POST /tenant/auth 成功响应。"""

    mobile: str
    tenants: list[TenantEmployee] = field(default_factory=list)

    @property
    def primary(self) -> TenantEmployee | None:
        return self.tenants[0] if self.tenants else None


@dataclass(frozen=True, slots=True)
class TenantConfigResult:
    """3.2 GET /inner/tenant/config/{id} 成功响应。"""

    id: int
    llm_key: str
    llm_url: str
    mcp_url: str
    status: int
    emp_num: int
    auth_end_time: str
    version: int


def _parse_tenant_employee(data: dict[str, Any]) -> TenantEmployee:
    return TenantEmployee(
        tenant_id=int(data.get("tenantId", 0)),
        tenant_name=str(data.get("tenantName") or ""),
        employee_id=int(data.get("employeeId", 0)),
        employee_code=str(data.get("employeeCode") or ""),
        employee_name=str(data.get("employeeName") or ""),
        department_name=str(data.get("departmentName") or ""),
        enabled=int(data.get("enabled", 0)),
        llm_key=str(data.get("llmKey") or ""),
        llm_url=str(data.get("llmUrl") or ""),
        agent_session_token=str(data.get("agentSessionToken") or ""),
        agent_session_expires_at=str(data.get("agentSessionExpiresAt") or ""),
        department_id=int(data["departmentId"]) if data.get("departmentId") is not None else None,
    )


def _parse_employee_auth(data: dict[str, Any]) -> EmployeeAuthResult:
    tenants_raw = data.get("tenants") or []
    return EmployeeAuthResult(
        mobile=str(data.get("mobile") or ""),
        tenants=[_parse_tenant_employee(t) for t in tenants_raw if isinstance(t, dict)],
    )


def _parse_employee_permission_department_id(data: dict[str, Any]) -> int | None:
    """从员工权限快照读取租户部门 ID。"""
    permission_config = data.get("permissionConfig")
    if isinstance(permission_config, dict) and permission_config.get("departmentId") is not None:
        return int(permission_config["departmentId"])
    if data.get("departmentId") is not None:
        return int(data["departmentId"])
    return None


def _parse_tenant_config_data(data: dict[str, Any]) -> TenantConfigResult:
    return TenantConfigResult(
        id=int(data.get("id") or 0),
        llm_key=str(data.get("llmKey") or ""),
        llm_url=str(data.get("llmUrl") or ""),
        mcp_url=str(data.get("mcpUrl") or ""),
        status=int(data.get("status") or 0),
        emp_num=int(data.get("empNum") or 0),
        auth_end_time=str(data.get("authEndTime") or ""),
        version=int(data.get("version") or 0),
    )


class SaasClient:
    """SaaS API 客户端。

    环境变量配置：
    - SAAS_ENABLED:          是否启用 SaaS 模式
    - TENANT_SERVICE_URL:    Tenant 服务地址（默认 http://10.69.188.230:8002）
    - PLATFORM_SERVICE_URL:  Platform 服务地址（默认 http://10.69.188.230:8001）
    """

    def __init__(
        self,
        tenant_url: str | None = None,
        platform_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self._tenant_url = (tenant_url or _env("TENANT_SERVICE_URL") or "http://10.69.188.230:8002").rstrip("/")
        self._platform_url = (platform_url or _env("PLATFORM_SERVICE_URL") or "http://10.69.188.230:8001").rstrip("/")
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        return _env("SAAS_ENABLED").lower() in ("true", "1")

    async def authenticate_employee(self, mobile: str, password: str) -> EmployeeAuthResult:
        """员工鉴权（§2.1 POST /api/v1/agent/inner/auth）。不再需要 tenantId。

        Raises:
            SaasAPIError: API 返回非 0 code。
            httpx.HTTPError: 网络/HTTP 错误。
        """
        url = _build_url(self._tenant_url, "/api/v1/agent/inner/auth")
        body = {"mobile": mobile, "password": password}
        logger.info(f"SaaS employee auth request: mobile={mobile}")

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(url, json=body)
            response.raise_for_status()
            payload = response.json()

        data = _check_response(payload)
        result = _parse_employee_auth(data)
        logger.info(f"SaaS employee auth success: mobile={result.mobile}, tenants={len(result.tenants)}")
        return result

    async def get_tenant_config(self, tenant_id: int) -> TenantConfigResult:
        """查询租户内部配置（3.2 GET /inner/tenant/config/{id}）。"""
        url = _build_url(self._platform_url, f"/inner/tenant/config/{tenant_id}")
        logger.info(f"SaaS fetch tenant config: tenant_id={tenant_id}")

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()

        data = _check_response(payload)
        result = _parse_tenant_config_data(data)
        logger.info(f"SaaS tenant config loaded: id={result.id}, version={result.version}, mcp_url={result.mcp_url}")
        return result

    async def get_employee_llm_config(self, tenant_id: int, employee_id: int) -> dict[str, Any]:
        """读取员工 LLM 配置（§2.4 POST /api/v1/agent/inner/employee-llm-config）。"""
        url = _build_url(self._tenant_url, "/api/v1/agent/inner/employee-llm-config")
        body = {"tenantId": tenant_id, "employeeId": employee_id}

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(url, json=body)
            response.raise_for_status()
            payload = response.json()

        data = _check_response(payload)
        logger.info(f"SaaS employee llm config loaded: tenant_id={tenant_id}, employee_id={employee_id}")
        return data

    async def get_employee_permission(self, tenant_id: int, employee_id: int) -> dict[str, Any]:
        """读取员工权限快照，获取租户部门 ID。"""
        url = _build_url(self._tenant_url, "/api/v1/agent/inner/employee-permission")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(url, params={"tenantId": tenant_id, "employeeId": employee_id})
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise SaasAPIError(code=50000, msg="员工权限响应格式错误")
        return payload

    async def list_mcp_servers(self, tenant_id: int) -> list[dict[str, Any]]:
        """读取当前员工可用 MCP 列表及其 Runtime instanceId。"""
        url = _build_url(self._tenant_url, f"/api/v1/agent/inner/system-settings/tenants/{tenant_id}/endpoints")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
        data = _check_response(payload)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        for key in ("mcps", "mcpServers", "servers", "items"):
            values = data.get(key) if isinstance(data, dict) else None
            if isinstance(values, list):
                return [item for item in values if isinstance(item, dict)]
        return []

    async def request_mcp_credentials(self, agent_session_token: str, mcp_instance_id: str) -> dict[str, str]:
        """申请一组只能使用一次的 MCP Runtime 请求 Header。"""
        url = _build_url(self._tenant_url, "/api/v1/agent/inner/mcp-credentials")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"AgentSession {agent_session_token}"},
                json={"mcpInstanceId": mcp_instance_id},
            )
            response.raise_for_status()
            payload = response.json()
        data = _check_response(payload)
        headers = data.get("headers") if isinstance(data, dict) else None
        if not isinstance(headers, dict):
            raise SaasAPIError(code=50000, msg="MCP 凭证响应缺少 headers")
        return {str(key): str(value) for key, value in headers.items()}

    async def logout_agent_session(self, agent_session_token: str) -> None:
        """注销 Tenant AgentSession。"""
        url = _build_url(self._tenant_url, "/api/v1/agent/inner/logout")
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(url, headers={"Authorization": f"AgentSession {agent_session_token}"})
            response.raise_for_status()


_saas_client: SaasClient | None = None


def get_saas_client() -> SaasClient:
    global _saas_client
    if _saas_client is None:
        _saas_client = SaasClient()
    return _saas_client
