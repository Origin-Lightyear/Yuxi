"""定时任务定义服务：校验并透传 Tenant 服务的任务 CRUD（AGENT_CLIENT_API.md §6）。

任务定义只在 Tenant 侧持久化，Yuxi 负责本地校验（五段式 Cron + IANA 时区）
和后续的调度执行（schedule_runner）。
"""

from __future__ import annotations

from typing import Any

from croniter import CroniterBadCronError, croniter
from fastapi import HTTPException
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from yuxi.services.agent_data_client import AgentDataClient
from yuxi.services.saas_client import SaasAPIError
from yuxi.services.saas_identity import SaasEmployeeContext

CRON_ERROR_CODE = 20312
TIMEZONE_ERROR_CODE = 20313
VERSION_CONFLICT_CODE = 20308


def validate_cron_and_timezone(cron_expression: str | None, timezone: str | None) -> None:
    """创建/修改前本地校验，错误码与 Tenant 服务保持一致（20312/20313）。"""
    if not cron_expression:
        raise HTTPException(status_code=400, detail={"code": 40001, "message": "cronExpression 不能为空"})
    if len(cron_expression.split()) != 5:
        raise HTTPException(status_code=400, detail={"code": CRON_ERROR_CODE, "message": "Cron 必须是 Unix 五段式"})
    try:
        croniter(cron_expression)
    except (CroniterBadCronError, ValueError):
        raise HTTPException(status_code=400, detail={"code": CRON_ERROR_CODE, "message": "Cron 表达式不合法"})

    if not timezone:
        raise HTTPException(status_code=400, detail={"code": 40001, "message": "timezone 不能为空"})
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise HTTPException(status_code=400, detail={"code": TIMEZONE_ERROR_CODE, "message": "时区必须是 IANA 标识"})


def _translate_api_error(exc: SaasAPIError) -> HTTPException:
    if exc.code == VERSION_CONFLICT_CODE:
        return HTTPException(status_code=409, detail={"code": exc.code, "message": exc.msg})
    return HTTPException(status_code=502, detail={"code": exc.code, "message": exc.msg})


def _client(ctx: SaasEmployeeContext) -> AgentDataClient:
    return AgentDataClient(ctx.tenant_id, ctx.employee_id)


async def list_schedules(ctx: SaasEmployeeContext) -> list[dict[str, Any]]:
    """查询全部任务（§6.1）。"""
    try:
        return await _client(ctx).list_schedules()
    except SaasAPIError as exc:
        raise _translate_api_error(exc)


async def create_schedule(ctx: SaasEmployeeContext, payload: dict[str, Any]) -> dict[str, Any]:
    """创建任务（§6.2），先本地校验 Cron 与时区。"""
    validate_cron_and_timezone(payload.get("cronExpression"), payload.get("timezone"))
    try:
        return await _client(ctx).create_schedule(
            name=payload["name"],
            prompt=payload["prompt"],
            cron_expression=payload["cronExpression"],
            timezone=payload["timezone"],
            description=payload.get("description"),
            enabled=payload.get("enabled"),
        )
    except SaasAPIError as exc:
        raise _translate_api_error(exc)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail={"code": 40001, "message": f"缺少必填字段: {exc}"})


async def get_schedule(ctx: SaasEmployeeContext, schedule_id: int) -> dict[str, Any]:
    """查询任务（§6.3）。"""
    try:
        return await _client(ctx).get_schedule(schedule_id)
    except SaasAPIError as exc:
        raise _translate_api_error(exc)


async def update_schedule(ctx: SaasEmployeeContext, schedule_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """局部修改和启停（§6.4）；Cron 或时区变更时重新校验。"""
    fields = {k: v for k, v in payload.items() if k != "version"}
    if "cronExpression" in fields or "timezone" in fields:
        merged_cron = fields.get("cronExpression")
        merged_tz = fields.get("timezone")
        if merged_cron is None or merged_tz is None:
            current = await get_schedule(ctx, schedule_id)
            merged_cron = merged_cron or current.get("cronExpression")
            merged_tz = merged_tz or current.get("timezone")
        validate_cron_and_timezone(merged_cron, merged_tz)
    try:
        return await _client(ctx).update_schedule(schedule_id, version=payload["version"], fields=fields)
    except SaasAPIError as exc:
        raise _translate_api_error(exc)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail={"code": 40001, "message": f"缺少必填字段: {exc}"})


async def delete_schedule(ctx: SaasEmployeeContext, schedule_id: int, version: int) -> dict[str, Any]:
    """删除任务（§6.5）。"""
    try:
        return await _client(ctx).delete_schedule(schedule_id, version=version)
    except SaasAPIError as exc:
        raise _translate_api_error(exc)


async def get_timezones(ctx: SaasEmployeeContext) -> list[str]:
    """时区字典（§6.6）。"""
    try:
        return await _client(ctx).get_timezones()
    except SaasAPIError as exc:
        raise _translate_api_error(exc)


def resolve_schedule_agent_slug(schedule: dict[str, Any]) -> str:
    """约定任务 name 即要触发的 agent slug。"""
    return str(schedule.get("name") or "").strip()


def schedule_thread_id(schedule_id: int) -> str:
    """每个任务使用专属线程。"""
    return f"schedule:{schedule_id}"
