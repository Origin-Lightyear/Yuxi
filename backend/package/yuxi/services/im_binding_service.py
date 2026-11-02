"""IM 账号绑定服务：绑定会话、平台 OAuth 授权换取与消息身份解析。

绑定链路：用户在 Yuxi 侧用该平台唯一启用机器人的凭据发起飞书/钉钉扫码授权，
回调后换取平台身份写入 ``im_account_bindings``。消息链路：Channel 入口先以
「API Key = 机器人归属人」的传输身份校验调用方，再按 ``(platform, sender_id)``
解析真实用户；未绑定且 ``require_binding`` 打开时返回 409 拦截。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.repositories.im_bot_repository import get_im_bot, list_im_bots
from yuxi.services.im_bot_service import PLATFORMS, decrypt_credentials
from yuxi.storage.postgres.models_business import IMAccountBinding, IMBindingSession, User
from yuxi.utils.datetime_utils import format_utc_datetime, utc_now_naive

# 飞书 / 钉钉授权与换取身份端点（与各平台开放文档一致）。
FEISHU_AUTHORIZE_URL = "https://open.feishu.cn/open-apis/authen/v1/index"
DINGTALK_AUTHORIZE_URL = "https://login.dingtalk.com/oauth2/auth"
FEISHU_TENANT_ACCESS_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
FEISHU_USER_ACCESS_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v1/access_token"
DINGTALK_USER_ACCESS_TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/userAccessToken"
DINGTALK_USERS_ME_URL = "https://api.dingtalk.com/v1.0/contact/users/me"
DINGTALK_GETTOKEN_URL = "https://oapi.dingtalk.com/gettoken"
DINGTALK_GETBYUNIONID_URL = "https://oapi.dingtalk.com/topapi/v2/user/getbyunionid"

# state 有效期 10 分钟；只存 sha256 哈希，用后置 used_at，重放必须失败。
BINDING_STATE_TTL_SECONDS = 600
BINDING_CALLBACK_PATH_TEMPLATE = "/api/im-bindings/callback/{platform}"
# 绑定完成后前端跳回应用首页（个人设置「IM 账号」tab 的具体锚点由前端决定）。
FRONTEND_BINDING_RESULT_PATH = "/"


@dataclass(frozen=True)
class ChannelIdentity:
    """Channel 入口解析出的真实执行身份。"""

    user: User
    owner_fallback: bool


def _hash_state(state: str) -> str:
    """对一次性 state 做不可逆哈希，避免明文落库。"""
    return hashlib.sha256(str(state).encode("utf-8")).hexdigest()


def _build_authorize_url(platform: str, credentials: dict, redirect_uri: str, state: str) -> str:
    """按平台构建扫码授权 URL。"""
    platform = str(platform or "").strip().lower()
    if platform == "feishu":
        params = {"app_id": credentials["app_id"], "redirect_uri": redirect_uri, "state": state}
        return f"{FEISHU_AUTHORIZE_URL}?{urlencode(params)}"
    if platform == "dingtalk":
        params = {
            "response_type": "code",
            "client_id": credentials["client_id"],
            "redirect_uri": redirect_uri,
            "scope": "openid",
            "state": state,
            "prompt": "consent",
        }
        return f"{DINGTALK_AUTHORIZE_URL}?{urlencode(params)}"
    raise ValueError(f"platform 只支持 feishu 或 dingtalk: {platform}")


async def _request_json(method: str, url: str, *, headers=None, json=None, params=None) -> dict:
    """发出 JSON 请求并在非 2xx 时抛错，返回解析后的 JSON。

    任何 HTTP 层错误都转为只含状态码与路径（不含 query 串）的 ValueError，
    避免 httpx 默认异常文本把 query 里的 appsecret/access_token 带进日志。
    """
    async with httpx.AsyncClient() as client:
        try:
            response = await client.request(method, url, headers=headers, json=json, params=params, timeout=30.0)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            request = getattr(exc, "request", None)
            path = request.url.path if request is not None else ""
            if isinstance(exc, httpx.HTTPStatusError):
                raise ValueError(f"平台请求失败：HTTP {exc.response.status_code} {path}") from exc
            raise ValueError(f"平台请求失败：{path or '网络错误'}") from exc
        return response.json()


async def _feishu_tenant_access_token(credentials: dict) -> str:
    """用 app_id/app_secret 换取飞书 tenant_access_token。"""
    data = await _request_json(
        "POST",
        FEISHU_TENANT_ACCESS_TOKEN_URL,
        json={"app_id": credentials["app_id"], "app_secret": credentials["app_secret"]},
    )
    token = data.get("tenant_access_token")
    if not token:
        raise ValueError("飞书获取 tenant_access_token 失败")
    return token


async def _dingtalk_enterprise_access_token(credentials: dict) -> str:
    """用 client_id/client_secret 换取钉钉企业 access_token（getbyunionid 需要）。"""
    data = await _request_json(
        "GET",
        DINGTALK_GETTOKEN_URL,
        params={"appkey": credentials["client_id"], "appsecret": credentials["client_secret"]},
    )
    token = data.get("access_token")
    if not token:
        raise ValueError("钉钉获取企业 access_token 失败")
    return token


async def _dingtalk_userid(credentials: dict, union_id: str) -> str:
    """用 unionId 换取消息侧使用的钉钉 userid（员工 ID）。"""
    token = await _dingtalk_enterprise_access_token(credentials)
    data = await _request_json(
        "POST",
        DINGTALK_GETBYUNIONID_URL,
        params={"access_token": token},
        json={"unionid": union_id},
    )
    userid = (data.get("result") or {}).get("userid")
    if not userid:
        raise ValueError("钉钉根据 unionId 换取 userid 失败")
    return userid


async def _exchange_code(
    platform: str, credentials: dict, code: str, redirect_uri: str | None
) -> tuple[str, str | None]:
    """用授权码换取平台身份，返回 ``(platform_user_id, union_id)``。

    飞书返回 ``open_id``，钉钉返回消息侧使用的 ``userid``。飞书/钉钉换取 token
    均不校验 ``redirect_uri``，该参数仅为对齐标准 OAuth 签名保留。
    """
    platform = str(platform or "").strip().lower()
    if platform == "feishu":
        tenant_token = await _feishu_tenant_access_token(credentials)
        data = await _request_json(
            "POST",
            FEISHU_USER_ACCESS_TOKEN_URL,
            headers={"Authorization": f"Bearer {tenant_token}"},
            json={"grant_type": "authorization_code", "code": code},
        )
        payload = data.get("data") or {}
        open_id = payload.get("open_id")
        if not open_id:
            raise ValueError("飞书换取 user_access_token 未返回 open_id")
        return open_id, payload.get("union_id")
    if platform == "dingtalk":
        data = await _request_json(
            "POST",
            DINGTALK_USER_ACCESS_TOKEN_URL,
            json={
                "clientId": credentials["client_id"],
                "clientSecret": credentials["client_secret"],
                "code": code,
                "grantType": "authorization_code",
            },
        )
        access_token = data.get("accessToken")
        if not access_token:
            raise ValueError("钉钉换取 userAccessToken 失败")
        me = await _request_json("GET", DINGTALK_USERS_ME_URL, headers={"x-acs-dingtalk-access-token": access_token})
        union_id = me.get("unionId")
        if not union_id:
            raise ValueError("钉钉查询当前用户 unionId 失败")
        userid = await _dingtalk_userid(credentials, union_id)
        return userid, union_id
    raise ValueError(f"platform 只支持 feishu 或 dingtalk: {platform}")


async def create_binding_session(db: AsyncSession, *, user: User, platform: str, base_url: str) -> dict:
    """为用户创建一次性绑定会话，返回 ``{"bind_url", "expires_at"}``。"""
    platform = str(platform or "").strip().lower()
    if platform not in PLATFORMS:
        raise ValueError("platform 只支持 feishu 或 dingtalk")
    bots = [bot for bot in await list_im_bots(db) if bot.enabled and bot.platform == platform]
    if len(bots) > 1:
        raise ValueError("同一平台只能有一个启用的机器人")
    if not bots:
        raise ValueError(f"{platform} 没有启用的机器人，无法发起绑定")
    bot = bots[0]

    state = secrets.token_urlsafe(32)
    expires_at = utc_now_naive() + timedelta(seconds=BINDING_STATE_TTL_SECONDS)
    session = IMBindingSession(
        user_id=user.id,
        platform=platform,
        bot_id=bot.id,
        state_hash=_hash_state(state),
        status="pending",
        expires_at=expires_at,
    )
    db.add(session)
    await db.flush()

    redirect_uri = base_url.rstrip("/") + BINDING_CALLBACK_PATH_TEMPLATE.format(platform=platform)
    credentials = decrypt_credentials(platform, bot.credentials_encrypted)
    bind_url = _build_authorize_url(platform, credentials, redirect_uri, state)
    return {"bind_url": bind_url, "expires_at": format_utc_datetime(expires_at)}


async def complete_binding(db: AsyncSession, *, platform: str, code: str, state: str) -> str:
    """校验 state、换取平台身份并写入绑定，返回前端回调路径。

    state 单次可用的持久化顺序：先 COMMIT 置 used_at，再换取平台身份，最后写绑定并再次
    COMMIT。代价是换取失败会烧掉该 state，用户需重新生成二维码。
    """
    platform = str(platform or "").strip().lower()
    if platform not in PLATFORMS:
        raise ValueError("platform 只支持 feishu 或 dingtalk")

    result = await db.execute(select(IMBindingSession).where(IMBindingSession.state_hash == _hash_state(state)))
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(400, detail={"code": "invalid_binding_state", "message": "绑定会话无效或已过期"})
    if session.platform != platform:
        raise HTTPException(400, detail={"code": "invalid_binding_state", "message": "绑定会话平台不匹配"})
    if session.status != "pending" or session.used_at is not None:
        raise HTTPException(400, detail={"code": "invalid_binding_state", "message": "绑定会话已被使用"})
    if session.expires_at <= utc_now_naive():
        raise HTTPException(400, detail={"code": "invalid_binding_state", "message": "绑定会话已过期"})

    # 同平台已有绑定须提前拒绝，避免触发 (user_id, platform) 唯一约束的 IntegrityError。
    bound = await db.execute(
        select(IMAccountBinding.id).where(
            IMAccountBinding.user_id == session.user_id,
            IMAccountBinding.platform == platform,
        )
    )
    if bound.scalar_one_or_none() is not None:
        raise HTTPException(
            409,
            detail={"code": "im_account_already_bound", "message": "该平台已绑定，请先解绑"},
        )

    # state 一次性消费：先置 used_at 并 COMMIT，使重放保证不依赖调用方事务。
    session.status = "used"
    session.used_at = utc_now_naive()
    await db.commit()

    bot = await get_im_bot(db, session.bot_id)
    if bot is None:
        raise ValueError("绑定会话关联的机器人不存在")
    credentials = decrypt_credentials(platform, bot.credentials_encrypted)
    platform_user_id, _union_id = await _exchange_code(platform, credentials, code, redirect_uri=None)

    existing = await db.execute(
        select(IMAccountBinding.id).where(
            IMAccountBinding.platform == platform,
            IMAccountBinding.platform_user_id == platform_user_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(409, detail={"code": "im_account_already_bound", "message": "该平台身份已绑定其他账号"})

    db.add(
        IMAccountBinding(
            user_id=session.user_id,
            platform=platform,
            platform_user_id=platform_user_id,
            bot_id=session.bot_id,
        )
    )
    await db.commit()
    return FRONTEND_BINDING_RESULT_PATH


async def list_bindings(db: AsyncSession, *, user_id: int) -> list[dict]:
    """列出指定用户的全部平台绑定。"""
    result = await db.execute(
        select(IMAccountBinding).where(IMAccountBinding.user_id == user_id).order_by(IMAccountBinding.platform.asc())
    )
    return [binding.to_dict() for binding in result.scalars().all()]


async def delete_binding(db: AsyncSession, *, user: User, platform: str) -> bool:
    """删除用户在某平台的绑定，返回是否实际删除了记录。"""
    platform = str(platform or "").strip().lower()
    if platform not in PLATFORMS:
        raise ValueError("platform 只支持 feishu 或 dingtalk")
    result = await db.execute(
        select(IMAccountBinding).where(
            IMAccountBinding.user_id == user.id,
            IMAccountBinding.platform == platform,
        )
    )
    binding = result.scalar_one_or_none()
    if binding is None:
        return False
    await db.delete(binding)
    await db.flush()
    return True


async def resolve_channel_identity(
    db: AsyncSession, *, transport_user: User, account_id: str, sender_id: str | None
) -> ChannelIdentity:
    """按 account_id 机器人归属与 sender_id 绑定解析真实执行身份。

    ``account_id`` 不归属 ``transport_user`` 时抛 403；未绑定且 ``require_binding``
    打开时抛 409 ``im_account_not_bound``，关闭时回退到机器人归属人并标记 owner_fallback。
    """
    try:
        bot_id = int(account_id)
    except (TypeError, ValueError):
        bot_id = None
    bot = await get_im_bot(db, bot_id) if bot_id is not None else None
    if bot is None or bot.owner_uid != transport_user.uid:
        raise HTTPException(
            status_code=403,
            detail={"code": "im_bot_not_owned", "message": "机器人不归属当前调用方"},
        )

    binding = None
    if sender_id:
        result = await db.execute(
            select(IMAccountBinding).where(
                IMAccountBinding.platform == bot.platform,
                IMAccountBinding.platform_user_id == sender_id,
            )
        )
        binding = result.scalar_one_or_none()
    if binding is not None:
        bound_user = (
            await db.execute(select(User).where(User.id == binding.user_id, User.is_deleted == 0))
        ).scalar_one_or_none()
        if bound_user is not None:
            return ChannelIdentity(user=bound_user, owner_fallback=False)

    if bot.require_binding:
        raise HTTPException(
            status_code=409,
            detail={"code": "im_account_not_bound", "message": "请先在 Yuxi 个人设置里绑定飞书/钉钉账号"},
        )
    return ChannelIdentity(user=transport_user, owner_fallback=True)
