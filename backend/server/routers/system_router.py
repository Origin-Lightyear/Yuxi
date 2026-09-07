import os
from pathlib import Path

import aiofiles
import yaml
from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi import config, get_version
from yuxi.storage.postgres.models_business import User
from yuxi.utils.logging_config import logger

from server.utils.auth_middleware import get_admin_user, get_db, get_required_user

system = APIRouter(prefix="/system", tags=["system"])

# =============================================================================
# === 健康检查分组 ===
# =============================================================================


@system.get("/health")
async def health_check():
    """系统健康检查接口（公开接口）"""
    return {"status": "ok", "message": "服务正常运行", "version": get_version()}


@system.get("/discovery")
async def discovery():
    """系统能力发现接口（公开接口）"""
    return {
        "name": "Linko AI",
        "version": get_version(),
        "api_prefix": "/api",
        "capabilities": {
            "cli": {
                "min_cli_version": "0.1.0",
                "browser_login": True,
                "api_key_auth": True,
                "remote_config": True,
                "kb_upload": True,
                "kb_list": True,
                "kb_files": True,
                "kb_query": True,
                "kb_open": True,
                "kb_find": True,
            }
        },
        "endpoints": {
            "health": "/api/system/health",
            "auth_me": "/api/auth/me",
            "cli_auth_sessions": "/api/auth/cli/sessions",
            "cli_auth_authorize": "/auth/cli/authorize",
        },
    }


# =============================================================================
# === 配置管理分组 ===
# =============================================================================


@system.get("/config")
async def get_config(current_user: User = Depends(get_required_user)):
    """获取系统配置"""
    return config.dump_config()


@system.post("/config")
async def update_config_single(key=Body(...), value=Body(...), current_user: User = Depends(get_admin_user)) -> dict:
    """更新单个配置项"""
    if not isinstance(key, str) or key not in type(config).model_fields:
        raise HTTPException(status_code=400, detail=f"未知配置项: {key}")
    if not config.can_update(key):
        raise HTTPException(status_code=400, detail=f"配置项不可修改: {key}")
    try:
        config.set_value(key, value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    config.save()
    return config.dump_config()


@system.post("/config/update")
async def update_config_batch(items: dict = Body(...), current_user: User = Depends(get_admin_user)) -> dict:
    """批量更新配置项"""
    try:
        config.update(items)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    config.save()
    return config.dump_config()


@system.get("/logs")
async def get_system_logs(levels: str | None = None, current_user: User = Depends(get_admin_user)):
    """获取系统日志

    Args:
        levels: 可选的日志级别过滤，多个级别用逗号分隔，如 "INFO,ERROR,DEBUG,WARNING"
    """
    try:
        from yuxi.utils.logging_config import LOG_FILE

        # 解析日志级别过滤条件
        level_filter = None
        if levels:
            level_filter = set(level.strip().upper() for level in levels.split(",") if level.strip())

        #  修复 GBK 编码报错：强制 utf-8 读取，忽略错误
        async with aiofiles.open(LOG_FILE, encoding="utf-8", errors="ignore") as f:
            # 读取最后1000行
            lines = []
            async for line in f:
                filtered_line = line.rstrip("\n\r")
                # 如果指定了日志级别过滤，则按级别过滤
                if level_filter:
                    # 日志格式: 2025-03-10 08:26:37,269 - INFO - module - message
                    # 提取日志级别
                    parts = filtered_line.split(" - ")
                    if len(parts) >= 2 and parts[1].strip() in level_filter:
                        lines.append(filtered_line + "\n")
                    # 继续读取以保持行数统计准确
                    if len(lines) > 1000:
                        lines.pop(0)
                else:
                    lines.append(filtered_line + "\n")
                    if len(lines) > 1000:
                        lines.pop(0)

        log = "".join(lines)
        return {"log": log, "message": "success", "log_file": LOG_FILE}
    except Exception as e:
        logger.error(f"获取系统日志失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取系统日志失败: {str(e)}")


# =============================================================================
# === SaaS 租户配置分组 ===
# =============================================================================


@system.get("/tenant-config")
async def get_tenant_config(
    current_user: User = Depends(get_required_user),
    tenant_id: int | None = None,
):
    """获取 SaaS 租户配置（含 MCP URL 等），从 Platform 服务拉取。

    tenant_id 可选：未传时尝试从当前用户推断（SaaS 模式下 uid 为手机号对应的租户）。
    """
    from yuxi.config import config as app_config
    from yuxi.services.saas_client import SaasAPIError, get_saas_client

    if not app_config.is_saas_enabled:
        raise HTTPException(status_code=404, detail="SaaS 模式未启用")

    saas = get_saas_client()
    try:
        tenant_cfg = await saas.get_tenant_config(tenant_id)
    except SaasAPIError as exc:
        raise HTTPException(status_code=502, detail=f"租户配置服务错误: {exc.msg}")
    except Exception as exc:
        logger.error(f"获取租户配置失败: {exc}")
        raise HTTPException(status_code=502, detail="租户配置服务不可用")

    return {
        "tenant_id": tenant_cfg.id,
        "llm_url": tenant_cfg.llm_url,
        "mcp_url": tenant_cfg.mcp_url,
        "status": tenant_cfg.status,
        "emp_num": tenant_cfg.emp_num,
        "auth_end_time": tenant_cfg.auth_end_time,
        "version": tenant_cfg.version,
    }


@system.get("/saas-status")
async def get_saas_status():
    """查询 SaaS 模式启用状态（公开接口）。"""
    from yuxi.config import config as app_config

    return {
        "saas_enabled": app_config.is_saas_enabled,
    }


# =============================================================================
# === SaaS 定时任务分组 ===
# =============================================================================


class ScheduleCreate(BaseModel):
    name: str
    prompt: str
    cronExpression: str
    timezone: str
    description: str | None = None
    enabled: bool | None = None


class ScheduleUpdate(BaseModel):
    version: int
    name: str | None = None
    prompt: str | None = None
    description: str | None = None
    cronExpression: str | None = None
    timezone: str | None = None
    enabled: bool | None = None


async def _require_saas_employee_context(
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """解析当前登录用户的 SaaS 员工上下文，非 SaaS 用户返回 404。"""
    from yuxi.config import config as app_config
    from yuxi.services.saas_identity import get_saas_employee_context

    if not app_config.is_agent_data_sync_enabled:
        raise HTTPException(status_code=404, detail="SaaS 模式未启用")
    ctx = await get_saas_employee_context(db, current_user.uid)
    if ctx is None:
        raise HTTPException(status_code=404, detail="当前用户未绑定 SaaS 员工身份")
    return ctx


@system.get("/schedules")
async def list_schedules(ctx=Depends(_require_saas_employee_context)):
    """查询当前员工的全部定时任务（透传 Tenant 服务）。"""
    from yuxi.services import schedule_service

    return {"items": await schedule_service.list_schedules(ctx)}


@system.post("/schedules")
async def create_schedule(data: ScheduleCreate, ctx=Depends(_require_saas_employee_context)):
    """创建定时任务（透传 Tenant 服务，本地先校验 Cron 与时区）。"""
    from yuxi.services import schedule_service

    return await schedule_service.create_schedule(ctx, data.model_dump())


@system.get("/schedules/{schedule_id}")
async def get_schedule(schedule_id: int, ctx=Depends(_require_saas_employee_context)):
    """查询单个定时任务。"""
    from yuxi.services import schedule_service

    return await schedule_service.get_schedule(ctx, schedule_id)


@system.patch("/schedules/{schedule_id}")
async def update_schedule(schedule_id: int, data: ScheduleUpdate, ctx=Depends(_require_saas_employee_context)):
    """修改或启停定时任务。"""
    from yuxi.services import schedule_service

    return await schedule_service.update_schedule(ctx, schedule_id, data.model_dump(exclude_none=True))


@system.delete("/schedules/{schedule_id}")
async def delete_schedule(schedule_id: int, version: int, ctx=Depends(_require_saas_employee_context)):
    """删除定时任务。"""
    from yuxi.services import schedule_service

    return await schedule_service.delete_schedule(ctx, schedule_id, version)


@system.get("/timezones")
async def get_timezones(ctx=Depends(_require_saas_employee_context)):
    """时区字典（透传 Tenant 服务）。"""
    from yuxi.services import schedule_service

    return {"items": await schedule_service.get_timezones(ctx)}


# =============================================================================
# === 信息管理分组 ===
# =============================================================================


async def load_info_config():
    """加载信息配置文件"""
    try:
        # 配置文件路径
        brand_file_path = os.environ.get("YUXI_BRAND_FILE_PATH", "package/yuxi/config/static/info.local.yaml")
        config_path = Path(brand_file_path)

        # 检查文件是否存在
        if not config_path.exists():
            logger.debug(f"The config file {config_path} does not exist, using default config")
            config_path = Path("package/yuxi/config/static/info.template.yaml")

        # 异步读取配置文件
        async with aiofiles.open(config_path, encoding="utf-8") as file:
            content = await file.read()

        # 注入版本号占位符
        content = content.replace("{{YUXI_VERSION}}", get_version())

        config = yaml.safe_load(content)

        return config

    except Exception as e:
        logger.error(f"Failed to load info config: {e}")
        return {}


@system.get("/info")
async def get_info_config():
    """获取系统信息配置（公开接口，无需认证）"""
    try:
        config = await load_info_config()
        return {"success": True, "data": config}
    except Exception as e:
        logger.error(f"获取信息配置失败: {e}")
        raise HTTPException(status_code=500, detail="获取信息配置失败")


@system.post("/info/reload")
async def reload_info_config(current_user: User = Depends(get_admin_user)):
    """重新加载信息配置"""
    try:
        config = await load_info_config()
        return {"success": True, "message": "配置重新加载成功", "data": config}
    except Exception as e:
        logger.error(f"重新加载信息配置失败: {e}")
        raise HTTPException(status_code=500, detail="重新加载信息配置失败")


# =============================================================================
# === 通用配置项与 OCR 分组 ===
# =============================================================================


class ConfigOptionValuePayload(BaseModel):
    """管理员可更新的通用配置值。"""

    value: dict


@system.get("/config/options")
async def get_config_options(
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """返回系统定义的通用配置表单和值。"""

    from yuxi.config.options import list_options, serialize_option

    return {"options": [serialize_option(record) for record in await list_options(db)]}


@system.put("/config/options/{key}")
async def put_config_option(
    key: str,
    payload: ConfigOptionValuePayload,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """保存一个通用配置项的 JSON 值。"""

    from yuxi.config.options import serialize_option, update_option_value

    try:
        record = await update_option_value(db, key, payload.value, current_user.username)
        if record is None:
            raise HTTPException(status_code=404, detail=f"配置项不存在: {key}")
        await db.commit()
        await db.refresh(record)
        return {"option": serialize_option(record)}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@system.get("/ocr/options")
async def get_ocr_engine_options(
    current_user: User = Depends(get_required_user),
):
    """返回所有代码支持的 OCR 方法和默认项。"""

    from yuxi.services.ocr_service import get_ocr_options

    return get_ocr_options()


@system.get("/ocr/health")
async def get_ocr_health(
    current_user: User = Depends(get_required_user),
    db: AsyncSession = Depends(get_db),
):
    """供登录用户使用当前有效配置检查全部 OCR 方法。"""

    from yuxi.services.ocr_service import check_all_ocr_health

    return {"health": await check_all_ocr_health(db)}
