import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.redis import RedisStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.config import BOT_TOKEN, REDIS_URL
from bot.handlers.admin import admin_router
from bot.handlers.user import user_router
from bot.scheduler import check_deadlines

logging.basicConfig(level=logging.INFO)


async def main():
    """Точка запуска бота: сборка стека и старт поллинга."""
    bot = Bot(token=BOT_TOKEN)
    # FSM-состояния храним в Redis, чтобы перезапуск бота не сбрасывал анкеты
    storage = RedisStorage.from_url(REDIS_URL)
    dp = Dispatcher(storage=storage)

    # Планировщик проверяет дедлайны розыгрышей каждую минуту
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_deadlines, "cron", minute="*", kwargs={"bot": bot})
    scheduler.start()

    # Порядок важен: фильтры админов ставятся первыми, чтобы перехватить
    # команды типа /admin до того, как их поглотит user_router
    dp.include_router(admin_router)
    dp.include_router(user_router)

    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
