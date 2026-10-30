"""Тесты админ-панели: навигация, чистый чат, выгрузка в Excel и бан/разбан (bot/handlers/admin.py)."""

import asyncio
import io
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import openpyxl
from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message, ReplyKeyboardMarkup
from aiogram.types import User as TgUser
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from bot.config import ADMIN_IDS, DATABASE_URL
from bot.database.models import Raffle, User
from bot.filters.is_admin import IsAdmin
from bot.handlers.admin import (
    ADMIN_HELP_TEXT,
    BTN_RAFFLES,
    BTN_USERS,
    DEADLINE_FORMAT,
    MAIN_MENU_TEXT,
    admin_help,
    admin_router,
    admin_start,
    cancel_ban,
    cancel_new_raffle,
    deadline_example,
    err_deadline_format,
    export_excel,
    open_raffles_section,
    open_users_section,
    process_ban_id,
    process_deadline,
    process_winner_slots,
    show_raffles_section,
    show_users_section,
    start_ban,
    start_new_raffle,
    step_deadline,
)
from bot.handlers.user import USER_HELP_TEXT, user_help, user_router
from bot.states.admin import BanUser, NewRaffle

BOT_ID = 8982338897
if not ADMIN_IDS:
    raise SystemExit("ADMIN_IDS пуст в .env — тесты админ-инструментов невозможны")
ADMIN_ID = ADMIN_IDS[0]
NOT_ADMIN_ID = next(i for i in range(111111, 999999) if i not in ADMIN_IDS)
SYNC_ENGINE = create_engine(DATABASE_URL.replace("+aiomysql", "+pymysql"))


def fresh_state(user_id: int = ADMIN_ID) -> FSMContext:
    key = StorageKey(bot_id=BOT_ID, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


def make_msg(text, user_id: int = ADMIN_ID) -> Message:
    """Фейковое сообщение админа с id и чатом (нужны для clean chat)."""
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.from_user = TgUser(id=user_id, is_bot=False, first_name="Admin")
    msg.chat = MagicMock()
    msg.chat.id = user_id
    msg.message_id = 1000
    msg.answer = AsyncMock()
    msg.delete = AsyncMock()
    return msg


def make_cb(data: str, user_id: int = ADMIN_ID) -> CallbackQuery:
    """Фейковый коллбек: сообщение меню, на котором происходит перелистывание."""
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = TgUser(id=user_id, is_bot=False, first_name="Admin")
    cb.message = MagicMock()
    cb.message.message_id = 2000
    cb.message.chat = MagicMock()
    cb.message.chat.id = user_id
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.message.answer_document = AsyncMock()
    cb.message.delete = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def make_bot() -> MagicMock:
    """Фейковый Bot: редактирование и удаление сообщений."""
    bot = MagicMock(spec=Bot)
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    bot.send_message = AsyncMock()
    return bot


def edited_text(bot: MagicMock) -> str:
    """Последний текст, который бот поставил в сообщение-вопрос."""
    return str(bot.edit_message_text.call_args)


def cleanup_db():
    with Session(SYNC_ENGINE) as s:
        s.execute(delete(User))
        s.execute(delete(Raffle))
        s.commit()


def add_raffle(**kw):
    defaults = dict(
        winner_slots=3,
        require_subscribe=False,
        channel_username=None,
        require_comment=False,
        comment_min_length=0,
        is_finished=False,
        deadline_at=datetime(2030, 4, 19, 18, 0),
    )
    defaults.update(kw)
    with Session(SYNC_ENGINE) as s:
        r = Raffle(**defaults)
        s.add(r)
        s.commit()
        return r.id


def add_user(raffle_id, tg_id=111, fio="Иванов И.И.", university="МГУ", username=None, **kw):
    defaults = dict(is_subscribed=True, is_banned=False)
    defaults.update(kw)
    with Session(SYNC_ENGINE) as s:
        u = User(telegram_id=tg_id, raffle_id=raffle_id, fio=fio, university=university, username=username, **defaults)
        s.add(u)
        s.commit()
        return u.id


# === НАВИГАЦИЯ ===


async def test_admin_start_gives_reply_keyboard():
    msg = make_msg("/start")
    await admin_start(msg, fresh_state())
    assert isinstance(msg.answer.call_args.kwargs["reply_markup"], ReplyKeyboardMarkup)
    assert msg.answer.call_args.kwargs["reply_markup"].keyboard[0][0].text == BTN_RAFFLES


async def test_admin_start_clears_state():
    state = fresh_state()
    await state.set_state(BanUser.waiting_for_id)
    msg = make_msg("/start")
    await admin_start(msg, state)
    assert await state.get_state() is None


async def test_reply_button_opens_raffles_section():
    msg = make_msg(BTN_RAFFLES)
    await open_raffles_section(msg, fresh_state())
    assert BTN_RAFFLES in str(msg.answer.call_args)


async def test_reply_button_opens_users_section():
    msg = make_msg(BTN_USERS)
    await open_users_section(msg, fresh_state())
    assert BTN_USERS in str(msg.answer.call_args)


async def test_inline_menu_returns_to_section():
    cb = make_cb("menu_raffles")
    await show_raffles_section(cb, fresh_state())
    cb.message.edit_text.assert_called_once()
    assert BTN_RAFFLES in str(cb.message.edit_text.call_args)


async def test_inline_menu_returns_to_users_section():
    cb = make_cb("menu_users")
    await show_users_section(cb, fresh_state())
    cb.message.edit_text.assert_called_once()
    assert BTN_USERS in str(cb.message.edit_text.call_args)


# === СПРАВКА /help ===


async def test_admin_help_shows_navigation():
    msg = make_msg("/help")
    await admin_help(msg)

    text = msg.answer.call_args.args[0]
    assert text == ADMIN_HELP_TEXT
    assert msg.answer.call_args.kwargs["parse_mode"] == "HTML"
    assert BTN_RAFFLES in text and BTN_USERS in text, "в справке нет обоих разделов панели"
    assert "/start" in text and "/help" in text, "в справке нет списка команд"


async def test_admin_help_keeps_running_scenario():
    """Справка не должна сбрасывать начатый сценарий."""
    state = await ban_state()
    await admin_help(make_msg("/help"))

    assert await state.get_state() == BanUser.waiting_for_id.state


async def test_user_help_explains_registration():
    msg = make_msg("/help")
    await user_help(msg)

    text = msg.answer.call_args.args[0]
    assert text == USER_HELP_TEXT
    assert msg.answer.call_args.kwargs["parse_mode"] == "HTML"
    assert "ФИО" in text and "/start" in text


async def test_help_is_matched_before_wizard_steps():
    """/help регистрируют раньше шагов ввода, иначе команду съест анкета."""
    admin_names = [h.callback.__name__ for h in admin_router.message.handlers]
    assert admin_names.index("admin_help") < admin_names.index("process_ban_id")

    user_names = [h.callback.__name__ for h in user_router.message.handlers]
    assert user_names.index("user_help") < user_names.index("process_fio")


# === ЭКСПОРТ ===


async def test_export_empty_db():
    cleanup_db()
    cb = make_cb("admin_export")
    await export_excel(cb)
    assert "пуста" in str(cb.message.edit_text.call_args)
    cb.message.answer_document.assert_not_called()


async def test_export_only_latest_raffle():
    cleanup_db()
    r1 = add_raffle()
    add_user(r1, tg_id=111, fio="Старый С.С.", university="ВУЗ-1")
    r2 = add_raffle()
    add_user(r2, tg_id=222, fio="Новый Н.Н.", university="ВУЗ-2", username="noviy")
    add_user(r2, tg_id=333, fio="Новый2 Н.Н.Н.", university="ВУЗ-2", is_banned=True)

    cb = make_cb("admin_export")
    await export_excel(cb)

    cb.message.answer_document.assert_called_once()
    doc = cb.message.answer_document.call_args.kwargs["document"]
    wb = openpyxl.load_workbook(io.BytesIO(doc.data))
    ws = wb.active
    rows = list(ws.values)
    assert len(rows) == 3, f"ожидали 2 строки (только raffle2) + заголовок, получили {len(rows)}"
    tg_ids = {r[1] for r in rows[1:]}
    assert tg_ids == {222, 333}, f"в выгрузке не должно быть старого raffle1, есть: {tg_ids}"
    banned = {r[1]: r[7] for r in rows[1:]}
    assert banned[333] == "ЗАБАНЕН"
    assert banned[222] == "Активен"

    # Колонка Username: с @ если ник задан, пусто если его нет (openpyxl читает пустую ячейку как None)
    usernames = {r[1]: r[2] for r in rows[1:]}
    assert usernames[222] == "@noviy", f"ожидали @noviy, получили {usernames[222]!r}"
    assert not usernames[333], f"ожидали пустую ячейку, получили {usernames[333]!r}"

    # Проверяем, что автофильтр установлен на весь диапазон (3 строки, 9 колонок → A1:I3)
    assert ws.auto_filter.ref == "A1:I3", f"ожидали A1:I3, получили {ws.auto_filter.ref}"


async def test_export_no_users_in_latest():
    cleanup_db()
    add_raffle()
    cb = make_cb("admin_export")
    await export_excel(cb)
    assert "нет участников" in str(cb.message.edit_text.call_args).lower()
    cb.message.answer_document.assert_not_called()


# === БАН ===


async def ban_state() -> FSMContext:
    """Стейт сцены бана, как его ставит кнопка «🚫 Блокировка»: со ссылкой на сообщение-вопрос."""
    state = fresh_state()
    await state.set_state(BanUser.waiting_for_id)
    await state.update_data(prompt_msg_id=2000)
    return state


async def test_ban_toggle_on():
    cleanup_db()
    r = add_raffle()
    add_user(r, tg_id=333, fio="Козлов К.К.")

    state = await ban_state()
    bot = make_bot()
    msg = make_msg("333")
    await process_ban_id(msg, state, bot)

    with Session(SYNC_ENGINE) as s:
        u = s.execute(select(User).where(User.telegram_id == 333)).scalars().first()
    assert u.is_banned is True
    assert "забанен" in edited_text(bot)
    assert await state.get_state() is None


async def test_ban_answer_is_deleted():
    """Clean chat: ответ админа удаляется, а бот лишь перелистывает свой вопрос."""
    cleanup_db()
    r = add_raffle()
    add_user(r, tg_id=334, fio="Козлов К.К.")

    state = await ban_state()
    bot = make_bot()
    msg = make_msg("334")
    await process_ban_id(msg, state, bot)

    msg.delete.assert_awaited_once()
    bot.edit_message_text.assert_awaited_once()
    msg.answer.assert_not_called()


async def test_ban_toggle_off():
    cleanup_db()
    r = add_raffle()
    add_user(r, tg_id=444, fio="Морозова М.М.", is_banned=True)

    state = await ban_state()
    bot = make_bot()
    await process_ban_id(make_msg("444"), state, bot)

    with Session(SYNC_ENGINE) as s:
        u = s.execute(select(User).where(User.telegram_id == 444)).scalars().first()
    assert u.is_banned is False
    assert "разбанен" in edited_text(bot)


async def test_ban_affects_all_user_tickets():
    """Бан по telegram_id должен закрыть заявки юзера во всех розыгрышах."""
    cleanup_db()
    r1 = add_raffle()
    r2 = add_raffle()
    add_user(r1, tg_id=555, fio="Орлова О.О.")
    add_user(r2, tg_id=555, fio="Орлова О.О.")

    state = await ban_state()
    await process_ban_id(make_msg("555"), state, make_bot())

    with Session(SYNC_ENGINE) as s:
        tickets = s.execute(select(User).where(User.telegram_id == 555)).scalars().all()
    assert len(tickets) == 2
    assert all(t.is_banned for t in tickets), "забанена не каждая заявка"


async def test_ban_user_not_found():
    cleanup_db()
    state = await ban_state()
    bot = make_bot()
    await process_ban_id(make_msg("999999"), state, bot)

    assert "не найден" in edited_text(bot)
    assert await state.get_state() is None


async def test_ban_by_username():
    """Бан по username в формате @username."""
    cleanup_db()
    r = add_raffle()
    add_user(r, tg_id=601, fio="Смирнов С.С.", username="smirnov")

    state = await ban_state()
    bot = make_bot()
    await process_ban_id(make_msg("@smirnov"), state, bot)

    with Session(SYNC_ENGINE) as s:
        u = s.execute(select(User).where(User.telegram_id == 601)).scalars().first()
    assert u.is_banned is True
    assert "забанен" in edited_text(bot)
    assert await state.get_state() is None


async def test_ban_by_username_uppercase():
    """Бан по username с заглавными буквами (в БД ник сохранил Telegram как есть)."""
    cleanup_db()
    r = add_raffle()
    add_user(r, tg_id=602, fio="Павлова П.П.", username="Pavlova")

    state = await ban_state()
    bot = make_bot()
    await process_ban_id(make_msg("@PAVLOVA"), state, bot)

    with Session(SYNC_ENGINE) as s:
        u = s.execute(select(User).where(User.telegram_id == 602)).scalars().first()
    assert u.is_banned is True
    assert "забанен" in edited_text(bot)


async def test_unban_by_username():
    cleanup_db()
    r = add_raffle()
    add_user(r, tg_id=603, fio="Фёдоров Ф.Ф.", username="fedorov", is_banned=True)

    state = await ban_state()
    bot = make_bot()
    await process_ban_id(make_msg("@fedorov"), state, bot)

    with Session(SYNC_ENGINE) as s:
        u = s.execute(select(User).where(User.telegram_id == 603)).scalars().first()
    assert u.is_banned is False
    assert "разбанен" in edited_text(bot)


async def test_ban_username_not_found():
    cleanup_db()
    state = await ban_state()
    bot = make_bot()
    await process_ban_id(make_msg("@net_takogo"), state, bot)

    assert "не найден" in edited_text(bot)


async def test_ban_username_affects_all_user_tickets():
    """Бан по username должен закрыть заявки юзера во всех розыгрышах."""
    cleanup_db()
    r1 = add_raffle()
    r2 = add_raffle()
    add_user(r1, tg_id=604, fio="Ильин И.И.", username="ilyin")
    add_user(r2, tg_id=604, fio="Ильин И.И.", username="ilyin")

    state = await ban_state()
    await process_ban_id(make_msg("@ilyin"), state, make_bot())

    with Session(SYNC_ENGINE) as s:
        tickets = s.execute(select(User).where(User.telegram_id == 604)).scalars().all()
    assert len(tickets) == 2
    assert all(t.is_banned for t in tickets), "забанена не каждая заявка"


async def test_ban_no_at_sign_rejected():
    """Ввод username без @ — ошибка, состояние не сбрасывается."""
    cleanup_db()
    r = add_raffle()
    add_user(r, tg_id=605, fio="Тестов Т.Т.", username="testov")

    state = await ban_state()
    bot = make_bot()
    await process_ban_id(make_msg("testov"), state, bot)

    assert "формат" in edited_text(bot)
    assert await state.get_state() == BanUser.waiting_for_id.state


async def test_ban_sticker_does_not_crash():
    """Стикер/фото в состоянии бана: text=None не должно ронять хендлер."""
    cleanup_db()
    state = await ban_state()
    bot = make_bot()
    msg = make_msg(None)
    await process_ban_id(msg, state, bot)

    assert edited_text(bot), "бот проигнорировал пустое сообщение"
    assert await state.get_state() == BanUser.waiting_for_id.state


async def test_ban_start_edits_menu_and_saves_prompt_id():
    state = fresh_state()
    cb = make_cb("admin_ban")
    await start_ban(cb, state)

    assert await state.get_state() == BanUser.waiting_for_id.state
    data = await state.get_data()
    assert data["prompt_msg_id"] == cb.message.message_id
    cb.message.edit_text.assert_awaited_once()


async def test_ban_cancel_clears_state_and_shows_main_menu():
    state = await ban_state()
    cb = make_cb("cancel_ban")
    await cancel_ban(cb, state)

    assert await state.get_state() is None
    # MAIN_MENU_TEXT многострочный: в repr он с экранированными \n, сверяем первую строку
    assert MAIN_MENU_TEXT.splitlines()[0] in str(cb.message.edit_text.call_args)


# === СОЗДАНИЕ РОЗЫГРЫША (clean chat) ===


async def test_new_raffle_start_saves_prompt_id():
    state = fresh_state()
    cb = make_cb("admin_new_raffle")
    await start_new_raffle(cb, state)

    assert await state.get_state() == NewRaffle.winner_slots.state
    data = await state.get_data()
    assert data["prompt_msg_id"] == cb.message.message_id
    cb.message.edit_text.assert_awaited_once()


async def test_new_raffle_deadline_example_is_now_plus_one_hour():
    """Пример дедлайна динамический: текущее время + 1 час, а не захардкоженная дата."""
    expected = (datetime.now() + timedelta(hours=1)).strftime(DEADLINE_FORMAT)
    assert deadline_example() == expected, f"ожидали {expected}, получили {deadline_example()}"


async def test_new_raffle_deadline_example_parses_and_is_future():
    """Пример обязан парситься тем же форматом и быть в будущем — иначе шаг непроходим."""
    text = deadline_example()
    parsed = datetime.strptime(text, DEADLINE_FORMAT)
    assert parsed > datetime.now(), f"пример {text} не в будущем"
    # запас ровно час: с учётом отбрасывания секунд — от 59 до 61 минуты
    delta = (parsed - datetime.now()).total_seconds() / 60
    assert 58 <= delta <= 61, f"ожидан сдвиг ~60 минут, факт {delta:.1f}"


async def test_new_raffle_step_shows_live_example():
    """Текст шага и текст ошибки содержат свежий пример, а не старый 19.04.2024."""
    step = step_deadline()
    assert "19.04.2024" not in step, "в тексте шага осталась захардкоженная дата"
    assert deadline_example() in step, f"пример не подставлен: {step}"

    err = err_deadline_format()
    assert "19.04.2024" not in err
    assert deadline_example() in err


async def test_new_raffle_deadline_example_is_accepted_as_is():
    """Самый важный сценарий: админ отправляет предложенный пример — шаг проходит."""
    cleanup_db()
    state = fresh_state()
    await start_new_raffle(make_cb("admin_new_raffle"), state)
    await process_winner_slots(make_msg("3"), state, make_bot())
    assert await state.get_state() == NewRaffle.deadline.state

    bot = make_bot()
    await process_deadline(make_msg(deadline_example()), state, bot)

    assert await state.get_state() == NewRaffle.channel.state, "пример дедлайна не принят"
    data = await state.get_data()
    assert "deadline" in data


async def test_new_raffle_steps_edit_same_message():
    """Каждый шаг перелистывает одно и то же сообщение, ответы админа удаляются."""
    cleanup_db()
    state = fresh_state()
    cb = make_cb("admin_new_raffle")
    await start_new_raffle(cb, state)
    bot = make_bot()

    msg1 = make_msg("5")
    await process_winner_slots(msg1, state, bot)
    msg1.delete.assert_awaited_once()
    assert await state.get_state() == NewRaffle.deadline.state

    msg2 = make_msg("19.04.2099 18:00")
    await process_deadline(msg2, state, bot)
    msg2.delete.assert_awaited_once()
    assert await state.get_state() == NewRaffle.channel.state

    # все перелистывания идут в одно сообщение, новое не плодится
    assert bot.edit_message_text.await_count == 2
    ids = {c.kwargs["message_id"] for c in bot.edit_message_text.call_args_list}
    assert ids == {cb.message.message_id}


async def test_new_raffle_rejects_bad_slots_without_losing_state():
    cleanup_db()
    state = fresh_state()
    await start_new_raffle(make_cb("admin_new_raffle"), state)
    bot = make_bot()

    msg = make_msg("abc")
    await process_winner_slots(msg, state, bot)

    assert "больше нуля" in edited_text(bot)
    assert await state.get_state() == NewRaffle.winner_slots.state


async def test_new_raffle_cancel_returns_to_main_menu():
    cleanup_db()
    state = fresh_state()
    await start_new_raffle(make_cb("admin_new_raffle"), state)

    cb = make_cb("cancel_new_raffle")
    await cancel_new_raffle(cb, state)

    assert await state.get_state() is None
    # MAIN_MENU_TEXT многострочный: в repr он с экранированными \n, сверяем первую строку
    assert MAIN_MENU_TEXT.splitlines()[0] in str(cb.message.edit_text.call_args)


async def test_reply_menu_interrupts_raffle_wizard():
    """Кнопка нижнего меню сбрасывает недособранный розыгрыш."""
    cleanup_db()
    state = fresh_state()
    await start_new_raffle(make_cb("admin_new_raffle"), state)

    await open_users_section(make_msg(BTN_USERS), state)
    assert await state.get_state() is None


# === РАЗНОЕ ===


async def test_isadmin_blocks_nonadmin_callbacks():
    """Кнопки экспорта/бана не должны дёргаться обычными юзерами."""
    f = IsAdmin()
    assert await f(make_cb("admin_export", user_id=NOT_ADMIN_ID)) is False
    assert await f(make_cb("admin_ban", user_id=NOT_ADMIN_ID)) is False
    assert await f(make_msg("123", user_id=NOT_ADMIN_ID)) is False
    assert await f(make_cb("admin_export")) is True


async def run_all():
    tests = [
        ("Старт: нижняя Reply-клавиатура", test_admin_start_gives_reply_keyboard),
        ("Старт: сбрасывает стейт", test_admin_start_clears_state),
        ("Reply: раздел розыгрышей", test_reply_button_opens_raffles_section),
        ("Reply: раздел модерации", test_reply_button_opens_users_section),
        ("Inline: возврат в розыгрыши", test_inline_menu_returns_to_section),
        ("Inline: возврат в модерацию", test_inline_menu_returns_to_users_section),
        ("Справка: /help по панели", test_admin_help_shows_navigation),
        ("Справка: /help не сбрасывает сценарий", test_admin_help_keeps_running_scenario),
        ("Справка: /help участника", test_user_help_explains_registration),
        ("Справка: /help раньше шагов анкеты", test_help_is_matched_before_wizard_steps),
        ("Экспорт: пустая БД", test_export_empty_db),
        ("Экспорт: только последний розыгрыш", test_export_only_latest_raffle),
        ("Экспорт: нет участников в последнем", test_export_no_users_in_latest),
        ("Бан: включение", test_ban_toggle_on),
        ("Бан: ответ админа удалён", test_ban_answer_is_deleted),
        ("Бан: выключение", test_ban_toggle_off),
        ("Бан: все заявки юзера", test_ban_affects_all_user_tickets),
        ("Бан: юзер не найден", test_ban_user_not_found),
        ("Бан: по @username", test_ban_by_username),
        ("Бан: по @username регистронезависимо", test_ban_by_username_uppercase),
        ("Разбан по @username", test_unban_by_username),
        ("Бан: @username не найден", test_ban_username_not_found),
        ("Бан: @username во всех заявках", test_ban_username_affects_all_user_tickets),
        ("Бан: ввод без @ отклоняется", test_ban_no_at_sign_rejected),
        ("Бан: стикер не роняет", test_ban_sticker_does_not_crash),
        ("Бан: кнопка сохраняет id вопроса", test_ban_start_edits_menu_and_saves_prompt_id),
        ("Бан: отмена в главное меню", test_ban_cancel_clears_state_and_shows_main_menu),
        ("Розыгрыш: кнопка сохраняет id вопроса", test_new_raffle_start_saves_prompt_id),
        ("Розыгрыш: пример дедлайна = сейчас + час", test_new_raffle_deadline_example_is_now_plus_one_hour),
        ("Розыгрыш: пример парсится и в будущем", test_new_raffle_deadline_example_parses_and_is_future),
        ("Розыгрыш: в тексте живой пример", test_new_raffle_step_shows_live_example),
        ("Розыгрыш: пример принимается как есть", test_new_raffle_deadline_example_is_accepted_as_is),
        ("Розыгрыш: шаги в одном сообщении", test_new_raffle_steps_edit_same_message),
        ("Розыгрыш: ошибка не сбрасывает шаг", test_new_raffle_rejects_bad_slots_without_losing_state),
        ("Розыгрыш: отмена в главное меню", test_new_raffle_cancel_returns_to_main_menu),
        ("Reply-меню прерывает сценарий", test_reply_menu_interrupts_raffle_wizard),
        ("IsAdmin режет чужие коллбеки", test_isadmin_blocks_nonadmin_callbacks),
    ]

    passed = failed = 0
    for name, fn in tests:
        try:
            await fn()
            print(f"  OK   {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
            failed += 1

    cleanup_db()
    from bot.database.engine import engine

    await engine.dispose()
    print(f"\nИтого: {passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    import sys

    sys.exit(1 if asyncio.run(run_all()) else 0)
