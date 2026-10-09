"""定时任务透传接口集成测试。

本地管理员账号未绑定 SaaS 员工身份，透传接口应稳定返回 404，
不产生任何对 Tenant 服务的调用。
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_schedules_endpoints_require_saas_identity(test_client, admin_headers):
    response = await test_client.get("/api/system/schedules", headers=admin_headers)
    assert response.status_code == 404, response.text
    assert "SaaS" in response.json()["detail"] or "SaaS" in str(response.json()["detail"])


async def test_timezones_endpoint_requires_saas_identity(test_client, admin_headers):
    response = await test_client.get("/api/system/timezones", headers=admin_headers)
    assert response.status_code == 404, response.text


async def test_create_schedule_rejected_before_cron_validation(test_client, admin_headers):
    """身份门禁优先于 Cron 校验：未绑定 SaaS 身份的用户即使带非法 Cron 也得到 404。

    Cron/时区校验在 schedule_service 内完成（先于 Tenant 调用），
    但前提是用户已通过 _require_saas_employee_context 门禁。
    """
    response = await test_client.post(
        "/api/system/schedules",
        json={
            "name": "每日汇总",
            "prompt": "汇总",
            "cronExpression": "0 9 * * * *",
            "timezone": "Asia/Shanghai",
        },
        headers=admin_headers,
    )
    assert response.status_code == 404, response.text
