from __future__ import annotations

import httpx
import pytest

from yuxi.agents.mcp.saas_credentials import SaasMCPAuth


@pytest.mark.asyncio
async def test_saas_mcp_auth_fetches_and_forwards_fresh_headers():
    calls = []
    credential_headers = {
        "Authorization": "signature-1",
        "Tenant": "tenant-1",
        "Mcp-Instance-Id": "instance-1",
        "Timestamp": "1700000000",
        "Expires-At": "1700000060",
        "Nonce": "nonce-1",
    }

    async def request_credentials():
        calls.append(True)
        return credential_headers

    auth = SaasMCPAuth(request_credentials)
    request = httpx.Request("POST", "https://mcp.example.com/mcp")

    async for signed_request in auth.async_auth_flow(request):
        assert signed_request is request
        for key, value in credential_headers.items():
            assert signed_request.headers[key] == value

    assert calls == [True]


@pytest.mark.asyncio
async def test_saas_mcp_auth_requests_new_headers_for_each_http_request():
    responses = iter(
        [
            {
                "Authorization": "signature-1",
                "Tenant": "tenant-1",
                "Mcp-Instance-Id": "instance-1",
                "Timestamp": "1",
                "Expires-At": "2",
                "Nonce": "nonce-1",
            },
            {
                "Authorization": "signature-2",
                "Tenant": "tenant-1",
                "Mcp-Instance-Id": "instance-1",
                "Timestamp": "3",
                "Expires-At": "4",
                "Nonce": "nonce-2",
            },
        ]
    )

    async def request_credentials():
        return next(responses)

    auth = SaasMCPAuth(request_credentials)
    first = httpx.Request("POST", "https://mcp.example.com/mcp")
    second = httpx.Request("POST", "https://mcp.example.com/mcp")

    async for _ in auth.async_auth_flow(first):
        pass
    async for _ in auth.async_auth_flow(second):
        pass

    assert first.headers["Nonce"] == "nonce-1"
    assert second.headers["Nonce"] == "nonce-2"


def test_saas_mcp_auth_rejects_incomplete_credential_headers():
    async def request_credentials():
        return {"Tenant": "tenant-1"}

    auth = SaasMCPAuth(request_credentials)

    with pytest.raises(RuntimeError, match="MCP 凭证缺少必需 Header"):
        import asyncio

        asyncio.run(auth._get_headers())
