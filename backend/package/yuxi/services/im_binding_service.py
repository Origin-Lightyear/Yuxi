"""IM 账号绑定服务：绑定会话、平台 OAuth 授权换取与消息身份解析。

绑定链路：用户在 Yuxi 侧用该平台唯一启用机器人的凭据发起飞书/钉钉扫码授权，
回调后换取平台身份并调用 Tenant ``employee/channel-bindings`` 保存绑定；Tenant
是绑定关系的唯一保存方，Yuxi 只保留一次性 state 与 TTL 对齐 AgentSession 的
Redis 映射缓存。消息链路：Channel 入口先以「API Key = 机器人归属人」的传输
身份校验调用方，再按 ``(platform, sender_id)`` 先查缓存、冷启动回查 Tenant
``auth/channel`` 解析真实用户；未绑定且 ``require_binding`` 打开时返回 409。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import NoReturn
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.config import config as app_config
from yuxi.repositories.im_bot_repository import get_im_bot, list_im_bots
from yuxi.services import im_binding_cache
from yuxi.services.im_bot_service import PLATFORMS, decrypt_credentials
from yuxi.services.saas_auth_service import find_or_create_saas_user
from yuxi.services.saas_client import SaasAPIError, channel_type_for, get_saas_client
from yuxi.services.saas_session import (
    SaasAgentSession,
    clear_saas_agent_session,
    get_saas_agent_session,
    get_saas_session_ttl_seconds,
)
from yuxi.storage.postgres.models_business import AgentRun, IMBindingSession, User
from yuxi.utils import logger
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


def _binding_not_enabled() -> HTTPException:
    """绑定能力只在 SaaS 部署开放；本地部署继续按机器人归属人执行。"""
    return HTTPException(400, detail={"code": "im_binding_not_enabled", "message": "当前部署未启用 IM 账号绑定"})


def _binding_login_required() -> HTTPException:
    """绑定、解绑与查询都需要有效的 Tenant AgentSession。"""
    return HTTPException(
        401,
        detail={"code": "im_binding_login_required", "message": "Yuxi 登录状态已失效，请重新登录后再试"},
    )


def _require_binding_enabled() -> None:
    """绑定能力未启用时显式失败，不静默落回本地绑定。"""
    if not app_config.is_saas_enabled:
        raise _binding_not_enabled()


async def _require_agent_session(user: User) -> SaasAgentSession:
    """读取当前用户的 Tenant AgentSession；缺失即要求重新登录。"""
    session = await get_saas_agent_session(user.uid)
    if session is None:
        raise _binding_login_required()
    return session


async def _handle_tenant_error(uid: str | None, exc: Exception) -> NoReturn:
    """把 Tenant 渠道绑定错误映射为用户可见的失败，绝不静默成功。"""
    if isinstance(exc, SaasAPIError):
        if exc.code == 20314:
            if uid:
                await clear_saas_agent_session(uid)
                await im_binding_cache.forget_user_bindings(uid)
            raise _binding_login_required()
        if exc.code == 20316:
            raise HTTPException(
                409,
                detail={"code": "im_account_already_bound", "message": "该平台账号已绑定其他员工，请先解绑"},
            )
        if exc.code == 20302:
            raise HTTPException(
                403,
                detail={"code": "im_employee_unavailable", "message": "员工账号不可用，请联系管理员"},
            )
        if exc.code == 20317:
            logger.error("Tenant 渠道绑定管理密钥认证失败")
            raise HTTPException(
                502,
                detail={"code": "im_binding_tenant_unavailable", "message": "租户服务认证失败，请联系管理员"},
            )
        logger.warning(f"Tenant channel binding failed: code={exc.code}")
    else:
        logger.warning(f"Tenant channel binding request failed: {exc.__class__.__name__}")
    raise HTTPException(
        502,
        detail={"code": "im_binding_tenant_unavailable", "message": "租户服务暂时不可用，请稍后重试"},
    )


async def _remember_binding_safely(*, uid: str, platform: str, channel_user_id: str, ttl_seconds: int) -> None:
    """缓存只是加速层：写入失败记录告警，不推翻 Tenant 已提交的绑定结果。"""
    try:
        await im_binding_cache.forget_binding(uid=uid, platform=platform)
        await im_binding_cache.remember_binding(
            uid=uid, platform=platform, channel_user_id=channel_user_id, ttl_seconds=ttl_seconds
        )
    except Exception as exc:  # Redis 故障不应把成功的绑定回报成失败
        logger.warning(f"IM binding cache write failed: platform={platform}, error={exc.__class__.__name__}")


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
    _require_binding_enabled()
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
    """校验 state、换取平台身份并由 Tenant 保存绑定，返回前端回调路径。

    state 单次可用的持久化顺序：先 COMMIT 置 used_at，再换取平台身份，最后调用
    Tenant。代价是换取或 Tenant 失败会烧掉该 state，用户需重新生成二维码。
    """
    _require_binding_enabled()
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

    # state 一次性消费：先置 used_at 并 COMMIT，使重放保证不依赖调用方事务。
    session.status = "used"
    session.used_at = utc_now_naive()
    await db.commit()

    user = (
        await db.execute(select(User).where(User.id == session.user_id, User.is_deleted == 0))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(400, detail={"code": "invalid_binding_state", "message": "绑定用户不存在"})
    agent_session = await _require_agent_session(user)

    bot = await get_im_bot(db, session.bot_id)
    if bot is None:
        raise ValueError("绑定会话关联的机器人不存在")
    credentials = decrypt_credentials(platform, bot.credentials_encrypted)
    platform_user_id, _union_id = await _exchange_code(platform, credentials, code, redirect_uri=None)

    try:
        await get_saas_client().bind_channel_account(
            agent_session.token, channel_type_for(platform), platform_user_id
        )
    except Exception as exc:
        await _handle_tenant_error(user.uid, exc)

    await _remember_binding_safely(
        uid=user.uid,
        platform=platform,
        channel_user_id=platform_user_id,
        ttl_seconds=get_saas_session_ttl_seconds(agent_session.expires_at),
    )
    return FRONTEND_BINDING_RESULT_PATH


async def list_bindings(db: AsyncSession, *, user: User) -> list[dict]:
    """实时读取 Tenant 的三平台绑定，映射为前端使用的飞书/钉钉列表。"""
    del db
    _require_binding_enabled()
    agent_session = await _require_agent_session(user)
    try:
        rows = await get_saas_client().list_channel_bindings(agent_session.token)
    except Exception as exc:
        await _handle_tenant_error(user.uid, exc)

    bindings: list[dict] = []
    for row in rows:
        platform = str(row.get("channelType") or "").strip().lower()
        channel_user_id = row.get("channelUserId")
        if platform not in PLATFORMS or row.get("bound") is not True or not channel_user_id:
            continue
        bindings.append(
            {
                "platform": platform,
                "platform_user_id": str(channel_user_id),
                "created_at": row.get("boundAt"),
            }
        )
    return sorted(bindings, key=lambda item: item["platform"])


async def delete_binding(db: AsyncSession, *, user: User, platform: str) -> None:
    """由 Tenant 解除当前用户在某平台的绑定，并清理本地缓存。"""
    del db
    _require_binding_enabled()
    platform = str(platform or "").strip().lower()
    if platform not in PLATFORMS:
        raise ValueError("platform 只支持 feishu 或 dingtalk")
    agent_session = await _require_agent_session(user)
    try:
        await get_saas_client().unbind_channel_account(agent_session.token, channel_type_for(platform))
    except Exception as exc:
        await _handle_tenant_error(user.uid, exc)
    try:
        await im_binding_cache.forget_binding(uid=user.uid, platform=platform)
    except Exception as exc:  # Tenant 已解绑，缓存清理失败只影响下一次冷启动
        logger.warning(f"IM binding cache cleanup failed: platform={platform}, error={exc.__class__.__name__}")


async def _find_active_user(db: AsyncSession, uid: str) -> User | None:
    """按 uid 读取未删除的本地用户。"""
    return (
        await db.execute(select(User).where(User.uid == uid, User.is_deleted == 0))
    ).scalar_one_or_none()


async def _resolve_bound_user(db: AsyncSession, *, platform: str, sender_id: str) -> User | None:
    """缓存未命中时用 Tenant ``auth/channel`` 查一次并回填缓存。"""
    try:
        auth_result = await get_saas_client().authenticate_channel(channel_type_for(platform), sender_id)
    except SaasAPIError as exc:
        if exc.code == 20206:
            return None
        logger.warning(f"Tenant channel auth failed: code={exc.code}")
        raise HTTPException(
            502,
            detail={"code": "im_binding_unavailable", "message": "身份服务暂时不可用，请稍后重试"},
        ) from exc
    except Exception as exc:
        logger.warning(f"Tenant channel auth request failed: {exc.__class__.__name__}")
        raise HTTPException(
            502,
            detail={"code": "im_binding_unavailable", "message": "身份服务暂时不可用，请稍后重试"},
        ) from exc

    employee = auth_result.primary
    if employee is None or employee.enabled != 1:
        return None

    user = await find_or_create_saas_user(
        db,
        mobile=auth_result.mobile,
        employee_code=employee.employee_code,
        employee_name=employee.employee_name,
        department_name=employee.department_name,
    )
    await _remember_binding_safely(
        uid=user.uid,
        platform=platform,
        channel_user_id=sender_id,
        ttl_seconds=get_saas_session_ttl_seconds(employee.agent_session_expires_at),
    )
    return user


async def resolve_channel_identity(
    db: AsyncSession, *, transport_user: User, account_id: str, sender_id: str | None
) -> ChannelIdentity:
    """按 account_id 机器人归属与 sender_id 绑定解析真实执行身份。

    ``account_id`` 不归属 ``transport_user`` 时抛 403；机器人停用时抛 403
    ``im_bot_disabled``。非 SaaS 部署不启用绑定，始终以机器人归属人执行。
    绑定身份先读 Redis 缓存，未命中才调用 Tenant；Tenant 不可达时失败关闭，
    未绑定且 ``require_binding`` 打开时抛 409 ``im_account_not_bound``。
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
    if not bot.enabled:
        raise HTTPException(
            status_code=403,
            detail={"code": "im_bot_disabled", "message": "机器人已停用"},
        )

    if not app_config.is_saas_enabled:
        return ChannelIdentity(user=transport_user, owner_fallback=True)

    if sender_id:
        cached_uid = await im_binding_cache.bind_uid(bot.platform, sender_id)
        if cached_uid:
            bound_user = await _find_active_user(db, cached_uid)
            if bound_user is not None:
                return ChannelIdentity(user=bound_user, owner_fallback=False)
        bound_user = await _resolve_bound_user(db, platform=bot.platform, sender_id=sender_id)
        if bound_user is not None:
            return ChannelIdentity(user=bound_user, owner_fallback=False)

    if bot.require_binding:
        raise HTTPException(
            status_code=409,
            detail={"code": "im_account_not_bound", "message": "请先在 Yuxi 个人设置里绑定飞书/钉钉账号"},
        )
    return ChannelIdentity(user=transport_user, owner_fallback=True)


async def can_read_channel_run_as_bot_owner(db: AsyncSession, *, run: AgentRun | None, caller_uid: str) -> bool:
    """判断调用方是否因拥有提交该 Channel Run 的机器人而可读取它。

    只对 ``source == "channel"`` 的 Run 生效；来源元数据 ``account_id`` 必须
    解析到仍启用且归属调用方的机器人，否则一律拒绝。
    """
    if run is None or run.source != "channel":
        return False
    account_id = (run.origin_metadata or {}).get("account_id")
    try:
        bot_id = int(account_id)
    except (TypeError, ValueError):
        return False
    bot = await get_im_bot(db, bot_id)
    return bot is not None and bool(bot.enabled) and bot.owner_uid == str(caller_uid)


async def can_read_channel_thread_as_bot_owner(db: AsyncSession, *, thread_id: str, caller_uid: str) -> bool:
    """判断调用方是否拥有为该线程提交过 Channel Run 的机器人。

    产物下载只有线程 ID，没有 Run ID，因此按线程内任一 Channel Run 的机器人
    归属判定；线程本身仍由后续 Workdir 归属校验约束。
    """
    result = await db.execute(
        select(AgentRun).where(
            AgentRun.conversation_thread_id == thread_id,
            AgentRun.source == "channel",
        )
    )
    for run in result.scalars().all():
        if await can_read_channel_run_as_bot_owner(db, run=run, caller_uid=caller_uid):
            return True
    return False
