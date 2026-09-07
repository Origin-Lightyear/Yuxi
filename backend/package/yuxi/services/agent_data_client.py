"""Agent Client 数据接口客户端。

对接 AGENT_CLIENT_API.md §3-§6：目录、会话、消息、定时任务、时区字典。
所有请求携带员工上下文请求头 X-Tenant-Id / X-Employee-Id（§1.2），
响应统一为 {code, msg, data}，code=0 表示成功（§1.1）。
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from yuxi.services.saas_client import SaasAPIError

DEFAULT_TIMEOUT_SECONDS = 15.0


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


class AgentDataClient:
    """Agent 数据接口客户端（按租户 + 员工维度实例化）。

    环境变量配置：
    - TENANT_SERVICE_URL: Tenant 服务地址（默认 http://10.69.188.230:8002）
    """

    def __init__(
        self,
        tenant_id: int,
        employee_id: int,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self._tenant_id = tenant_id
        self._employee_id = employee_id
        self._base_url = (base_url or _env("TENANT_SERVICE_URL") or "http://10.69.188.230:8002").rstrip("/")
        self._timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "X-Tenant-Id": str(self._tenant_id),
            "X-Employee-Id": str(self._employee_id),
        }

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """发起请求并校验统一响应体，返回 data 部分。

        Raises:
            SaasAPIError: API 返回非 0 code。
            httpx.HTTPError: 网络/HTTP 错误。
        """
        url = f"{self._base_url}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.request(method, url, json=json_body, params=params, headers=self._headers)
            response.raise_for_status()
            payload = response.json()

        code = payload.get("code")
        if code != 0:
            raise SaasAPIError(code=code, msg=payload.get("msg", "unknown error"))
        return payload.get("data")

    # ------------------------------------------------------------------
    # 目录（§3）
    # ------------------------------------------------------------------

    async def get_directories_tree(self) -> list[dict[str, Any]]:
        """查询完整目录树（§3.1），一次返回全部目录和会话摘要。"""
        return await self._request("GET", "/api/v1/agent/directories/tree") or []

    async def create_directory(self, name: str) -> dict[str, Any]:
        """创建目录（§3.2）。"""
        return await self._request("POST", "/api/v1/agent/directories", json_body={"name": name})

    async def rename_directory(self, directory_id: int, name: str, version: int) -> dict[str, Any]:
        """重命名目录（§3.3）。"""
        return await self._request(
            "PATCH", f"/api/v1/agent/directories/{directory_id}", json_body={"name": name, "version": version}
        )

    async def reorder_directories(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """批量调整目录顺序（§3.4），任一版本冲突时整批回滚。"""
        return await self._request("PATCH", "/api/v1/agent/directories/order", json_body={"items": items})

    async def delete_directory(self, directory_id: int, version: int) -> dict[str, Any]:
        """删除目录（§3.5），目录内会话迁移到默认目录。"""
        return await self._request("DELETE", f"/api/v1/agent/directories/{directory_id}", params={"version": version})

    # ------------------------------------------------------------------
    # 会话（§4）
    # ------------------------------------------------------------------

    async def create_conversation(self, title: str | None = None, directory_id: int | None = None) -> dict[str, Any]:
        """创建会话（§4.1），directoryId 为空时使用默认目录。"""
        body: dict[str, Any] = {"title": title}
        if directory_id is not None:
            body["directoryId"] = directory_id
        return await self._request("POST", "/api/v1/agent/conversations", json_body=body)

    async def get_conversation(self, conversation_id: int) -> dict[str, Any]:
        """查询会话详情（§4.2）。"""
        return await self._request("GET", f"/api/v1/agent/conversations/{conversation_id}")

    async def update_conversation(
        self,
        conversation_id: int,
        version: int,
        title: str | None = None,
        directory_id: int | None = None,
    ) -> dict[str, Any]:
        """修改标题或移动目录（§4.3）。"""
        body: dict[str, Any] = {"version": version}
        if title is not None:
            body["title"] = title
        if directory_id is not None:
            body["directoryId"] = directory_id
        return await self._request("PATCH", f"/api/v1/agent/conversations/{conversation_id}", json_body=body)

    async def delete_conversation(self, conversation_id: int, version: int) -> dict[str, Any]:
        """删除会话（§4.4），会话及全部消息在同一事务内硬删除。"""
        return await self._request(
            "DELETE", f"/api/v1/agent/conversations/{conversation_id}", params={"version": version}
        )

    # ------------------------------------------------------------------
    # 消息（§5）
    # ------------------------------------------------------------------

    async def get_messages(self, conversation_id: int) -> list[dict[str, Any]]:
        """查询会话全部消息（§5.1），按 sequenceNo ASC。"""
        return await self._request("GET", f"/api/v1/agent/conversations/{conversation_id}/messages") or []

    async def append_message(
        self,
        conversation_id: int,
        client_message_id: str,
        role: str,
        message_type: str,
        content: str | None = None,
        content_json: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
        consumed_score: int = 0,
    ) -> dict[str, Any]:
        """追加消息（§5.2-§5.4），clientMessageId 幂等。

        返回 {message: {...}, conversationVersion: int}。
        """
        body: dict[str, Any] = {
            "clientMessageId": client_message_id,
            "role": role,
            "messageType": message_type,
            "content": content,
            "contentJson": content_json,
            "replyToMessageId": reply_to_message_id,
            "consumedScore": consumed_score,
        }
        return await self._request("POST", f"/api/v1/agent/conversations/{conversation_id}/messages", json_body=body)

    # ------------------------------------------------------------------
    # 定时任务（§6）
    # ------------------------------------------------------------------

    async def list_schedules(self) -> list[dict[str, Any]]:
        """查询全部任务（§6.1）。"""
        return await self._request("GET", "/api/v1/agent/schedules") or []

    async def create_schedule(
        self,
        name: str,
        prompt: str,
        cron_expression: str,
        timezone: str,
        description: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        """创建任务（§6.2），五段式 Cron + IANA 时区。"""
        body: dict[str, Any] = {
            "name": name,
            "description": description,
            "prompt": prompt,
            "cronExpression": cron_expression,
            "timezone": timezone,
        }
        if enabled is not None:
            body["enabled"] = enabled
        return await self._request("POST", "/api/v1/agent/schedules", json_body=body)

    async def get_schedule(self, schedule_id: int) -> dict[str, Any]:
        """查询任务（§6.3）。"""
        return await self._request("GET", f"/api/v1/agent/schedules/{schedule_id}")

    async def update_schedule(self, schedule_id: int, version: int, fields: dict[str, Any]) -> dict[str, Any]:
        """局部修改和启停（§6.4），version 必填，只更新明确提供的字段。"""
        body = {"version": version, **fields}
        return await self._request("PATCH", f"/api/v1/agent/schedules/{schedule_id}", json_body=body)

    async def delete_schedule(self, schedule_id: int, version: int) -> dict[str, Any]:
        """删除任务（§6.5）。"""
        return await self._request("DELETE", f"/api/v1/agent/schedules/{schedule_id}", params={"version": version})

    # ------------------------------------------------------------------
    # 时区字典（§6.6）
    # ------------------------------------------------------------------

    async def get_timezones(self) -> list[str]:
        """返回 Tenant 服务支持的全部 IANA 时区标识，按字典序排列。"""
        return await self._request("GET", "/api/v1/agent/timezones") or []
