"""Хендлеры администратора: гибридная навигация (Reply + Inline), чистый чат, экспорт, бан."""

import io
from datetime import datetime, timedelta

import openpyxl
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
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


# --- ИНТЕРФЕЙС: тексты, клавиатуры ---

BTN_RAFFLES = "🎁 Управление розыгрышами"
BTN_USERS = "👥 База и модерация"

MAIN_MENU_TEXT = (
    "🛠 Панель администратора. Выберите раздел:\n\n"
    "ℹ️ <b>/help</b> — справка по панели, <b>/start</b> — сброс сценария и меню"
)
SECTION_RAFFLES_TEXT = "🎁 Управление розыгрышами. Выберите действие:"
SECTION_USERS_TEXT = "👥 База и модерация. Выберите действие:"

ADMIN_HELP_TEXT = (
    "🛠 <b>Справка администратора</b>\n\n"
    "<b>Навигация</b> — кнопки внизу чата:\n"
    f"• {BTN_RAFFLES} — создать розыгрыш, завершить досрочно\n"
    f"• {BTN_USERS} — выгрузка в Excel, блокировка участников\n\n"
    "<b>Создание розыгрыша</b> — три шага:\n"
    "1. число победителей\n"
    "2. дедлайн в формате ДД.ММ.ГГГГ ЧЧ:ММ — бот подсказывает готовый пример (сейчас + 1 час)\n"
    "3. канал (@username или ссылка https://t.me/..., «-» если подписка не нужна)\n"
    "Затем сводка с кнопкой подтверждения.\n\n"
    "<b>Чистый чат:</b> ваш ввод удаляется из истории, вопрос бота перезаписывается "
    "на месте. «❌ Отмена» на любом шаге возвращает в главное меню.\n\n"
    "<b>Блокировка</b>: ищите по Telegram ID (число) или @username (регистронезависимо). "
    "Бан закрывает все заявки аккаунта во всех розыгрышах; повторный ввод того же "
    "идентификатора разбанивает.\n\n"
    "<b>Экспорт</b>: .xlsx по последнему розыгрышу — колонки ID, Telegram ID, Username, "
    "ФИО, ВУЗ, розыгрыш, подписка, бан, дата. Во всех колонках работают автофильтры.\n\n"
    "<b>Команды:</b>\n"
    "• /start — меню и сброс незавершённого сценария\n"
    "• /help — этот текст"
)

# Статичная нижняя клавиатура: висит в чате постоянно, не исчезает после нажатия
ADMIN_REPLY_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_RAFFLES)], [KeyboardButton(text=BTN_USERS)]],
    resize_keyboard=True,
    input_field_placeholder="Выберите раздел",
)

# «Домашний экран» в inline-виде: в него возвращают все кнопки «Отмена»
HOME_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text=BTN_RAFFLES, callback_data="menu_raffles")],
        [InlineKeyboardButton(text=BTN_USERS, callback_data="menu_users")],
    ]
)

RAFFLES_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Создать новый", callback_data="admin_new_raffle")],
        [InlineKeyboardButton(text="🛑 Завершить досрочно", callback_data="admin_finish_early")],
    ]
)

USERS_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📊 Выгрузить в Excel", callback_data="admin_export")],
        [InlineKeyboardButton(text="🚫 Блокировка", callback_data="admin_ban")],
    ]
)

CANCEL_RAFFLE_KB = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_new_raffle")]]
)

CANCEL_BAN_KB = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_ban")]]
)

CONFIRM_RAFFLE_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить и открыть", callback_data="confirm_new_raffle")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_new_raffle")],
    ]
)

FINISH_CONFIRM_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ ДА, завершить сейчас", callback_data="confirm_finish_early")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_finish_early")],
    ]
)

# Шаги создания розыгрыша. Текст ошибки повторяет шаг, чтобы админ видел, что вводить,
# не листая историю чата.
DEADLINE_FORMAT = "%d.%m.%Y %H:%M"


def deadline_example() -> str:
    """Живой пример дедлайна: текущий момент + 1 час в том же формате, что парсит хендлер.

    Пример, предложенный ботом, заведомо проходит проверку «время в будущем»,
    поэтому его можно отправить как есть.
    """
    return (datetime.now() + timedelta(hours=1)).strftime(DEADLINE_FORMAT)


STEP_SLOTS = "Шаг 1. Введите количество победителей (число):"
STEP_CHANNEL = (
    "Шаг 3. Введите ссылку на канал для подписки (пример: https://t.me/... или @...)\n\n"
    "Если подписка не нужна, отправьте '-'."
)
ERR_SLOTS = "❌ Ошибка. Введите целое число больше нуля.\n\n" + STEP_SLOTS
ERR_CHANNEL = (
    "❌ Неверный формат ссылки. Используйте @username или https://t.me/... (или '-' для пропуска).\n\n" + STEP_CHANNEL
)


def step_deadline() -> str:
    """Шаг 2: пример даты пересчитывается на каждый показ."""
    return f"Шаг 2. Введите дату и время окончания (пример: {deadline_example()}):"


def err_deadline_format() -> str:
    return f"❌ Неверный формат. Используйте пример: {deadline_example()}\n\n" + step_deadline()


def err_deadline_past() -> str:
    return "❌ Время должно быть в будущем. Попробуйте еще раз:\n\n" + step_deadline()


BAN_PROMPT = "Введите Telegram ID (число) или username в формате @username, которого нужно забанить или разбанить:"
ERR_BAN_EMPTY = "❌ Введите Telegram ID (число) или username (@формат).\n\n" + BAN_PROMPT
ERR_BAN_FORMAT = "❌ Введите Telegram ID (число) или username в формате @username.\n\n" + BAN_PROMPT


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ «ЧИСТОГО ЧАТА» ---


async def _drop_admin_answer(message: Message) -> None:
    """Удаляет ответ админа из истории: в чате остаются только сообщения-шаги бота."""
    try:
        await message.delete()
    except TelegramAPIError:
        pass  # сообщение уже удалено или недоступно боту — не считаем это ошибкой


async def _show_step(
    bot: Bot,
    message: Message,
    prompt_msg_id: int | None,
    text: str,
    reply_markup: InlineKeyboardMarkup,
    **kwargs,
) -> int:
    """Перелистывает сообщение-вопрос: меняет его текст на следующий шаг вместо нового сообщения.

    Возвращает id сообщения, которое стало активным вопросом (его же сохраняем в FSM
    как prompt_msg_id, чтобы следующий шаг снова редактировал это же сообщение).
    """
    if prompt_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=prompt_msg_id,
                text=text,
                reply_markup=reply_markup,
                **kwargs,
            )
            return prompt_msg_id
        except TelegramBadRequest as exc:
            # Тот же текст + те же кнопки: Telegram отвечает 400 «message is not modified».
            # Ничего показывать не нужно — подсказка уже стоит на экране.
            if "message is not modified" in str(exc):
                return prompt_msg_id
        except TelegramAPIError:
            pass  # сообщение удалено или недоступны правки — покажем новым

    sent = await message.answer(text=text, reply_markup=reply_markup, **kwargs)
    return sent.message_id


# --- НАВИГАЦИЯ: /start, нижнее Reply-меню и inline-переходы ---


@admin_router.message(CommandStart())
async def admin_start(message: Message, state: FSMContext):
    """Выдаёт статичное нижнее меню и сбрасывает незавершённый сценарий."""
    await state.clear()
    await message.answer(MAIN_MENU_TEXT, reply_markup=ADMIN_REPLY_KB, parse_mode="HTML")


# /help регистрируем до пошаговых хендлеров: команда нужна и в разгар сценария,
# когда состояние уже стоит. Состояние при этом НЕ трогаем — вернёмся к шагу.
# ignore_case — чтобы /HELP и /Help тоже сработали (по умолчанию aiogram регистр чувствителен).
@admin_router.message(Command("help", ignore_case=True))
async def admin_help(message: Message):
    """Справка по админ-панели."""
    await message.answer(ADMIN_HELP_TEXT, parse_mode="HTML")


# Кнопки нижнего меню ловим в ЛЮБОМ состоянии (StateFilter("*")) и регистрируем раньше
# пошаговых хендлеров: переход по разделам важнее недособранной анкеты.
@admin_router.message(StateFilter("*"), F.text == BTN_RAFFLES)
async def open_raffles_section(message: Message, state: FSMContext):
    """Нижняя кнопка «Управление розыгрышами»."""
    await state.clear()
    await message.answer(SECTION_RAFFLES_TEXT, reply_markup=RAFFLES_KB)


@admin_router.message(StateFilter("*"), F.text == BTN_USERS)
async def open_users_section(message: Message, state: FSMContext):
    """Нижняя кнопка «База и модерация»."""
    await state.clear()
    await message.answer(SECTION_USERS_TEXT, reply_markup=USERS_KB)


@admin_router.callback_query(F.data == "menu_raffles")
async def show_raffles_section(callback: CallbackQuery, state: FSMContext):
    """Тот же переход в раздел розыгрышей, но по inline-кнопке (перелистыванием)."""
    await state.clear()
    await callback.message.edit_text(SECTION_RAFFLES_TEXT, reply_markup=RAFFLES_KB)
    await callback.answer()


@admin_router.callback_query(F.data == "menu_users")
async def show_users_section(callback: CallbackQuery, state: FSMContext):
    """Тот же переход в раздел модерации, но по inline-кнопке (перелистыванием)."""
    await state.clear()
    await callback.message.edit_text(SECTION_USERS_TEXT, reply_markup=USERS_KB)
    await callback.answer()


# --- СЦЕНАРИЙ: НАСТРОИТЬ НОВЫЙ РОЗЫГРЫШ ---


@admin_router.callback_query(F.data == "admin_new_raffle")
async def start_new_raffle(callback: CallbackQuery, state: FSMContext):
    """Шаг 1: превращает сообщение меню в вопрос и запоминает его id."""
    await state.clear()
    await state.set_state(NewRaffle.winner_slots)
    await state.update_data(prompt_msg_id=callback.message.message_id)
    await callback.message.edit_text(STEP_SLOTS, reply_markup=CANCEL_RAFFLE_KB)
    await callback.answer()


@admin_router.message(NewRaffle.winner_slots)
async def process_winner_slots(message: Message, state: FSMContext, bot: Bot):
    """Принимаем число победителей (должно быть > 0), не оставляя следов в чате."""
    data = await state.get_data()
    await _drop_admin_answer(message)

    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        return await _show_step(bot, message, data["prompt_msg_id"], ERR_SLOTS, CANCEL_RAFFLE_KB)

    await state.update_data(winner_slots=int(raw))
    new_id = await _show_step(bot, message, data["prompt_msg_id"], step_deadline(), CANCEL_RAFFLE_KB)
    await state.update_data(prompt_msg_id=new_id)
    await state.set_state(NewRaffle.deadline)


@admin_router.message(NewRaffle.deadline)
async def process_deadline(message: Message, state: FSMContext, bot: Bot):
    """Парсим дату и проверяем, что она в будущем."""
    data = await state.get_data()
    await _drop_admin_answer(message)

    raw = (message.text or "").strip()
    try:
        deadline_dt = datetime.strptime(raw, DEADLINE_FORMAT)
    except ValueError:
        return await _show_step(bot, message, data["prompt_msg_id"], err_deadline_format(), CANCEL_RAFFLE_KB)

    if deadline_dt <= datetime.now():
        return await _show_step(bot, message, data["prompt_msg_id"], err_deadline_past(), CANCEL_RAFFLE_KB)

    await state.update_data(deadline=deadline_dt.isoformat())
    new_id = await _show_step(bot, message, data["prompt_msg_id"], STEP_CHANNEL, CANCEL_RAFFLE_KB)
    await state.update_data(prompt_msg_id=new_id)
    await state.set_state(NewRaffle.channel)


@admin_router.message(NewRaffle.channel)
async def process_channel(message: Message, state: FSMContext, bot: Bot):
    """Проверяем формат канала и показываем сводку для подтверждения."""
    data = await state.get_data()
    await _drop_admin_answer(message)

    channel = (message.text or "").strip()
    require_subscribe = False
    if channel != "-":
        if not (channel.startswith("@") or channel.startswith("https://t.me/")):
            return await _show_step(bot, message, data["prompt_msg_id"], ERR_CHANNEL, CANCEL_RAFFLE_KB)
        require_subscribe = True
    else:
        channel = None

    await state.update_data(channel=channel, require_subscribe=require_subscribe)

    deadline_dt = datetime.fromisoformat(data["deadline"])
    summary = (
        "📋 <b>Сводка по розыгрышу:</b>\n"
        f"Победителей: {data['winner_slots']}\n"
        f"Окончание: {deadline_dt.strftime(DEADLINE_FORMAT)}\n"
        f"Канал: {html.quote(channel) if channel else 'Нет'}"
    )
    new_id = await _show_step(bot, message, data["prompt_msg_id"], summary, CONFIRM_RAFFLE_KB, parse_mode="HTML")
    await state.update_data(prompt_msg_id=new_id)
    await state.set_state(NewRaffle.confirm)


@admin_router.callback_query(F.data == "confirm_new_raffle", NewRaffle.confirm)
async def confirm_new_raffle(callback: CallbackQuery, state: FSMContext):
    """Создаёт запись Raffle в БД и разблокирует регистрацию."""
    data = await state.get_data()
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

    await callback.message.edit_text("✅ Розыгрыш успешно создан! Регистрация открыта.", reply_markup=HOME_KB)
    await state.clear()
    await callback.answer()


@admin_router.callback_query(F.data == "cancel_new_raffle")
async def cancel_new_raffle(callback: CallbackQuery, state: FSMContext):
    """Отменяет создание на любом шаге: чистим стейт и возвращаем главный экран."""
    await state.clear()
    await callback.message.edit_text(MAIN_MENU_TEXT, reply_markup=HOME_KB, parse_mode="HTML")
    await callback.answer()


# --- СЦЕНАРИЙ: ЭКСПОРТ В EXCEL ---


@admin_router.callback_query(F.data == "admin_export")
async def export_excel(callback: CallbackQuery):
    """Выгружает всех участников последнего розыгрыша в Excel (в памяти, без дисковых записей)."""
    # Перелистываем меню в статус, а не плодим новое сообщение
    await callback.message.edit_text("⏳ Собираю данные, подождите...")

    async with async_session() as session:
        # Ищем последний (текущий или только что завершенный) розыгрыш
        latest_raffle = (await session.execute(select(Raffle).order_by(Raffle.id.desc()).limit(1))).scalars().first()

        if not latest_raffle:
            await callback.message.edit_text("База данных пуста.", reply_markup=USERS_KB)
            return await callback.answer()

        result = await session.execute(select(User).where(User.raffle_id == latest_raffle.id).order_by(User.id.desc()))
        rows = result.scalars().all()

    if not rows:
        await callback.message.edit_text("В последнем розыгрыше нет участников.", reply_markup=USERS_KB)
        return await callback.answer()

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
                "ЗАБАНЕН" if user.is_banned else "Активен",
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
    # Возвращаем панель в исходный вид
    await callback.message.edit_text(SECTION_USERS_TEXT, reply_markup=USERS_KB)
    await callback.answer()


# --- СЦЕНАРИЙ: БАН / РАЗБАН ---


@admin_router.callback_query(F.data == "admin_ban")
async def start_ban(callback: CallbackQuery, state: FSMContext):
    """Запрашивает Telegram ID или @username, превращая сообщение меню в вопрос."""
    await state.clear()
    await state.set_state(BanUser.waiting_for_id)
    await state.update_data(prompt_msg_id=callback.message.message_id)
    await callback.message.edit_text(BAN_PROMPT, reply_markup=CANCEL_BAN_KB)
    await callback.answer()


@admin_router.message(BanUser.waiting_for_id)
async def process_ban_id(message: Message, state: FSMContext, bot: Bot):
    """Ищет пользователя по telegram_id или @username и переключает is_banned."""
    data = await state.get_data()
    prompt_id = data.get("prompt_msg_id")

    # Хендлер ловит любое сообщение, включая стикеры и фото (text=None)
    await _drop_admin_answer(message)
    raw = (message.text or "").strip()
    if not raw:
        return await _show_step(bot, message, prompt_id, ERR_BAN_EMPTY, CANCEL_BAN_KB)

    # Числовой ввод — ищем по telegram_id; ввод с @ — по username (регистронезависимо)
    if raw.isdigit():
        by_username, lookup_value = False, int(raw)
    elif raw.startswith("@"):
        by_username, lookup_value = True, raw.lstrip("@").lower()
    else:
        return await _show_step(bot, message, prompt_id, ERR_BAN_FORMAT, CANCEL_BAN_KB)

    async with async_session() as session:
        if by_username:
            # Username в БД хранится как прислал Telegram (может быть с заглавной буквы)
            stmt = select(User).where(func.lower(User.username) == lookup_value).limit(1)
        else:
            stmt = select(User).where(User.telegram_id == lookup_value).limit(1)

        user = (await session.execute(stmt)).scalars().first()

        if not user:
            await state.clear()
            not_found = (
                f"Пользователь с username «{raw}» не найден в базе."
                if by_username
                else "Пользователь с таким Telegram ID не найден в базе."
            )
            return await _show_step(bot, message, prompt_id, not_found, HOME_KB)

        # Меняем статус на противоположный для всех записей этого пользователя
        new_status = not user.is_banned
        await session.execute(update(User).where(User.telegram_id == user.telegram_id).values(is_banned=new_status))
        await session.commit()
        fio = user.fio

    action = "забанен 🚫" if new_status else "разбанен ✅"
    await state.clear()
    await _show_step(
        bot,
        message,
        prompt_id,
        f"Пользователь <b>{html.quote(fio)}</b> был {action}.",
        HOME_KB,
        parse_mode="HTML",
    )


@admin_router.callback_query(F.data == "cancel_ban")
async def cancel_ban(callback: CallbackQuery, state: FSMContext):
    """Отменяет блокировку: чистим стейт и возвращаем главный экран."""
    await state.clear()
    await callback.message.edit_text(MAIN_MENU_TEXT, reply_markup=HOME_KB, parse_mode="HTML")
    await callback.answer()


# --- СЦЕНАРИЙ: ДОСРОЧНОЕ ЗАВЕРШЕНИЕ ---


@admin_router.callback_query(F.data == "admin_finish_early")
async def ask_finish_early(callback: CallbackQuery):
    """Подтверждение досрочного завершения (двойной клик)."""
    await callback.message.edit_text(
        "Вы уверены? Это сразу закроет регистрацию и выберет победителей.",
        reply_markup=FINISH_CONFIRM_KB,
    )
    await callback.answer()


@admin_router.callback_query(F.data == "cancel_finish_early")
async def cancel_finish_early(callback: CallbackQuery):
    """Отмена досрочного завершения: возврат в раздел розыгрышей."""
    await callback.message.edit_text(SECTION_RAFFLES_TEXT, reply_markup=RAFFLES_KB)
    await callback.answer()


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
            await callback.message.edit_text("❌ Нет активных розыгрышей для завершения.", reply_markup=RAFFLES_KB)
            return await callback.answer()

        success = await finish_raffle(active_raffle.id, session, bot)

    if success:
        result_text = "✅ Розыгрыш успешно завершен. Результаты отправлены."
    else:
        result_text = "❌ Ошибка при завершении розыгрыша."

    await callback.message.edit_text(result_text, reply_markup=RAFFLES_KB)
    await callback.answer()
