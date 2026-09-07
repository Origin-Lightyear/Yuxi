"""本地 Cron 调度执行器。

Tenant 服务只保存任务定义（AGENT_CLIENT_API.md §6），到点执行在 Agent 侧完成。
本组件随 worker 进程启动，周期性拉取启用任务，按五段式 Cron + IANA 时区
计算触发时刻，到点后以任务属主（SaaS 员工对应的本地用户）身份创建 agent run。

约定：任务 ``name`` 即要触发的 agent slug，每个任务使用专属线程
``schedule:{scheduleId}``，结果消息走既有落库与双写链路。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select

from yuxi.config import config as app_config
from yuxi.repositories.conversation_repository import ConversationRepository
from yuxi.services.agent_data_client import AgentDataClient
from yuxi.services.agent_run_service import AgentRunWaitTimeout, await_agent_run_result, create_agent_run_view
from yuxi.services.input_message_service import build_chat_input_message
from yuxi.services.schedule_service import resolve_schedule_agent_slug, schedule_thread_id
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import UserConfig
from yuxi.utils.logging_config import logger

POLL_INTERVAL_SECONDS = 30


def next_fire_time(cron_expression: str, tz_name: str, after: datetime) -> datetime:
    """计算 Cron 在指定 IANA 时区的下一次触发时刻（返回 aware UTC 时间）。"""
    tz = ZoneInfo(tz_name)
    cron = croniter(cron_expression, after.astimezone(tz))
    return cron.get_next(datetime).astimezone(UTC)


class ScheduleRunner:
    """单实例调度循环；同一任务上一个 run 未结束时跳过本次触发（防重入）。"""

    def __init__(self, poll_interval: float = POLL_INTERVAL_SECONDS):
        self._poll_interval = poll_interval
        self._loop_task: asyncio.Task | None = None
        # (tenant_id, employee_id, schedule_id) -> 下次触发时间（aware UTC）
        self._next_fire: dict[tuple[int, int, int], datetime] = {}
        # (tenant_id, employee_id, schedule_id) -> 是否正在执行
        self._running: set[tuple[int, int, int]] = set()

    def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._run_loop())
            logger.info("ScheduleRunner started")

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
            logger.info("ScheduleRunner stopped")

    async def _run_loop(self) -> None:
        while True:
            try:
                if app_config.is_agent_data_sync_enabled:
                    await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"ScheduleRunner tick failed: {exc}")
            await asyncio.sleep(self._poll_interval)

    async def _tick(self) -> None:
        """拉取全部 SaaS 员工的启用任务并触发到点者。"""
        now = datetime.now(UTC)
        async with pg_manager.get_async_session_context() as db:
            result = await db.execute(
                select(UserConfig).where(UserConfig.tenant_id.is_not(None), UserConfig.employee_id.is_not(None))
            )
            contexts = [(record.uid, record.tenant_id, record.employee_id) for record in result.scalars().all()]

        for uid, tenant_id, employee_id in contexts:
            try:
                client = AgentDataClient(tenant_id, employee_id)
                schedules = await client.list_schedules()
            except Exception as exc:
                logger.warning(f"ScheduleRunner: list schedules failed for tenant={tenant_id}: {exc}")
                continue
            for schedule in schedules:
                if not schedule.get("enabled", True):
                    continue
                self._check_and_fire(uid, tenant_id, employee_id, schedule, now)

    def _check_and_fire(self, uid: str, tenant_id: int, employee_id: int, schedule: dict, now: datetime) -> None:
        key = (tenant_id, employee_id, int(schedule["id"]))
        next_at = self._next_fire.get(key)
        if next_at is None:
            self._next_fire[key] = next_fire_time(str(schedule["cronExpression"]), str(schedule["timezone"]), now)
            return
        if now < next_at or key in self._running:
            return
        self._next_fire[key] = next_fire_time(str(schedule["cronExpression"]), str(schedule["timezone"]), next_at)
        self._running.add(key)
        asyncio.create_task(self._execute(uid, tenant_id, employee_id, schedule, key))

    async def _execute(
        self,
        uid: str,
        tenant_id: int,
        employee_id: int,
        schedule: dict[str, Any],
        key: tuple[int, int, int],
    ) -> None:
        schedule_id = key[2]
        try:
            agent_slug = resolve_schedule_agent_slug(schedule)
            if not agent_slug:
                logger.warning(f"ScheduleRunner: schedule {schedule_id} has empty name (agent slug), skipped")
                return

            prompt = str(schedule.get("prompt") or "").strip()
            if not prompt:
                logger.warning(f"ScheduleRunner: schedule {schedule_id} has empty prompt, skipped")
                return

            thread_id = schedule_thread_id(schedule_id)
            async with pg_manager.get_async_session_context() as db:
                conv_repo = ConversationRepository(db)
                conversation = await conv_repo.get_conversation_by_thread_id(thread_id)
                if conversation is None:
                    conversation = await conv_repo.create_conversation(
                        uid=uid,
                        agent_id=agent_slug,
                        title=str(schedule.get("name") or "定时任务"),
                        thread_id=thread_id,
                        metadata={"source": "schedule", "schedule_id": schedule_id},
                    )
                elif conversation.agent_id != agent_slug:
                    logger.warning(
                        f"ScheduleRunner: thread {thread_id} bound to agent {conversation.agent_id}, "
                        f"schedule now points to {agent_slug}, skipped"
                    )
                    return

                run = await create_agent_run_view(
                    input_message=build_chat_input_message(prompt),
                    agent_slug=agent_slug,
                    thread_id=thread_id,
                    meta={"request_id": str(uuid.uuid4()), "source": "schedule"},
                    current_uid=uid,
                    db=db,
                    source="schedule",
                )

            result = await await_agent_run_result(run_id=run["run_id"], current_uid=uid)
            logger.info(
                f"ScheduleRunner: schedule {schedule_id} run {run['run_id']} finished with status "
                f"{result.get('status')}"
            )
        except AgentRunWaitTimeout as exc:
            logger.warning(f"ScheduleRunner: schedule {schedule_id} run wait timeout: {exc}")
        except Exception as exc:
            logger.warning(f"ScheduleRunner: schedule {schedule_id} execution failed: {exc}")
        finally:
            self._running.discard(key)


schedule_runner = ScheduleRunner()
