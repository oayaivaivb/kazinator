import asyncio
import random

from aiogram import Bot
from aiogram.types import BufferedInputFile
from aiogram.utils.text_decorations import html_decoration as html
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import ADMIN_IDS
from bot.database.models import Raffle, User, Winner
from bot.services.image_gen import generate_personal_image, generate_summary_image


async def finish_raffle(raffle_id: int, session: AsyncSession, bot: Bot) -> bool:
    """Завершает розыгрыш, выбирает победителей и рассылает результаты."""
    # 1. Блокируем розыгрыш
    raffle = (await session.execute(select(Raffle).where(Raffle.id == raffle_id))).scalars().first()
    if not raffle or raffle.is_finished:
        return False
    raffle.is_finished = True

    # 2. Выбираем валидных участников (забаненные в выборку не попадают)
    users_result = await session.execute(select(User).where(User.raffle_id == raffle_id, User.is_banned == False))
    users = list(users_result.scalars().all())

    # 3. Рандомизация (выбираем N уникальных, или всех, если участников меньше)
    winners_count = min(raffle.winner_slots, len(users))
    winners = random.sample(users, winners_count) if users else []

    # 4. Сохраняем победителей
    for winner in winners:
        session.add(Winner(raffle_id=raffle_id, user_id=winner.id))
    await session.commit()

    # 5. Формируем отчет для админов
    # fio/university — пользовательский ввод, экранируем, иначе parse_mode="HTML"
    # уронит send_message и админы молча не получат вообще ничего
    report = (
        f"🏁 <b>Розыгрыш #{raffle_id} завершен!</b>\n"
        f"👥 Всего участников: {len(users)}\n"
        f"🏆 Победителей: {winners_count}\n\n"
    )
    for i, w in enumerate(winners, 1):
        nick = f" — @{w.username}" if w.username else ""
        report += f"{i}. {html.quote(w.fio)}\n   {html.quote(w.university)}{nick} (ID: {w.telegram_id})\n"

    # Рассылка админам
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=report, parse_mode="HTML")
        except Exception:
            pass  # Игнорируем, если админ заблокировал бота

    # 6. Отправка картинок
    if winners:
        # Сводная картинка админам
        summary_buf = await generate_summary_image(winners)
        summary_bytes = summary_buf.read()  # читаем один раз, а не в цикле
        for admin_id in ADMIN_IDS:
            try:
                summary_file = BufferedInputFile(summary_bytes, filename="summary.png")
                await bot.send_photo(
                    chat_id=admin_id,
                    photo=summary_file,
                    caption="📊 Таблица победителей",
                )
            except Exception:
                pass

        # Личная рассылка победителям
        for i, winner in enumerate(winners, 1):
            try:
                personal_buf = await generate_personal_image(winner.fio, i, winner.username)
                personal_file = BufferedInputFile(personal_buf.read(), filename="congrats.png")
                await bot.send_photo(
                    chat_id=winner.telegram_id,
                    photo=personal_file,
                    caption="🎉 Поздравляем! Вы стали победителем нашего розыгрыша!",
                )
            except Exception:
                pass
            # Защита от FloodWait: пауза 50мс между сообщениями
            await asyncio.sleep(0.05)

    return True
