"""Тесты Ядра: finish_raffle, check_deadlines, досрочное завершение."""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, User as TgUser
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from bot.config import DATABASE_URL, ADMIN_IDS
from bot.database.models import Raffle, User, Winner
from bot.handlers.admin import do_finish_early, ask_finish_early, cancel_finish_early
from bot.scheduler import check_deadlines
from bot.services.raffle import finish_raffle
from bot.database.engine import async_session

BOT_ID = 8982338897
if not ADMIN_IDS:
    raise SystemExit("ADMIN_IDS пуст")
ADMIN_ID = ADMIN_IDS[0]
SYNC_ENGINE = create_engine(DATABASE_URL.replace("+aiomysql", "+pymysql"))


def fresh_state(user_id: int = ADMIN_ID) -> FSMContext:
    key = StorageKey(bot_id=BOT_ID, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


def make_cb(data: str, user_id: int = ADMIN_ID) -> CallbackQuery:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = TgUser(id=user_id, is_bot=False, first_name="Admin")
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def make_bot_mock():
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()
    return bot


def cleanup_db():
    with Session(SYNC_ENGINE) as s:
        s.execute(delete(Winner))
        s.execute(delete(User))
        s.execute(delete(Raffle))
        s.commit()


def add_raffle(**kw):
    defaults = dict(
        winner_slots=3, require_subscribe=False, channel_username=None,
        require_comment=False, comment_min_length=0, is_finished=False,
        deadline_at=datetime(2030, 4, 19, 18, 0),
    )
    defaults.update(kw)
    with Session(SYNC_ENGINE) as s:
        r = Raffle(**defaults)
        s.add(r)
        s.commit()
        return r.id


def add_user(raffle_id, tg_id, fio, university="МГУ", username=None):
    with Session(SYNC_ENGINE) as s:
        u = User(telegram_id=tg_id, raffle_id=raffle_id, fio=fio,
                 university=university, username=username,
                 is_subscribed=False, is_banned=False)
        s.add(u)
        s.commit()
        return u.id


def count_winners(raffle_id):
    with Session(SYNC_ENGINE) as s:
        return s.execute(
            select(func.count(Winner.id)).where(Winner.raffle_id == raffle_id)
        ).scalar()


# === finish_raffle ===

async def test_finish_normal():
    """Обычное завершение: N участников, N победителей."""
    cleanup_db()
    r = add_raffle(winner_slots=2)
    add_user(r, 101, "Андреев А.А.")
    add_user(r, 102, "Борисов Б.Б.")
    add_user(r, 103, "Викторов В.В.")

    bot = make_bot_mock()
    async with async_session() as session:
        ok = await finish_raffle(r, session, bot)

    assert ok is True
    assert count_winners(r) == 2

    with Session(SYNC_ENGINE) as s:
        raffle = s.get(Raffle, r)
        assert raffle.is_finished is True

    bot.send_message.assert_called_once()
    text = bot.send_message.call_args.kwargs["text"]
    assert "завершен" in text
    assert "3" in text  # участников

async def test_finish_fewer_users_than_slots():
    cleanup_db()
    r = add_raffle(winner_slots=5)
    add_user(r, 201, "Единственный Е.Е.")

    bot = make_bot_mock()
    async with async_session() as session:
        ok = await finish_raffle(r, session, bot)

    assert ok is True
    assert count_winners(r) == 1, "только 1 участник, 5 слотов -> 1 победитель"


async def test_finish_no_users():
    cleanup_db()
    r = add_raffle(winner_slots=3)

    bot = make_bot_mock()
    async with async_session() as session:
        ok = await finish_raffle(r, session, bot)

    assert ok is True
    assert count_winners(r) == 0

    text = bot.send_message.call_args.kwargs["text"]
    assert "0" in text


async def test_finish_bans_excluded():
    """Забаненные участники не могут победить."""
    cleanup_db()
    r = add_raffle(winner_slots=2)
    add_user(r, 301, "Активный А.А.")
    add_user(r, 302, "Забаненный З.З.")
    add_user(r, 303, "Активный2 Б.Б.")

    with Session(SYNC_ENGINE) as s:
        u = s.execute(select(User).where(User.telegram_id == 302)).scalars().first()
        u.is_banned = True
        s.commit()

    bot = make_bot_mock()
    async with async_session() as session:
        ok = await finish_raffle(r, session, bot)

    assert ok is True
    assert count_winners(r) == 2

    # Проверяем, что забаненный не в победителях
    with Session(SYNC_ENGINE) as s:
        winners = s.execute(
            select(Winner, User).join(User, Winner.user_id == User.id).where(Winner.raffle_id == r)
        ).all()
    winner_ids = {w[1].telegram_id for w in winners}
    assert 302 not in winner_ids


async def test_finish_idempotent():
    cleanup_db()
    r = add_raffle(winner_slots=2)
    add_user(r, 401, "Иван И.И.")

    bot = make_bot_mock()
    async with async_session() as session:
        await finish_raffle(r, session, bot)

    async with async_session() as session:
        ok = await finish_raffle(r, session, bot)
    assert ok is False
    assert count_winners(r) == 1


async def test_finish_sends_to_all_admins():
    cleanup_db()
    r = add_raffle(winner_slots=1)
    add_user(r, 501, "Петр П.П.")

    bot = make_bot_mock()
    async with async_session() as session:
        await finish_raffle(r, session, bot)

    assert bot.send_message.call_count == len(ADMIN_IDS)


# === check_deadlines ===

async def test_scheduler_expires_raffle():
    cleanup_db()
    expired = add_raffle(deadline_at=datetime(2020, 1, 1))
    active = add_raffle(deadline_at=datetime(2030, 12, 31))
    add_user(expired, 601, "Срочный С.С.")

    bot = make_bot_mock()
    await check_deadlines(bot)

    with Session(SYNC_ENGINE) as s:
        r = s.get(Raffle, expired)
        assert r.is_finished is True
        r2 = s.get(Raffle, active)
        assert r2.is_finished is False

    assert count_winners(expired) == 1


async def test_scheduler_no_expired():
    cleanup_db()
    add_raffle(deadline_at=datetime(2030, 12, 31))

    bot = make_bot_mock()
    await check_deadlines(bot)
    bot.send_message.assert_not_called()


# === do_finish_early ===

async def test_early_finish():
    cleanup_db()
    r = add_raffle(winner_slots=1)
    add_user(r, 701, "Досрочный Д.Д.")

    cb = make_cb("confirm_finish_early")
    bot = make_bot_mock()
    await do_finish_early(cb, bot)

    with Session(SYNC_ENGINE) as s:
        raffle = s.get(Raffle, r)
        assert raffle.is_finished is True

    assert count_winners(r) == 1
    cb.message.answer.assert_called()
    assert "завершен" in str(cb.message.answer.call_args)


async def test_early_finish_no_active():
    cleanup_db()
    cb = make_cb("confirm_finish_early")
    bot = make_bot_mock()
    await do_finish_early(cb, bot)
    assert "Нет активных" in str(cb.message.edit_text.call_args)


async def test_ask_finish_shows_keyboard():
    cleanup_db()
    cb = make_cb("admin_finish_early")
    await ask_finish_early(cb)
    cb.message.edit_text.assert_called_once()
    kwargs = cb.message.edit_text.call_args.kwargs
    assert "reply_markup" in kwargs


async def test_cancel_finish():
    cleanup_db()
    cb = make_cb("cancel_finish_early")
    await cancel_finish_early(cb)
    assert "отменено" in str(cb.message.edit_text.call_args).lower()


# === Runner ===

async def test_finish_sends_photos():
    """Сводка всем админам + личная карточка каждому победителю."""
    cleanup_db()
    r = add_raffle(winner_slots=2)
    add_user(r, 801, "Фотографов Ф.Ф.", username="nick_one")
    add_user(r, 802, "Снимков С.С.")

    bot = make_bot_mock()
    async with async_session() as session:
        await finish_raffle(r, session, bot)

    # админам сводка (len(ADMIN_IDS)) + 2 личные
    assert bot.send_photo.call_count == len(ADMIN_IDS) + 2, \
        f"ожидали {len(ADMIN_IDS) + 2} фото, получили {bot.send_photo.call_count}"

    # Проверяем, что это валильный PNG
    first_photo = bot.send_photo.call_args_list[0].kwargs["photo"]
    assert first_photo.data[:8] == b"\x89PNG\r\n\x1a\n", "файл не является PNG"
    assert len(first_photo.data) > 1000, "пустое изображение"

    # У каждого админа должен быть полный файл (ловушка с съеденным буфером)
    sizes = [len(c.kwargs["photo"].data) for c in bot.send_photo.call_args_list[:len(ADMIN_IDS)]]
    assert all(s == sizes[0] for s in sizes), f"размеры сводок по админам различаются: {sizes}"
    assert sizes[0] > 1000, "второму админу ушёл пустой буфер"


async def test_finish_no_photos_without_winners():
    cleanup_db()
    r = add_raffle(winner_slots=3)

    bot = make_bot_mock()
    async with async_session() as session:
        await finish_raffle(r, session, bot)
    bot.send_photo.assert_not_called()


async def test_finish_personal_photo_to_winner():
    cleanup_db()
    r = add_raffle(winner_slots=1)
    add_user(r, 810, "Победителей П.П.", username="winner_nick")

    bot = make_bot_mock()
    async with async_session() as session:
        await finish_raffle(r, session, bot)

    # последняя отправка — личная победителю
    last = bot.send_photo.call_args_list[-1]
    assert last.kwargs["chat_id"] == 810
    assert "Поздравляем" in last.kwargs["caption"]


async def test_report_escapes_html():
    """ФИКС: fio с '<' ломает parse_mode=HTML -> send_message падает -> админы молча
    не получают ничего, потому что ошибка глотается except pass."""
    cleanup_db()
    r = add_raffle(winner_slots=1)
    add_user(r, 820, "<script>Иван</script>", university="М&ГУ")

    bot = make_bot_mock()
    async with async_session() as session:
        await finish_raffle(r, session, bot)

    text = bot.send_message.call_args.kwargs["text"]
    assert "<script>" not in text, "сырой <script> уйдёт в parse_mode=HTML и упадёт"
    assert "&lt;script&gt;" in text, "должно быть экранировано"
    assert "М&amp;ГУ" in text


async def test_report_contains_username():
    cleanup_db()
    r = add_raffle(winner_slots=1)
    add_user(r, 830, "Никовый Н.Н.", username="super_nick")

    bot = make_bot_mock()
    async with async_session() as session:
        await finish_raffle(r, session, bot)

    text = bot.send_message.call_args.kwargs["text"]
    assert "@super_nick" in text, f"ник должен быть в отчёте: {text}"


async def run_all():
    tests = [
        ("finish: обычное завершение", test_finish_normal),
        ("finish: участников < слотов", test_finish_fewer_users_than_slots),
        ("finish: нет участников", test_finish_no_users),
        ("finish: бан исключает", test_finish_bans_excluded),
        ("finish: идемпотентность", test_finish_idempotent),
        ("finish: рассылка всем админам", test_finish_sends_to_all_admins),
        ("finish: отправка картинок", test_finish_sends_photos),
        ("finish: нет победителей — нет фото", test_finish_no_photos_without_winners),
        ("finish: личная карточка победителю", test_finish_personal_photo_to_winner),
        ("finish: экранирование HTML в отчёте", test_report_escapes_html),
        ("finish: ник в отчёте", test_report_contains_username),
        ("scheduler: дедлайн истёк", test_scheduler_expires_raffle),
        ("scheduler: дедлайн не истёк", test_scheduler_no_expired),
        ("early finish: завершение", test_early_finish),
        ("early finish: нет активных", test_early_finish_no_active),
        ("early finish: подтверждение с кнопками", test_ask_finish_shows_keyboard),
        ("early finish: отмена", test_cancel_finish),
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
