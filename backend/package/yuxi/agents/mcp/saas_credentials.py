"""按 Tenant AgentSession 获取一次性 MCP 请求凭证。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

import httpx

from yuxi.services.saas_client import get_saas_client
from yuxi.services.saas_session import get_saas_agent_session

REQUIRED_MCP_HEADERS = frozenset(
    {"authorization", "tenant", "mcp-instance-id", "timestamp", "expires-at", "nonce"}
)


class SaasMCPAuth(httpx.Auth):
    """让每个 MCP HTTP 请求都申请并转发一组新凭证。"""

    requires_request_body = False

    def __init__(self, request_credentials: Callable[[], Awaitable[Mapping[str, str]]]):
        self._request_credentials = request_credentials

    async def _get_headers(self) -> dict[str, str]:
        headers = {str(key): str(value) for key, value in (await self._request_credentials()).items()}
        missing = REQUIRED_MCP_HEADERS - {key.lower() for key in headers}
        if missing:
            raise RuntimeError(f"MCP 凭证缺少必需 Header: {', '.join(sorted(missing))}")
        return headers

    async def async_auth_flow(self, request: httpx.Request):
        for key, value in (await self._get_headers()).items():
            request.headers[key] = value
        yield request


async def create_saas_mcp_auth(uid: str) -> SaasMCPAuth:
    """从当前用户的 Tenant Session 创建请求级 MCP 凭证认证器。"""
    session = await get_saas_agent_session(uid)
    if session is None:
        raise RuntimeError("SaaS AgentSession 不存在或已过期，请重新登录")

    async def request_credentials() -> Mapping[str, str]:
        result = await get_saas_client().request_mcp_credentials(session.token, session.mcp_instance_id)
        return result

    return SaasMCPAuth(request_credentials)
