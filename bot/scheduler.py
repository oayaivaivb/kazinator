from datetime import datetime

from aiogram import Bot
from sqlalchemy import select

from bot.database.engine import async_session
from bot.database.models import Raffle
from bot.services.raffle import finish_raffle


async def check_deadlines(bot: Bot):
    """Планировщик (каждую минуту): ищет истёкшие и завершает их."""
    async with async_session() as session:
        result = await session.execute(
            select(Raffle).where(
                Raffle.is_finished == False,
                Raffle.deadline_at <= datetime.now(),
            )
        )
        expired_raffles = result.scalars().all()
        for raffle in expired_raffles:
            await finish_raffle(raffle.id, session, bot)
