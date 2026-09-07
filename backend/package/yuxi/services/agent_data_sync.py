"""Agent 数据双写同步（fail-soft）。

SaaS 模式下把本地会话与消息镜像到 Tenant 服务（AGENT_CLIENT_API.md §3-§5）：
本地 Postgres 为主存储，Tenant 会话统一放在其默认目录。所有同步失败只记
warning，绝不影响本地主链路；Tenant 侧映射保存在 Conversation.extra_metadata
的 agent_data_sync 键下，Tenant 消息 ID 保存在 Message.extra_metadata 的
tenant_message_id 键下。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from yuxi.config import config as app_config
from yuxi.services.agent_data_client import AgentDataClient
from yuxi.services.saas_client import SaasAPIError
from yuxi.services.saas_identity import SaasEmployeeContext, get_saas_employee_context
from yuxi.storage.postgres.models_business import Conversation, Message
from yuxi.utils.logging_config import logger

SYNC_META_KEY = "agent_data_sync"
TENANT_MESSAGE_ID_KEY = "tenant_message_id"
VERSION_CONFLICT_CODE = 20308
CONVERSATION_NOT_FOUND_CODE = 20306

# clientMessageId 命名空间：由 request_id + role 稳定派生，重试天然幂等（§5.5）
_CLIENT_MESSAGE_NS = uuid.UUID("8f2e7a1c-4b6d-4c8e-9a3f-2d5b8c1e7a42")


def derive_client_message_id(request_id: str | None, role: str) -> str:
    seed = request_id or ""
    return str(uuid.uuid5(_CLIENT_MESSAGE_NS, f"{seed}:{role}"))


async def _resolve_context(db: AsyncSession, uid: str) -> SaasEmployeeContext | None:
    """双写开关关闭或该用户无 SaaS 身份时返回 None，调用方直接跳过同步。"""
    if not app_config.is_agent_data_sync_enabled:
        return None
    return await get_saas_employee_context(db, uid)


def _read_sync_meta(conversation: Conversation) -> dict[str, Any]:
    meta = (conversation.extra_metadata or {}).get(SYNC_META_KEY)
    return dict(meta) if isinstance(meta, dict) else {}


def _write_sync_meta(db: AsyncSession, conversation: Conversation, **fields: Any) -> None:
    """合并写入 Tenant 映射字段（不提交，由调用方统一 commit）。"""
    metadata = dict(conversation.extra_metadata or {})
    meta = _read_sync_meta(conversation)
    meta.update(fields)
    conversation.extra_metadata = {**metadata, SYNC_META_KEY: meta}
    try:
        flag_modified(conversation, "extra_metadata")
    except (KeyError, AttributeError):
        # 非 ORM 实例（如测试替身）无法打标，直接赋值已足够
        pass


async def _ensure_tenant_conversation(db: AsyncSession, client: AgentDataClient, conversation: Conversation) -> int:
    """确保 Tenant 侧会话存在（惰性创建到默认目录），返回会话 ID。"""
    meta = _read_sync_meta(conversation)
    if meta.get("conversation_id") is not None:
        return int(meta["conversation_id"])

    data = await client.create_conversation(title=conversation.title)
    _write_sync_meta(
        db,
        conversation,
        conversation_id=int(data["id"]),
        version=int(data.get("version") or 1),
    )
    await db.commit()
    return int(data["id"])


async def _store_tenant_message_id(db: AsyncSession, message: Message, tenant_message_id: int) -> None:
    metadata = dict(message.extra_metadata or {})
    metadata[TENANT_MESSAGE_ID_KEY] = tenant_message_id
    message.extra_metadata = metadata
    try:
        flag_modified(message, "extra_metadata")
    except (KeyError, AttributeError):
        pass


def _consumed_score(message: Message) -> int:
    """整轮合计积分：最终 assistant 消息 usage_metadata 的 input + output（§5.3）。"""
    usage = (message.extra_metadata or {}).get("usage_metadata") or {}
    if not isinstance(usage, dict):
        return 0
    return int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)


def _append_message(
    client: AgentDataClient,
    conversation_id: int,
    message: Message,
    role: str,
    reply_to_message_id: int | None,
    consumed_score: int,
) -> dict[str, Any]:
    return client.append_message(
        conversation_id=conversation_id,
        client_message_id=derive_client_message_id(message.request_id, role),
        role=role,
        message_type="TEXT",
        content=message.content,
        reply_to_message_id=reply_to_message_id,
        consumed_score=consumed_score,
    )


# ----------------------------------------------------------------------
# 线程生命周期同步
# ----------------------------------------------------------------------


async def sync_thread_created(db: AsyncSession, conversation: Conversation) -> None:
    """本地线程创建后，在 Tenant 默认目录创建对应会话。"""
    ctx = await _resolve_context(db, conversation.uid)
    if ctx is None:
        return
    try:
        client = AgentDataClient(ctx.tenant_id, ctx.employee_id)
        await _ensure_tenant_conversation(db, client, conversation)
    except Exception as exc:
        logger.warning(f"Agent data sync: create conversation failed for {conversation.thread_id}: {exc}")


async def sync_thread_renamed(db: AsyncSession, conversation: Conversation) -> None:
    """本地线程改名后同步 Tenant 会话标题；版本冲突时重拉一次再重试。"""
    ctx = await _resolve_context(db, conversation.uid)
    if ctx is None:
        return
    meta = _read_sync_meta(conversation)
    if meta.get("conversation_id") is None:
        return
    conversation_id = int(meta["conversation_id"])
    client = AgentDataClient(ctx.tenant_id, ctx.employee_id)
    try:
        data = await client.update_conversation(
            conversation_id, version=int(meta.get("version") or 1), title=conversation.title
        )
        _write_sync_meta(db, conversation, version=int(data.get("version") or 1))
        await db.commit()
    except SaasAPIError as exc:
        if exc.code != VERSION_CONFLICT_CODE:
            logger.warning(f"Agent data sync: rename conversation {conversation_id} failed: {exc}")
            return
        try:
            latest = await client.get_conversation(conversation_id)
            data = await client.update_conversation(
                conversation_id, version=int(latest.get("version") or 1), title=conversation.title
            )
            _write_sync_meta(db, conversation, version=int(data.get("version") or 1))
            await db.commit()
        except Exception as retry_exc:
            logger.warning(f"Agent data sync: rename conversation {conversation_id} retry failed: {retry_exc}")
    except Exception as exc:
        logger.warning(f"Agent data sync: rename conversation {conversation_id} failed: {exc}")


async def sync_thread_deleted(db: AsyncSession, conversation: Conversation) -> None:
    """本地线程删除后删除 Tenant 侧会话；本地软删，Tenant 硬删（§1.3）。"""
    ctx = await _resolve_context(db, conversation.uid)
    if ctx is None:
        return
    meta = _read_sync_meta(conversation)
    if meta.get("conversation_id") is None:
        return
    conversation_id = int(meta["conversation_id"])
    try:
        client = AgentDataClient(ctx.tenant_id, ctx.employee_id)
        await client.delete_conversation(conversation_id, version=int(meta.get("version") or 1))
    except SaasAPIError as exc:
        if exc.code != CONVERSATION_NOT_FOUND_CODE:
            logger.warning(f"Agent data sync: delete conversation {conversation_id} failed: {exc}")
    except Exception as exc:
        logger.warning(f"Agent data sync: delete conversation {conversation_id} failed: {exc}")


# ----------------------------------------------------------------------
# 消息同步
# ----------------------------------------------------------------------


async def sync_user_message(db: AsyncSession, conversation: Conversation, message: Message) -> None:
    """USER 消息落库后同步到 Tenant，并记录 Tenant 消息 ID 供回复关联。"""
    ctx = await _resolve_context(db, conversation.uid)
    if ctx is None:
        return
    try:
        client = AgentDataClient(ctx.tenant_id, ctx.employee_id)
        conversation_id = await _ensure_tenant_conversation(db, client, conversation)
        data = await _append_message(client, conversation_id, message, "USER", None, 0)
        tenant_message = data.get("message") or {}
        await _store_tenant_message_id(db, message, int(tenant_message.get("id") or 0))
        _write_sync_meta(db, conversation, version=int(data.get("conversationVersion") or 1))
        await db.commit()
    except Exception as exc:
        logger.warning(f"Agent data sync: append USER message failed for {conversation.thread_id}: {exc}")


async def sync_assistant_message(
    db: AsyncSession, conversation: Conversation, message: Message, run_id: str | None
) -> None:
    """最终 assistant 回复落库后同步到 Tenant，写入整轮 consumedScore 并关联 USER 消息。"""
    ctx = await _resolve_context(db, conversation.uid)
    if ctx is None:
        return
    try:
        client = AgentDataClient(ctx.tenant_id, ctx.employee_id)
        conversation_id = await _ensure_tenant_conversation(db, client, conversation)

        reply_to = None
        if run_id:
            result = await db.execute(
                select(Message)
                .where(Message.run_id == run_id, Message.role == "user")
                .order_by(Message.id.desc())
                .limit(1)
            )
            user_message = result.scalar_one_or_none()
            if user_message is not None:
                reply_to = (user_message.extra_metadata or {}).get(TENANT_MESSAGE_ID_KEY)

        data = await _append_message(
            client,
            conversation_id,
            message,
            "ASSISTANT",
            int(reply_to) if reply_to else None,
            _consumed_score(message),
        )
        _write_sync_meta(db, conversation, version=int(data.get("conversationVersion") or 1))
        await db.commit()
    except Exception as exc:
        logger.warning(f"Agent data sync: append ASSISTANT message failed for {conversation.thread_id}: {exc}")
