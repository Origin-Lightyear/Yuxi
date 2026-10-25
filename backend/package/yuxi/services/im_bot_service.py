"""IM 机器人配置服务：CRUD、凭证加解密与 API Key 自动创建。"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.repositories.agent_repository import AgentRepository
from yuxi.repositories.im_bot_repository import (
    create_im_bot as _create,
    delete_im_bot as _delete,
    get_im_bot,
    list_im_bots as _list,
)
from yuxi.services.digital_employee_service import ensure_digital_employee_can_start
from yuxi.storage.postgres.models_business import APIKey, IMBotConfig, User
from yuxi.utils.auth_utils import AuthUtils
from yuxi.utils.crypto_utils import decrypt_secret, encrypt_secret
from yuxi.utils.logging_config import logger

PLATFORMS = {"feishu", "dingtalk"}

_REQUIRED_FIELDS = {
    "feishu": ("app_id", "app_secret"),
    "dingtalk": ("client_id", "client_secret"),
}


def _validate_credentials(platform: str, credentials: dict) -> dict:
    """校验平台凭证字段，返回规范化后的副本。"""
    platform = str(platform or "").strip().lower()
    if platform not in PLATFORMS:
        raise ValueError("platform 只支持 feishu 或 dingtalk")
    if not isinstance(credentials, dict):
        raise ValueError("credentials 必须是对象")
    normalized = {key: str(value).strip() for key, value in credentials.items() if value is not None}
    for field in _REQUIRED_FIELDS[platform]:
        if not normalized.get(field):
            raise ValueError(f"{platform} 凭证缺少 {field}")
    if platform == "feishu":
        normalized["domain"] = normalized.get("domain") or "feishu"
    return normalized


def encrypt_credentials(platform: str, credentials: dict) -> str:
    """校验并加密平台凭证，返回密文。"""
    normalized = _validate_credentials(platform, credentials)
    return encrypt_secret(json.dumps(normalized, ensure_ascii=False))


def decrypt_credentials(platform: str, cipher: str) -> dict:
    """解密密文并返回平台凭证。"""
    return json.loads(decrypt_secret(cipher))


def _normalize_allowed_users(value) -> list[str]:
    """去重并只保留非空字符串。"""
    if not value:
        return []
    seen: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in seen:
            seen.append(text)
    return seen


async def _resolve_owner(db: AsyncSession, owner_uid: str) -> User:
    """按 uid 找到未删除的属主用户。"""
    owner_uid = str(owner_uid or "").strip()
    if not owner_uid:
        raise ValueError("owner_uid 不能为空")
    # is_deleted 为 Integer（0=否，1=是），必须用 == 0 判断软删除。
    result = await db.execute(select(User).where(User.uid == owner_uid, User.is_deleted == 0))
    user = result.scalar_one_or_none()
    if user is None:
        raise ValueError("归属用户不存在")
    return user


async def _validate_agent_binding(db: AsyncSession, agent_slug: str, owner: User) -> None:
    """校验 IM Bot 绑定的智能体对属主可见且当前可运行。"""
    agent = await AgentRepository(db).get_visible_by_slug(slug=agent_slug, user=owner, kind="main")
    if agent is None:
        raise ValueError("智能体不存在或不可访问")
    ensure_digital_employee_can_start(owner, agent)


async def _provision_api_key(db: AsyncSession, user: User, bot_name: str) -> tuple[APIKey, str]:
    """为机器人创建绑定属主的 API Key，返回 (记录, 明文)。"""
    full_key, key_hash, key_prefix = AuthUtils.generate_api_key()
    api_key = APIKey(
        key_hash=key_hash,
        key_prefix=key_prefix,
        name=f"im-bot:{bot_name}",
        user_id=user.id,
        created_by=str(user.id),
    )
    db.add(api_key)
    await db.flush()
    return api_key, full_key


async def _revoke_api_key(db: AsyncSession, api_key_id: int | None) -> None:
    """删除机器人关联的 API Key（若存在）。"""
    if not api_key_id:
        return
    result = await db.execute(select(APIKey).where(APIKey.id == api_key_id))
    api_key = result.scalar_one_or_none()
    if api_key is not None:
        await db.delete(api_key)
        await db.flush()


async def create_im_bot(db: AsyncSession, payload: dict, username: str) -> IMBotConfig:
    """创建机器人配置，并自动创建绑定属主的 API Key。"""
    platform = str(payload.get("platform", "")).strip().lower()
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("name 不能为空")
    agent_slug = str(payload.get("agent_slug") or "").strip()
    if not agent_slug:
        raise ValueError("agent_slug 不能为空")
    owner = await _resolve_owner(db, payload.get("owner_uid"))
    await _validate_agent_binding(db, agent_slug, owner)
    credentials_encrypted = encrypt_credentials(platform, payload.get("credentials") or {})
    api_key, full_key = await _provision_api_key(db, owner, name)
    bot = await _create(
        db,
        {
            "platform": platform,
            "name": name,
            "enabled": bool(payload.get("enabled", True)),
            "credentials_encrypted": credentials_encrypted,
            "owner_uid": owner.uid,
            "api_key_id": api_key.id,
            "api_key_cipher": encrypt_secret(full_key),
            "agent_slug": agent_slug,
            "allowed_users": _normalize_allowed_users(payload.get("allowed_users")),
            "require_mention": bool(payload.get("require_mention", True)),
            "group_sessions_per_user": bool(payload.get("group_sessions_per_user", True)),
            "created_by": username,
            "updated_by": username,
        },
    )
    return bot


async def update_im_bot(db: AsyncSession, bot_id: int, payload: dict, username: str) -> IMBotConfig:
    """更新机器人配置；仅在提供 credentials 或 owner_uid 变化时改动凭证。"""
    bot = await get_im_bot(db, bot_id)
    if bot is None:
        raise ValueError("机器人配置不存在")

    if "owner_uid" in payload:
        owner = await _resolve_owner(db, payload.get("owner_uid"))
        if owner.uid != bot.owner_uid:
            # 属主变更时回收旧 Key，并为新属主重新签发，保证凭证始终与属主绑定。
            # 先清空外键引用再删除旧 Key，避免 flush 时触发 api_keys 外键约束。
            old_key_id = bot.api_key_id
            bot.api_key_id = None
            await db.flush()
            await _revoke_api_key(db, old_key_id)
            api_key, full_key = await _provision_api_key(db, owner, bot.name)
            bot.owner_uid = owner.uid
            bot.api_key_id = api_key.id
            bot.api_key_cipher = encrypt_secret(full_key)
    if "agent_slug" in payload or "owner_uid" in payload:
        effective_owner = owner if "owner_uid" in payload else await _resolve_owner(db, bot.owner_uid)
        effective_slug = str(payload.get("agent_slug") or "").strip() or bot.agent_slug
        await _validate_agent_binding(db, effective_slug, effective_owner)
    if "credentials" in payload:
        bot.credentials_encrypted = encrypt_credentials(bot.platform, payload.get("credentials") or {})
    if "name" in payload:
        bot.name = str(payload.get("name") or "").strip() or bot.name
    if "agent_slug" in payload:
        bot.agent_slug = str(payload.get("agent_slug") or "").strip() or bot.agent_slug
    if "enabled" in payload:
        bot.enabled = bool(payload.get("enabled"))
    if "allowed_users" in payload:
        bot.allowed_users = _normalize_allowed_users(payload.get("allowed_users"))
    if "require_mention" in payload:
        bot.require_mention = bool(payload.get("require_mention"))
    if "group_sessions_per_user" in payload:
        bot.group_sessions_per_user = bool(payload.get("group_sessions_per_user"))
    bot.updated_by = username
    await db.flush()
    await db.refresh(bot)
    return bot


async def set_im_bot_enabled(db: AsyncSession, bot_id: int, enabled: bool, username: str) -> IMBotConfig:
    """启用 / 停用机器人。"""
    bot = await get_im_bot(db, bot_id)
    if bot is None:
        raise ValueError("机器人配置不存在")
    bot.enabled = bool(enabled)
    bot.updated_by = username
    await db.flush()
    await db.refresh(bot)
    return bot


async def delete_im_bot(db: AsyncSession, bot_id: int) -> bool:
    """删除机器人配置并回收其 API Key。"""
    bot = await get_im_bot(db, bot_id)
    if bot is None:
        return False
    # 先清空外键引用并删除 Bot，再回收 API Key，避免 flush 时触发 api_keys 外键约束。
    key_id = bot.api_key_id
    bot.api_key_id = None
    await db.flush()
    await _delete(db, bot)
    await _revoke_api_key(db, key_id)
    return True


async def list_im_bots(db: AsyncSession) -> list[dict]:
    """返回全部机器人配置的序列化结果。"""
    bots = await _list(db)
    return [bot.to_dict() for bot in bots]


async def get_gateway_configs(db: AsyncSession) -> list[dict]:
    """返回启用中的机器人配置，含解密后的凭证与 API Key 明文，供网关使用。"""
    configs = []
    for bot in await _list(db):
        if not bot.enabled:
            continue
        # api_key_id 为 NULL 说明关联的 API Key 已被吊销，此时 cipher 是陈旧数据，不能再对外提供。
        if bot.api_key_id is None:
            continue
        if not bot.api_key_cipher:
            continue
        try:
            configs.append(
                {
                    "id": bot.id,
                    "platform": bot.platform,
                    "name": bot.name,
                    "credentials": decrypt_credentials(bot.platform, bot.credentials_encrypted),
                    "api_key": decrypt_secret(bot.api_key_cipher),
                    "agent_slug": bot.agent_slug,
                    "allowed_users": list(bot.allowed_users or []),
                    "require_mention": bool(bot.require_mention),
                    "group_sessions_per_user": bool(bot.group_sessions_per_user),
                }
            )
        except Exception as exc:
            # 单个机器人密文损坏（密钥轮换 / 数据被篡改）时只跳过该机器人，避免拖垮整个网关配置接口。
            logger.warning(f"跳过无法解密的 IM 机器人配置 {bot.id}: {exc}")
            continue
    return configs
