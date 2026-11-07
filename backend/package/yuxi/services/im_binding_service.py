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
from sqlalchemy import select, update
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
FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"
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
    """Channel 入口解析出的真实执行身份。

    ``identity_source`` 区分三种来源：``binding``（绑定用户）、
    ``owner_fallback``（SaaS 未绑定回退归属人，需要提示）与 ``owner``
    （非 SaaS 部署直接按归属人执行，无需绑定提示）。
    """

    user: User
    identity_source: str


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
                try:
                    await clear_saas_agent_session(uid)
                    await im_binding_cache.forget_user_bindings(uid)
                except Exception as cache_exc:  # 清理失败不能把 401 变成 500，缓存会自然过期
                    logger.warning(
                        f"IM binding cache clear failed on 20314: uid={uid}, "
                        f"error={cache_exc.__class__.__name__}"
                    )
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
) -> tuple[str, str | None, str | None]:
    """用授权码换取平台身份，返回 ``(platform_user_id, union_id, mobile)``。

    飞书返回 ``open_id``，钉钉返回消息侧使用的 ``userid``；``mobile`` 取自平台
    用户资料，用于校验扫码账号属于当前 Yuxi 用户。飞书/钉钉换取 token 均不
    校验 ``redirect_uri``，该参数仅为对齐标准 OAuth 签名保留。
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
        access_token = payload.get("access_token")
        mobile = None
        if access_token:
            profile = await _request_json(
                "GET",
                FEISHU_USER_INFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            mobile = (profile.get("data") or {}).get("mobile")
        return open_id, payload.get("union_id"), mobile
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
        return userid, union_id, me.get("mobile")
    raise ValueError(f"platform 只支持 feishu 或 dingtalk: {platform}")


def _normalize_mobile(value: str | None) -> str:
    """把手机号归一化为纯数字并去掉中国区号，供平台资料与本地用户比对。"""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 13 and digits.startswith("86"):
        digits = digits[2:]
    return digits


def _ensure_platform_account_matches_user(
    uid: str, *, expected_mobile: str | None, platform_mobile: str | None
) -> None:
    """确认扫码的平台账号属于当前登录用户，避免把他人平台身份绑到自己账号。

    平台授权只证明扫码者控制该平台账号，Tenant 按 AgentSession 确定绑定目标，
    因此归属核验必须由 Yuxi 完成。比较基准是 Tenant 登录时下发并存入 AgentSession
    的手机号（用户不能改写），任一侧缺失或不一致都失败关闭，不调用 Tenant。
    """
    expected_value = _normalize_mobile(expected_mobile)
    platform_value = _normalize_mobile(platform_mobile)
    if not expected_value or not platform_value or expected_value != platform_value:
        logger.warning(
            f"Platform identity verification failed: uid={uid}, has_platform_mobile={bool(platform_value)}"
        )
        raise HTTPException(
            403,
            detail={
                "code": "im_binding_identity_mismatch",
                "message": "扫码账号与当前 Yuxi 账号不一致，请使用本人飞书/钉钉账号重新扫码",
            },
        )


async def create_binding_session(db: AsyncSession, *, user: User, platform: str, base_url: str) -> dict:
    """为用户创建一次性绑定会话，返回 ``{"bind_url", "expires_at"}``。

    绑定最终调用 Tenant 需要用户的 AgentSession，缺失时直接要求重新登录，
    避免生成一张必然失败、却已写入 pending state 的二维码。
    """
    _require_binding_enabled()
    platform = str(platform or "").strip().lower()
    if platform not in PLATFORMS:
        raise ValueError("platform 只支持 feishu 或 dingtalk")
    agent_session = await _require_agent_session(user)
    # 旧会话没有 Tenant 下发手机号，无法完成归属核验，提前失败避免消耗 state。
    if not _normalize_mobile(agent_session.mobile):
        raise _binding_login_required()
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

    state 用条件 UPDATE 原子消费：只有 pending、未使用且未过期的行能被置为
    used，并发回调最多一个成功。消费后立即 COMMIT，再换取平台身份并调用
    Tenant；换取或 Tenant 失败会烧掉该 state，用户需重新生成二维码。
    """
    _require_binding_enabled()
    platform = str(platform or "").strip().lower()
    if platform not in PLATFORMS:
        raise ValueError("platform 只支持 feishu 或 dingtalk")

    now = utc_now_naive()
    consumed = await db.execute(
        update(IMBindingSession)
        .where(
            IMBindingSession.state_hash == _hash_state(state),
            IMBindingSession.platform == platform,
            IMBindingSession.status == "pending",
            IMBindingSession.used_at.is_(None),
            IMBindingSession.expires_at > now,
        )
        .values(status="used", used_at=now)
        .returning(IMBindingSession.user_id, IMBindingSession.bot_id)
    )
    row = consumed.one_or_none()
    await db.commit()
    if row is None:
        raise HTTPException(
            400,
            detail={"code": "invalid_binding_state", "message": "绑定会话无效、已过期或已被使用"},
        )
    user_id, bot_id = row

    user = (
        await db.execute(select(User).where(User.id == user_id, User.is_deleted == 0))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(400, detail={"code": "invalid_binding_state", "message": "绑定用户不存在"})
    agent_session = await _require_agent_session(user)
    # 旧会话没有 Tenant 下发手机号，无法核验扫码账号归属，要求重新登录刷新会话。
    if not _normalize_mobile(agent_session.mobile):
        raise _binding_login_required()

    bot = await get_im_bot(db, bot_id)
    if bot is None:
        raise ValueError("绑定会话关联的机器人不存在")
    credentials = decrypt_credentials(platform, bot.credentials_encrypted)
    platform_user_id, _union_id, platform_mobile = await _exchange_code(
        platform, credentials, code, redirect_uri=None
    )
    _ensure_platform_account_matches_user(
        user.uid, expected_mobile=agent_session.mobile, platform_mobile=platform_mobile
    )
    # 先算缓存 TTL：解析失败时必须在调用 Tenant 前失败，避免“已绑定却显示失败”。
    ttl_seconds = get_saas_session_ttl_seconds(agent_session.expires_at)

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
        ttl_seconds=ttl_seconds,
    )
    return FRONTEND_BINDING_RESULT_PATH


def _bound_platform(row: dict) -> tuple[str, str] | None:
    """从 Tenant 绑定行解析受支持的平台与平台用户 ID，其余返回 None。"""
    platform = str(row.get("channelType") or "").strip().lower()
    channel_user_id = row.get("channelUserId")
    if platform not in PLATFORMS or row.get("bound") is not True or not channel_user_id:
        return None
    return platform, str(channel_user_id)


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
        parsed = _bound_platform(row)
        if parsed is None:
            continue
        platform, channel_user_id = parsed
        bindings.append(
            {
                "platform": platform,
                "platform_user_id": channel_user_id,
                "created_at": row.get("boundAt"),
            }
        )
    return sorted(bindings, key=lambda item: item["platform"])


async def preheat_binding_cache(uid: str, agent_session: SaasAgentSession) -> int:
    """登录后按 Tenant 当前绑定预热缓存，返回写入的平台数。

    支持平台先清旧映射再写新映射：Tenant 已解绑或已换号的平台不能继续命中
    旧缓存。TTL 取 AgentSession 剩余有效秒数，与消息侧可复用窗口对齐。
    """
    rows = await get_saas_client().list_channel_bindings(agent_session.token)
    bound: dict[str, str] = {}
    for row in rows:
        parsed = _bound_platform(row)
        if parsed is not None:
            bound[parsed[0]] = parsed[1]

    ttl_seconds = get_saas_session_ttl_seconds(agent_session.expires_at)
    for platform in sorted(PLATFORMS):
        await im_binding_cache.forget_binding(uid=uid, platform=platform)
        channel_user_id = bound.get(platform)
        if channel_user_id is not None:
            await im_binding_cache.remember_binding(
                uid=uid,
                platform=platform,
                channel_user_id=channel_user_id,
                ttl_seconds=ttl_seconds,
            )
    return len(bound)


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
    db: AsyncSession,
    *,
    transport_user: User,
    account_id: str,
    sender_id: str | None,
    transport_api_key_id: int | None,
) -> ChannelIdentity:
    """按 account_id 机器人归属与 sender_id 绑定解析真实执行身份。

    ``account_id`` 不归属 ``transport_user`` 或调用凭证不是该机器人的 API Key
    时抛 403；机器人停用时抛 403 ``im_bot_disabled``。非 SaaS 部署不启用绑定，
    始终以机器人归属人执行。绑定身份先读 Redis 缓存，未命中才调用 Tenant；
    Tenant 不可达时失败关闭，未绑定且 ``require_binding`` 打开时抛 409。
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
    if transport_api_key_id is None or bot.api_key_id is None or bot.api_key_id != transport_api_key_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "im_bot_credential_mismatch", "message": "调用凭证不是该机器人的 API Key"},
        )
    if not bot.enabled:
        raise HTTPException(
            status_code=403,
            detail={"code": "im_bot_disabled", "message": "机器人已停用"},
        )

    if not app_config.is_saas_enabled:
        return ChannelIdentity(user=transport_user, identity_source="owner")

    if sender_id:
        try:
            cached_uid = await im_binding_cache.bind_uid(bot.platform, sender_id)
        except Exception as exc:  # 缓存是加速层，读失败回退到 Tenant 冷启动回查
            logger.warning(f"IM binding cache read failed: platform={bot.platform}, error={exc.__class__.__name__}")
            cached_uid = None
        if cached_uid:
            bound_user = await _find_active_user(db, cached_uid)
            if bound_user is not None:
                return ChannelIdentity(user=bound_user, identity_source="binding")
        bound_user = await _resolve_bound_user(db, platform=bot.platform, sender_id=sender_id)
        if bound_user is not None:
            return ChannelIdentity(user=bound_user, identity_source="binding")

    if bot.require_binding:
        raise HTTPException(
            status_code=409,
            detail={"code": "im_account_not_bound", "message": "请先在 Yuxi 个人设置里绑定飞书/钉钉账号"},
        )
    return ChannelIdentity(user=transport_user, identity_source="owner_fallback")


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
