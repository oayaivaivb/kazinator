"""Интеграционный тест FSM-сценария создания розыгрыша."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.context import FSMContext
from aiogram.types import User, CallbackQuery, Message
from bot.filters.is_admin import IsAdmin
from bot.states.admin import NewRaffle
from bot.handlers.admin import process_winner_slots, process_deadline, process_channel, confirm_new_raffle, cancel_new_raffle
from bot.database.models import Raffle
from bot.config import DATABASE_URL, ADMIN_IDS
from sqlalchemy import select
from bot.database.engine import engine


# --- Мок-объекты ---
# ID берём из конфига, а не хардкодим: ADMIN_IDS меняется через .env
if not ADMIN_IDS:
    raise SystemExit("ADMIN_IDS пуст в .env — тесты админ-фильтра невозможны")

BOT_ID = 8982338897
USER_ID = ADMIN_IDS[0]
CHAT_ID = USER_ID
NOT_ADMIN_ID = next(i for i in range(111111, 999999) if i not in ADMIN_IDS)


def make_user(user_id: int = USER_ID, is_bot: bool = False) -> User:
    return User(id=user_id, is_bot=is_bot, first_name="Test")


def make_storage_key():
    return StorageKey(bot_id=BOT_ID, chat_id=CHAT_ID, user_id=USER_ID)


def make_fsm_context(storage: MemoryStorage = None) -> FSMContext:
    if storage is None:
        storage = MemoryStorage()
    return FSMContext(storage=storage, key=make_storage_key())


def make_message(user_id: int = USER_ID, text: str = "") -> Message:
    mock_msg = MagicMock(spec=Message)
    mock_msg.text = text
    mock_msg.from_user = make_user(user_id)
    mock_msg.answer = AsyncMock()
    return mock_msg


def make_callback(user_id: int = USER_ID, data: str = "") -> CallbackQuery:
    user = make_user(user_id)
    mock_msg = MagicMock()
    mock_msg.edit_text = AsyncMock()
    cq = MagicMock(spec=CallbackQuery)
    cq.from_user = user
    cq.data = data
    cq.message = mock_msg
    cq.answer = AsyncMock()
    return cq


# --- Тест 1: IsAdmin фильтр ---
async def test_is_admin_filter():
    is_admin = IsAdmin()

    msg = make_message(USER_ID, "/start")
    assert await is_admin(msg) is True

    msg2 = make_message(NOT_ADMIN_ID, "/start")
    assert await is_admin(msg2) is False

    cb = make_callback(USER_ID, "admin_new_raffle")
    assert await is_admin(cb) is True

    cb2 = make_callback(NOT_ADMIN_ID, "admin_new_raffle")
    assert await is_admin(cb2) is False


# --- Тест 2: Шаг 1 — валидное количество победителей ---
async def test_process_winner_slots_valid():
    state = make_fsm_context()
    await state.set_state(NewRaffle.winner_slots)

    msg = make_message(text="3")
    await process_winner_slots(msg, state)

    data = await state.get_data()
    assert data["winner_slots"] == 3
    current = await state.get_state()
    assert current == NewRaffle.deadline


# --- Тест 3: Шаг 1 — невалидное количество ---
async def test_process_winner_slots_invalid():
    state = make_fsm_context()
    await state.set_state(NewRaffle.winner_slots)

    msg = make_message(text="-5")
    await process_winner_slots(msg, state)
    msg.answer.assert_called_once()
    assert "Ошибка" in str(msg.answer.call_args)
    assert await state.get_state() == NewRaffle.winner_slots


# --- Тест 4: Шаг 2 — валидная дата ---
async def test_process_deadline_valid():
    state = make_fsm_context()
    await state.set_state(NewRaffle.deadline)

    msg = make_message(text="19.04.2030 18:00")
    await process_deadline(msg, state)

    data = await state.get_data()
    assert "deadline" in data
    current = await state.get_state()
    assert current == NewRaffle.channel


# --- Тест 5: Шаг 2 — невалидная дата ---
async def test_process_deadline_invalid():
    state = make_fsm_context()
    await state.set_state(NewRaffle.deadline)

    msg = make_message(text="not-a-date")
    await process_deadline(msg, state)
    msg.answer.assert_called_once()
    assert "Неверный формат" in str(msg.answer.call_args)
    assert await state.get_state() == NewRaffle.deadline


# --- Тест 6: Шаг 3 — без подписки ---
async def test_process_channel_none():
    state = make_fsm_context()
    await state.set_state(NewRaffle.channel)

    await state.update_data(
        winner_slots=3,
        deadline=MagicMock(strftime=lambda fmt: "19.04.2030 18:00"),
    )

    msg = make_message(text="-")
    await process_channel(msg, state)

    data = await state.get_data()
    assert data["channel"] is None
    assert data["require_subscribe"] is False
    assert await state.get_state() == NewRaffle.confirm


# --- Тест 7: Шаг 3 — с подпиской ---
async def test_process_channel_with_subscribe():
    from datetime import datetime
    state = make_fsm_context()
    await state.set_state(NewRaffle.channel)

    await state.update_data(
        winner_slots=3,
        deadline=datetime(2030, 4, 19, 18, 0),
    )

    msg = make_message(text="@mychannel")
    await process_channel(msg, state)

    data = await state.get_data()
    assert data["channel"] == "@mychannel"
    assert data["require_subscribe"] is True


# --- Тест 8: Шаг 3 — невалидная ссылка ---
async def test_process_channel_invalid():
    state = make_fsm_context()
    await state.set_state(NewRaffle.channel)

    msg = make_message(text="invalid_link")
    await process_channel(msg, state)
    msg.answer.assert_called_once()
    assert "Неверный формат ссылки" in str(msg.answer.call_args)


# --- Тест 9: Подтверждение — запись в БД ---
async def test_confirm_new_raffle():
    from datetime import datetime
    state = make_fsm_context()
    await state.set_state(NewRaffle.confirm)
    await state.update_data(
        winner_slots=5,
        deadline=datetime(2030, 4, 19, 18, 0),
        channel=None,
        require_subscribe=False,
    )

    cb = make_callback(data="confirm_new_raffle")
    await confirm_new_raffle(cb, state)

    # Проверим, что запись появилась в БД (синхронный движок для тестов)
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    sync_url = DATABASE_URL.replace("+aiomysql", "+pymysql")
    sync_engine = create_engine(sync_url)
    with Session(sync_engine) as session:
        raffle = session.execute(
            select(Raffle).order_by(Raffle.id.desc()).limit(1)
        ).scalar_one()
        assert raffle.winner_slots == 5
        assert raffle.is_finished is False
        assert raffle.require_subscribe is False
        assert raffle.channel_username is None

    assert await state.get_state() is None


# --- Тест 10: Отмена ---
async def test_cancel_new_raffle():
    state = make_fsm_context()
    await state.set_state(NewRaffle.confirm)

    cb = make_callback(data="cancel_new_raffle")
    await cancel_new_raffle(cb, state)

    assert await state.get_state() is None
    cb.message.edit_text.assert_called_once()
    assert "отменено" in str(cb.message.edit_text.call_args)


# --- Запуск ---
if __name__ == "__main__":
    import sys

    tests = [
        ("IsAdmin фильтр", test_is_admin_filter),
        ("Шаг 1 — валидный ввод", test_process_winner_slots_valid),
        ("Шаг 1 — невалидный ввод", test_process_winner_slots_invalid),
        ("Шаг 2 — валидная дата", test_process_deadline_valid),
        ("Шаг 2 — невалидная дата", test_process_deadline_invalid),
        ("Шаг 3 — без подписки", test_process_channel_none),
        ("Шаг 3 — с подпиской", test_process_channel_with_subscribe),
        ("Шаг 3 — невалидная ссылка", test_process_channel_invalid),
        ("Подтверждение — запись в БД", test_confirm_new_raffle),
        ("Отмена", test_cancel_new_raffle),
    ]

    passed = 0
    failed = 0
    for name, test_fn in tests:
        try:
            asyncio.run(test_fn())
            print(f"  OK {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {name}: {e}")
            failed += 1

    print(f"\nИтого: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
