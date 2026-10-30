"""Хендлеры администратора: создание розыгрыша, досрочное завершение, бан, экспорт."""

import io
from datetime import datetime

import openpyxl
from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.text_decorations import html_decoration as html
from openpyxl.utils import get_column_letter
from sqlalchemy import func, select, update

from bot.database.engine import async_session
from bot.database.models import Raffle, User
from bot.filters.is_admin import IsAdmin
from bot.services.raffle import finish_raffle
from bot.states.admin import BanUser, NewRaffle

admin_router = Router()

# Все сообщения в этом роутере будут обрабатываться только для админов
admin_router.message.filter(IsAdmin())
# Применяем фильтр и к коллбекам (нажатиям кнопок), чтобы обычные юзеры не могли их дергать
admin_router.callback_query.filter(IsAdmin())


@admin_router.message(CommandStart())
async def admin_start(message: Message, state: FSMContext):
    """Главное меню администратора."""
    await state.clear()  # Сбрасываем состояния на случай, если админ застрял на каком-то шаге
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚙️ Настроить новый розыгрыш", callback_data="admin_new_raffle")],
            [InlineKeyboardButton(text="🛑 Завершить досрочно", callback_data="admin_finish_early")],
            [InlineKeyboardButton(text="📊 Выгрузить базу", callback_data="admin_export")],
            [InlineKeyboardButton(text="🚫 Забанить/Разбанить", callback_data="admin_ban")],
        ]
    )
    await message.answer("🛠 Панель администратора. Выберите действие:", reply_markup=keyboard)


# --- СЦЕНАРИЙ: НАСТРОИТЬ НОВЫЙ РОЗЫГРЫШ ---


@admin_router.callback_query(F.data == "admin_new_raffle")
async def start_new_raffle(callback: CallbackQuery, state: FSMContext):
    """Переход к шагу ввода количества победителей."""
    await callback.message.edit_text("Шаг 1. Введите количество победителей (число):")
    await state.set_state(NewRaffle.winner_slots)


@admin_router.message(NewRaffle.winner_slots)
async def process_winner_slots(message: Message, state: FSMContext):
    """Принимаем число победителей (должно быть > 0)."""
    if not message.text.isdigit() or int(message.text) <= 0:
        return await message.answer("❌ Ошибка. Введите целое число больше нуля:")
    await state.update_data(winner_slots=int(message.text))
    await message.answer("Шаг 2. Введите дату и время окончания (пример: 19.04.2024 18:00):")
    await state.set_state(NewRaffle.deadline)


@admin_router.message(NewRaffle.deadline)
async def process_deadline(message: Message, state: FSMContext):
    """Парсим дату и проверяем, что она в будущем."""
    try:
        deadline_dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")
        if deadline_dt <= datetime.now():
            return await message.answer("❌ Время должно быть в будущем. Попробуйте еще раз:")
        await state.update_data(deadline=deadline_dt.isoformat())
        await message.answer(
            "Шаг 3. Введите ссылку на канал для подписки (пример: https://t.me/... или @...)\n\n"
            "Если подписка не нужна, отправьте '-'."
        )
        await state.set_state(NewRaffle.channel)
    except ValueError:
        await message.answer("❌ Неверный формат. Используйте пример: 19.04.2024 18:00")


@admin_router.message(NewRaffle.channel)
async def process_channel(message: Message, state: FSMContext):
    """Проверяем формат канала и показываем сводку для подтверждения."""
    channel = message.text.strip()
    require_subscribe = False
    if channel != "-":
        if not (channel.startswith("@") or channel.startswith("https://t.me/")):
            return await message.answer(
                "❌ Неверный формат ссылки. Используйте @username или https://t.me/... (или '-' для пропуска)."
            )
        require_subscribe = True
    else:
        channel = None

    await state.update_data(channel=channel, require_subscribe=require_subscribe)

    # Формируем сводку
    data = await state.get_data()
    deadline_dt = datetime.fromisoformat(data["deadline"])
    summary = (
        "📋 <b>Сводка по розыгрышу:</b>\n"
        f"Победителей: {data['winner_slots']}\n"
        f"Окончание: {deadline_dt.strftime('%d.%m.%Y %H:%M')}\n"
        f"Канал: {data['channel'] if data['channel'] else 'Нет'}"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить и открыть", callback_data="confirm_new_raffle")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_new_raffle")],
        ]
    )

    # Используем parse_mode="HTML" для жирного текста
    await message.answer(summary, reply_markup=keyboard, parse_mode="HTML")
    await state.set_state(NewRaffle.confirm)


@admin_router.callback_query(F.data == "confirm_new_raffle", NewRaffle.confirm)
async def confirm_new_raffle(callback: CallbackQuery, state: FSMContext):
    """Создаёт запись Raffle в БД и разблокирует регистрацию."""
    data = await state.get_data()
    # Записываем в БД
    deadline_dt = datetime.fromisoformat(data["deadline"])
    async with async_session() as session:
        new_raffle = Raffle(
            winner_slots=data["winner_slots"],
            deadline_at=deadline_dt,
            channel_username=data["channel"],
            require_subscribe=data["require_subscribe"],
            is_finished=False,
            # Оставляем комментарии выключенными по умолчанию, как договаривались в упрощенном ТЗ
            require_comment=False,
        )
        session.add(new_raffle)
        await session.commit()

    await callback.message.edit_text("✅ Розыгрыш успешно создан! Регистрация открыта.")
    await state.clear()


@admin_router.callback_query(F.data == "cancel_new_raffle", NewRaffle.confirm)
async def cancel_new_raffle(callback: CallbackQuery, state: FSMContext):
    """Отменяет создание розыгрыша и сбрасывает состояние."""
    await callback.message.edit_text("❌ Создание розыгрыша отменено.")
    await state.clear()


# --- СЦЕНАРИЙ: ЭКСПОРТ В EXCEL ---


@admin_router.callback_query(F.data == "admin_export")
async def export_excel(callback: CallbackQuery):
    """Выгружает всех участников последнего розыгрыша в Excel (в памяти, без дисковых записей)."""
    await callback.message.answer("⏳ Собираю данные, подождите...")

    async with async_session() as session:
        # Ищем последний (текущий или только что завершенный) розыгрыш
        latest_raffle = (await session.execute(select(Raffle).order_by(Raffle.id.desc()).limit(1))).scalars().first()

        if not latest_raffle:
            await callback.answer()
            return await callback.message.answer("База данных пуста.")

        result = await session.execute(select(User).where(User.raffle_id == latest_raffle.id).order_by(User.id.desc()))
        rows = result.scalars().all()

    if not rows:
        await callback.answer()
        return await callback.message.answer("В последнем розыгрыше нет участников.")

    # Создаем Excel в памяти
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Участники"

    # Заголовки
    headers = ["ID", "Telegram ID", "Username", "ФИО", "ВУЗ", "ID Розыгрыша", "Подписка", "Бан", "Дата регистрации"]
    ws.append(headers)

    for user in rows:
        ws.append(
            [
                user.id,
                user.telegram_id,
                f"@{user.username.lstrip('@')}" if user.username else "",
                user.fio,
                user.university,
                user.raffle_id,
                "Да" if user.is_subscribed else "Нет",
                "ЗАБАНЕН" if user.is_banned else "Нет",
                user.registered_at.strftime("%d.%m.%Y %H:%M"),
            ]
        )

    # Включаем автофильтр по всем колонкам (включая ФИО и Username)
    last_col = get_column_letter(ws.max_column)
    ws.auto_filter.ref = f"A1:{last_col}{ws.max_row}"

    # Сохраняем в буфер
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    excel_file = BufferedInputFile(buffer.read(), filename="raffle_users.xlsx")
    await callback.message.answer_document(document=excel_file, caption="📊 Выгрузка базы участников.")
    await callback.answer()


# --- СЦЕНАРИЙ: БАН / РАЗБАН ---


@admin_router.callback_query(F.data == "admin_ban")
async def start_ban(callback: CallbackQuery, state: FSMContext):
    """Запрашивает Telegram ID или username в формате @username."""
    await callback.message.answer(
        "Введите Telegram ID (число) или username в формате @username, " "которого нужно забанить или разбанить:"
    )
    await state.set_state(BanUser.waiting_for_id)
    await callback.answer()


@admin_router.message(BanUser.waiting_for_id)
async def process_ban_id(message: Message, state: FSMContext):
    """Ищет пользователя по telegram_id или username (@формат) и переключает is_banned."""
    # Хендлер ловит любое сообщение, включая стикеры и фото (text=None)
    raw = (message.text or "").strip()
    if not raw:
        return await message.answer("❌ Введите Telegram ID (число) или username (@формат).")

    # Числовой ввод — ищем по telegram_id
    if raw.isdigit():
        lookup_value = int(raw)
        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == lookup_value).limit(1))
            user = result.scalars().first()

            if not user:
                await state.clear()
                return await message.answer("Пользователь с таким Telegram ID не найден в базе.")

            new_status = not user.is_banned
            await session.execute(update(User).where(User.telegram_id == user.telegram_id).values(is_banned=new_status))
            await session.commit()
            fio = user.fio

    # Ввод начинается с @ — ищем по username (регистронезависимо)
    elif raw.startswith("@"):
        lookup_value = raw.lstrip("@").lower()
        async with async_session() as session:
            result = await session.execute(select(User).where(func.lower(User.username) == lookup_value).limit(1))
            user = result.scalars().first()

            if not user:
                await state.clear()
                return await message.answer(f"Пользователь с username «{raw}» не найден в базе.")

            new_status = not user.is_banned
            await session.execute(update(User).where(User.telegram_id == user.telegram_id).values(is_banned=new_status))
            await session.commit()
            fio = user.fio

    else:
        return await message.answer("❌ Введите Telegram ID (число) или username в формате @username.")

    action = "забанен 🚫" if new_status else "разбанен ✅"
    await message.answer(f"Пользователь <b>{html.quote(fio)}</b> был {action}.", parse_mode="HTML")
    await state.clear()


# --- СЦЕНАРИЙ: ДОСРОЧНОЕ ЗАВЕРШЕНИЕ ---


@admin_router.callback_query(F.data == "admin_finish_early")
async def ask_finish_early(callback: CallbackQuery):
    """Подтверждение досрочного завершения (двойной клик)."""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚠️ ДА, завершить сейчас", callback_data="confirm_finish_early")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_finish_early")],
        ]
    )
    await callback.message.edit_text(
        "Вы уверены? Это сразу закроет регистрацию и выберет победителей.",
        reply_markup=keyboard,
    )


@admin_router.callback_query(F.data == "cancel_finish_early")
async def cancel_finish_early(callback: CallbackQuery):
    """Отмена досрочного завершения."""
    await callback.message.edit_text("Действие отменено.")


@admin_router.callback_query(F.data == "confirm_finish_early")
async def do_finish_early(callback: CallbackQuery, bot: Bot):
    """Находит активный розыгрыш и вызывает finish_raffle."""
    await callback.message.edit_text("⏳ Подвожу итоги...")

    async with async_session() as session:
        active_raffle = (
            (await session.execute(select(Raffle).where(Raffle.is_finished == False).order_by(Raffle.id.desc())))
            .scalars()
            .first()
        )

        if not active_raffle:
            return await callback.message.edit_text("❌ Нет активных розыгрышей для завершения.")

        success = await finish_raffle(active_raffle.id, session, bot)

    if success:
        await callback.message.answer("✅ Розыгрыш успешно завершен. Результаты отправлены.")
    else:
        await callback.message.answer("❌ Ошибка при завершении розыгрыша.")
