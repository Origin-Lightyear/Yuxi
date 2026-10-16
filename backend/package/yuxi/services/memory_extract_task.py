"""memory_extract durable task：开关短路的 LLM 记忆抽取、幂等写入与容量截断。"""

from __future__ import annotations

import json_repair
from sqlalchemy.exc import IntegrityError

from yuxi.models.chat import select_model
from yuxi.repositories.agent_memory_repository import AgentMemoryRepository
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.services.agent_memory_service import is_memory_enabled
from yuxi.services.task_service import TaskContext
from yuxi.storage.postgres.manager import pg_manager
from yuxi.utils.logging_config import logger

EXTRACT_MAX_ITEMS = 10
EXTRACT_MAX_CHARS = 100
AGENT_MEMORY_LIMIT = 50
CONVERSATION_TEXT_MAX_CHARS = 8000

EXTRACT_SYSTEM_PROMPT = (
    "你是用户记忆抽取器。从给出的对话中抽取值得该智能体长期记住的用户事实或偏好，"
    "只抽取用户明确表达的内容，不要凭空推测；每条用中文、不超过 100 字、独立成条。"
    "没有可抽取的内容时输出空 JSON 数组 []。"
    '只输出 JSON 数组，格式如 ["条目1", "条目2"]，不要输出任何其他文字。'
)


async def run_memory_extract(context: TaskContext) -> dict:
    """按持久 payload 执行一次抽取：开关短路、读取对话、LLM 抽取、幂等写入并截断容量。"""
    payload = context.payload
    uid = payload["uid"]
    agent_slug = payload["agent_slug"]
    thread_id = payload["thread_id"]
    run_id = payload["run_id"]
    model_spec = payload["model_spec"]

    if not await is_memory_enabled(uid):
        return {"skipped": True}

    await context.set_progress(10.0, "读取对话历史")
    async with pg_manager.get_async_session_context() as db:
        history = await ConversationRepository(db).read_memory_messages(
            uid=uid,
            thread_id=thread_id,
            limit=20,
            include_tools=False,
        )
    messages = history.get("messages") or []
    if not messages:
        return {"skipped": True}

    items = await extract_memory_items(messages=messages, model_spec=model_spec)

    await context.raise_if_cancelled()
    await context.set_progress(60.0, "写入记忆条目")
    extracted = 0
    async with pg_manager.get_async_session_context() as db:
        repository = AgentMemoryRepository(db)
        for item in items:
            try:
                await repository.add(uid=uid, agent_slug=agent_slug, content=item, source_run_id=run_id)
                await db.commit()
                extracted += 1
            except IntegrityError:
                # add 只 flush；唯一冲突会让事务失效，必须回滚后跳过该条，已提交的前序条目不受影响。
                await db.rollback()
                logger.warning("记忆 source_run_id 冲突，跳过该条: run_id=%s", run_id)
        await repository.delete_oldest_over_limit(uid=uid, agent_slug=agent_slug, limit=AGENT_MEMORY_LIMIT)

    await context.set_progress(100.0, f"记忆抽取完成，新增 {extracted} 条")
    return {"extracted": extracted}


async def extract_memory_items(*, messages: list, model_spec: str) -> list[str]:
    """调用一次模型把对话抽取为记忆条目；输出不可解析时记 warning 并降级为空集。"""
    conversation = "\n".join(f"{message['role']}: {message['content']}" for message in messages)
    response = await select_model(model_spec=model_spec).call(
        [
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": conversation[:CONVERSATION_TEXT_MAX_CHARS]},
        ],
        stream=False,
    )
    raw = response.content if response else ""
    try:
        parsed = json_repair.loads(raw)
    except Exception as exc:
        logger.warning("记忆抽取输出解析失败，降级为空集: %s", exc)
        return []
    if not isinstance(parsed, list):
        logger.warning("记忆抽取输出不是 JSON 数组，降级为空集: %.120s", raw)
        return []
    items: list[str] = []
    for entry in parsed:
        if not isinstance(entry, str):
            continue
        text = entry.strip()
        if not text:
            continue
        items.append(text[:EXTRACT_MAX_CHARS])
        if len(items) >= EXTRACT_MAX_ITEMS:
            break
    return items
