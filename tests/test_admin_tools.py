"""Тесты Этапа 4: выгрузка в Excel и бан/разбан (bot/handlers/admin.py)."""

import asyncio
import io
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import openpyxl
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram.types import User as TgUser
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from bot.config import ADMIN_IDS, DATABASE_URL
from bot.database.models import Raffle, User
from bot.filters.is_admin import IsAdmin
from bot.handlers.admin import export_excel, process_ban_id, start_ban
from bot.states.admin import BanUser

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
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.from_user = TgUser(id=user_id, is_bot=False, first_name="Admin")
    msg.answer = AsyncMock()
    return msg


def make_cb(data: str, user_id: int = ADMIN_ID) -> CallbackQuery:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = TgUser(id=user_id, is_bot=False, first_name="Admin")
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.message.answer_document = AsyncMock()
    cb.answer = AsyncMock()
    return cb


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


def add_user(raffle_id, tg_id=111, fio="Иванов И.И.", university="МГУ", **kw):
    defaults = dict(is_subscribed=True, is_banned=False)
    defaults.update(kw)
    with Session(SYNC_ENGINE) as s:
        u = User(telegram_id=tg_id, raffle_id=raffle_id, fio=fio, university=university, **defaults)
        s.add(u)
        s.commit()
        return u.id


# === ЭКСПОРТ ===


async def test_export_empty_db():
    cleanup_db()
    cb = make_cb("admin_export")
    await export_excel(cb)
    assert "пуста" in str(cb.message.answer.call_args_list[-1])
    cb.message.answer_document.assert_not_called()


async def test_export_only_latest_raffle():
    cleanup_db()
    r1 = add_raffle()
    add_user(r1, tg_id=111, fio="Старый С.С.", university="ВУЗ-1")
    r2 = add_raffle()
    add_user(r2, tg_id=222, fio="Новый Н.Н.", university="ВУЗ-2")
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
    banned = {r[1]: r[6] for r in rows[1:]}
    assert banned[333] == "ЗАБАНЕН"
    assert banned[222] == "Активен"


async def test_export_no_users_in_latest():
    cleanup_db()
    add_raffle()
    cb = make_cb("admin_export")
    await export_excel(cb)
    assert "нет участников" in str(cb.message.answer.call_args_list[-1]).lower()
    cb.message.answer_document.assert_not_called()


# === БАН ===


async def test_ban_toggle_on():
    cleanup_db()
    r = add_raffle()
    add_user(r, tg_id=333, fio="Козлов К.К.")

    state = fresh_state()
    await state.set_state(BanUser.waiting_for_id)
    msg = make_msg("333")
    await process_ban_id(msg, state)

    with Session(SYNC_ENGINE) as s:
        u = s.execute(select(User).where(User.telegram_id == 333)).scalars().first()
    assert u.is_banned is True
    assert "забанен" in str(msg.answer.call_args)
    assert await state.get_state() is None


async def test_ban_toggle_off():
    cleanup_db()
    r = add_raffle()
    add_user(r, tg_id=444, fio="Морозова М.М.", is_banned=True)

    state = fresh_state()
    await state.set_state(BanUser.waiting_for_id)
    msg = make_msg("444")
    await process_ban_id(msg, state)

    with Session(SYNC_ENGINE) as s:
        u = s.execute(select(User).where(User.telegram_id == 444)).scalars().first()
    assert u.is_banned is False
    assert "разбанен" in str(msg.answer.call_args)


async def test_ban_affects_all_user_tickets():
    """Бан по telegram_id должен закрыть заявки юзера во всех розыгрышах."""
    cleanup_db()
    r1 = add_raffle()
    r2 = add_raffle()
    add_user(r1, tg_id=555, fio="Орлова О.О.")
    add_user(r2, tg_id=555, fio="Орлова О.О.")

    state = fresh_state()
    await state.set_state(BanUser.waiting_for_id)
    await process_ban_id(make_msg("555"), state)

    with Session(SYNC_ENGINE) as s:
        tickets = s.execute(select(User).where(User.telegram_id == 555)).scalars().all()
    assert len(tickets) == 2
    assert all(t.is_banned for t in tickets), "забанена не каждая заявка"


async def test_ban_user_not_found():
    cleanup_db()
    state = fresh_state()
    await state.set_state(BanUser.waiting_for_id)
    msg = make_msg("999999")
    await process_ban_id(msg, state)
    assert "не найден" in str(msg.answer.call_args)
    assert await state.get_state() is None


async def test_ban_non_numeric_text():
    state = fresh_state()
    await state.set_state(BanUser.waiting_for_id)
    msg = make_msg("абвгд")
    await process_ban_id(msg, state)
    assert "корректный числовой" in str(msg.answer.call_args)
    assert await state.get_state() == BanUser.waiting_for_id.state


async def test_ban_sticker_does_not_crash():
    """Стикер/фото в состоянии бана: text=None не должно ронять хендлер."""
    state = fresh_state()
    await state.set_state(BanUser.waiting_for_id)
    msg = make_msg(None)
    try:
        await process_ban_id(msg, state)
        assert "корректный числовой" in str(msg.answer.call_args)
        return True, "OK — бот просит числовой ID"
    except AttributeError as e:
        return False, f"AttributeError: {e}"


async def test_ban_start_sets_state():
    state = fresh_state()
    cb = make_cb("admin_ban")
    await start_ban(cb, state)
    assert await state.get_state() == BanUser.waiting_for_id.state


async def test_isadmin_blocks_nonadmin_callbacks():
    """Кнопки экспорта/бана не должны дёргаться обычными юзерами."""
    f = IsAdmin()
    assert await f(make_cb("admin_export", user_id=NOT_ADMIN_ID)) is False
    assert await f(make_cb("admin_ban", user_id=NOT_ADMIN_ID)) is False
    assert await f(make_msg("123", user_id=NOT_ADMIN_ID)) is False
    assert await f(make_cb("admin_export")) is True


async def run_all():
    tests = [
        ("Экспорт: пустая БД", test_export_empty_db),
        ("Экспорт: только последний розыгрыш", test_export_only_latest_raffle),
        ("Экспорт: нет участников в последнем", test_export_no_users_in_latest),
        ("Бан: включение", test_ban_toggle_on),
        ("Бан: выключение", test_ban_toggle_off),
        ("Бан: все заявки юзера", test_ban_affects_all_user_tickets),
        ("Бан: юзер не найден", test_ban_user_not_found),
        ("Бан: нечисловой ввод", test_ban_non_numeric_text),
        ("Бан: стикер не роняет", test_ban_sticker_does_not_crash),
        ("Бан: кнопка ставит стейт", test_ban_start_sets_state),
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
