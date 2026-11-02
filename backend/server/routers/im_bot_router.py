"""IM 机器人配置管理接口与网关配置拉取接口。"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.services.im_bot_service import (
    create_im_bot,
    delete_im_bot,
    get_gateway_configs,
    list_im_bots,
    set_im_bot_enabled,
    update_im_bot,
)
from yuxi.storage.postgres.models_business import User

from server.utils.auth_middleware import get_admin_user, get_db

im_bots = APIRouter(prefix="/system/im-bots", tags=["im-bots"])
im_gateway = APIRouter(prefix="/internal/im-gateway", tags=["im-gateway"])


class IMBotPayload(BaseModel):
    """机器人创建 / 更新请求体。"""

    platform: str | None = Field(None, description="feishu 或 dingtalk")
    name: str | None = None
    enabled: bool | None = None
    owner_uid: str | None = None
    agent_slug: str | None = None
    credentials: dict | None = None
    allowed_users: list[str] | None = None
    require_mention: bool | None = None
    require_binding: bool | None = None
    group_sessions_per_user: bool | None = None


def require_gateway_token(x_im_gateway_token: str | None = Header(None, alias="X-IM-Gateway-Token")) -> None:
    """校验网关静态令牌；未配置时隐藏路由。"""
    from yuxi.config import config as app_config

    token = app_config.im_gateway_token
    if not token:
        raise HTTPException(status_code=404, detail="Not Found")
    if not x_im_gateway_token or not hmac.compare_digest(x_im_gateway_token, token):
        raise HTTPException(status_code=401, detail="无效的网关令牌")


@im_bots.get("")
async def list_bots(current_user: User = Depends(get_admin_user), db: AsyncSession = Depends(get_db)):
    """列出全部机器人配置。"""
    return {"success": True, "data": await list_im_bots(db)}


@im_bots.post("")
async def create_bot(
    payload: IMBotPayload,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """创建机器人配置。"""
    try:
        bot = await create_im_bot(db, payload.model_dump(exclude_none=True), current_user.username)
        await db.commit()
        return {"success": True, "data": bot.to_dict()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@im_bots.put("/{bot_id}")
async def update_bot(
    bot_id: int,
    payload: IMBotPayload,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """更新机器人配置。"""
    try:
        bot = await update_im_bot(db, bot_id, payload.model_dump(exclude_unset=True), current_user.username)
        await db.commit()
        return {"success": True, "data": bot.to_dict()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@im_bots.post("/{bot_id}/enabled")
async def set_enabled(
    bot_id: int,
    enabled: bool,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """启用或停用机器人。"""
    try:
        bot = await set_im_bot_enabled(db, bot_id, enabled, current_user.username)
        await db.commit()
        return {"success": True, "data": bot.to_dict()}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@im_bots.delete("/{bot_id}")
async def delete_bot(
    bot_id: int,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """删除机器人配置。"""
    deleted = await delete_im_bot(db, bot_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="机器人配置不存在")
    await db.commit()
    return {"success": True}


@im_gateway.get("/config")
async def gateway_config(_: None = Depends(require_gateway_token), db: AsyncSession = Depends(get_db)):
    """返回启用中的机器人配置（含解密凭证），供网关拉取。"""
    return {"success": True, "data": await get_gateway_configs(db)}
