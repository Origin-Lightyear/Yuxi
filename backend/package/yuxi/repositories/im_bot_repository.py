"""IM 机器人配置数据访问层。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from yuxi.storage.postgres.models_business import IMBotConfig


async def list_im_bots(db: AsyncSession) -> list[IMBotConfig]:
    """按 id 升序返回全部机器人配置。"""
    result = await db.execute(select(IMBotConfig).order_by(IMBotConfig.id.asc()))
    return list(result.scalars().all())


async def get_im_bot(db: AsyncSession, bot_id: int) -> IMBotConfig | None:
    """按 id 获取机器人配置。"""
    result = await db.execute(select(IMBotConfig).where(IMBotConfig.id == bot_id))
    return result.scalar_one_or_none()


async def create_im_bot(db: AsyncSession, data: dict) -> IMBotConfig:
    """创建机器人配置（只 flush，提交交给调用方）。"""
    bot = IMBotConfig(**data)
    db.add(bot)
    await db.flush()
    await db.refresh(bot)
    return bot


async def delete_im_bot(db: AsyncSession, bot: IMBotConfig) -> None:
    """删除机器人配置。"""
    await db.delete(bot)
    await db.flush()
