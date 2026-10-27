"""Интеграционные тесты клиентской воронки регистрации."""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User as TgUser
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from bot.config import DATABASE_URL, ADMIN_IDS
from bot.database.models import Raffle, User
from bot.handlers.user import (
    check_sub_callback,
    process_fio,
    process_university,
    user_start,
)
from bot.states.user import UserRegistration

BOT_ID = 8982338897
USER_ID = 555000111
SYNC_ENGINE = create_engine(DATABASE_URL.replace("+aiomysql", "+pymysql"))


def fresh_state(user_id: int = USER_ID) -> FSMContext:
    key = StorageKey(bot_id=BOT_ID, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=MemoryStorage(), key=key)


def make_msg(text, user_id: int = USER_ID, username: str | None = "test_nick") -> Message:
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.from_user = TgUser(id=user_id, is_bot=False, first_name="Test", username=username)
    msg.answer = AsyncMock()
    return msg


def make_send_bot():
    """Мок бота для рассылки (антифрод-алерты)."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


def make_cb(data: str, user_id: int = USER_ID) -> CallbackQuery:
    cb = MagicMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = TgUser(id=user_id, is_bot=False, first_name="Test")
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    return cb


def make_bot(status: str = "member", raises: bool = False):
    bot = MagicMock()
    if raises:
        bot.get_chat_member = AsyncMock(side_effect=Exception("bot not in chat"))
    else:
        member = MagicMock()
        member.status = status
        bot.get_chat_member = AsyncMock(return_value=member)
    return bot


def cleanup_db():
    with Session(SYNC_ENGINE) as s:
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


def last_user():
    with Session(SYNC_ENGINE) as s:
        return s.execute(select(User).order_by(User.id.desc()).limit(1)).scalar_one_or_none()


# === ТЕСТЫ ===

async def test_no_active_raffle():
    cleanup_db()
    state = fresh_state()
    msg = make_msg("/start")
    await user_start(msg, state)
    assert "нет активных розыгрышей" in str(msg.answer.call_args)
    assert await state.get_state() is None


async def test_already_registered():
    cleanup_db()
    raffle_id = add_raffle()
    with Session(SYNC_ENGINE) as s:
        s.add(User(telegram_id=USER_ID, raffle_id=raffle_id, fio="Иванов И.И.", university="МГУ"))
        s.commit()
    state = fresh_state()
    msg = make_msg("/start")
    await user_start(msg, state)
    assert "уже зарегистрированы" in str(msg.answer.call_args)


async def test_start_stores_raffle_id_in_state():
    """raffle_id и require_subscribe должны попадать в стейт."""
    cleanup_db()
    raffle_id = add_raffle(require_subscribe=True, channel_username="@chan")
    state = fresh_state()
    await user_start(make_msg("/start"), state)
    data = await state.get_data()
    assert data["raffle_id"] == raffle_id
    assert data["require_subscribe"] is True


async def test_start_no_subscribe():
    cleanup_db()
    add_raffle(require_subscribe=False, channel_username=None)
    state = fresh_state()
    msg = make_msg("/start")
    await user_start(msg, state)
    assert await state.get_state() == UserRegistration.fio.state
    assert "ФИО" in str(msg.answer.call_args)


async def test_start_subscribe_at():
    cleanup_db()
    add_raffle(require_subscribe=True, channel_username="@test_channel")
    state = fresh_state()
    msg = make_msg("/start")
    await user_start(msg, state)
    assert await state.get_state() == UserRegistration.check_sub.state
    markup = msg.answer.call_args.kwargs["reply_markup"]
    assert isinstance(markup, InlineKeyboardMarkup)
    assert markup.inline_keyboard[0][0].url == "https://t.me/test_channel"
    data = await state.get_data()
    assert data["channel"] == "@test_channel"


async def test_start_subscribe_tme():
    cleanup_db()
    add_raffle(require_subscribe=True, channel_username="https://t.me/live_channel")
    state = fresh_state()
    msg = make_msg("/start")
    await user_start(msg, state)
    markup = msg.answer.call_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].url == "https://t.me/live_channel"


async def test_check_sub_success():
    cleanup_db()
    state = fresh_state()
    await state.set_state(UserRegistration.check_sub)
    await state.update_data(channel="https://t.me/some_channel")
    cb = make_cb("check_subscription")
    bot = make_bot(status="member")
    await check_sub_callback(cb, bot, state)
    assert bot.get_chat_member.call_args.kwargs["chat_id"] == "@some_channel"
    assert await state.get_state() == UserRegistration.fio.state


async def test_check_sub_not_subscribed():
    cleanup_db()
    state = fresh_state()
    await state.set_state(UserRegistration.check_sub)
    await state.update_data(channel="@chan")
    cb = make_cb("check_subscription")
    await check_sub_callback(cb, make_bot(status="left"), state)
    assert cb.answer.call_args.kwargs.get("show_alert") is True
    assert await state.get_state() == UserRegistration.check_sub.state


async def test_check_sub_error():
    cleanup_db()
    state = fresh_state()
    await state.set_state(UserRegistration.check_sub)
    await state.update_data(channel="@chan")
    cb = make_cb("check_subscription")
    await check_sub_callback(cb, make_bot(raises=True), state)
    assert "Ошибка проверки подписки" in str(cb.message.answer.call_args)


async def test_process_fio():
    state = fresh_state()
    await state.set_state(UserRegistration.fio)
    await process_fio(make_msg("  Петров П.П.  "), state)
    assert (await state.get_data())["fio"] == "Петров П.П."
    assert await state.get_state() == UserRegistration.university.state


async def test_process_fio_truncates():
    state = fresh_state()
    await state.set_state(UserRegistration.fio)
    await process_fio(make_msg("А" * 400), state)
    assert len((await state.get_data())["fio"]) == 250


async def test_process_university_writes_db():
    cleanup_db()
    raffle_id = add_raffle()
    state = fresh_state()
    await state.set_state(UserRegistration.university)
    await state.update_data(fio="Сидоров С.С.", channel="@chan", raffle_id=raffle_id, require_subscribe=False)
    await process_university(make_msg("МГТУ им. Баумана"), state, make_send_bot())
    u = last_user()
    assert u is not None
    assert u.telegram_id == USER_ID
    assert u.raffle_id == raffle_id
    assert u.fio == "Сидоров С.С."
    assert u.university == "МГТУ им. Баумана"
    assert u.is_banned is False
    assert u.is_subscribed is False, "require_subscribe=False, is_subscribed должен быть False"
    assert await state.get_state() is None


async def test_process_university_is_subscribed_true():
    cleanup_db()
    raffle_id = add_raffle(require_subscribe=True, channel_username="@chan")
    state = fresh_state()
    await state.set_state(UserRegistration.university)
    await state.update_data(fio="Подписчик П.П.", channel="@chan", raffle_id=raffle_id, require_subscribe=True)
    await process_university(make_msg("МГУ"), state, make_send_bot())
    u = last_user()
    assert u.is_subscribed is True


async def test_banned_user_blocked():
    """Забаненный юзер не может зарегистрироваться ни в один розыгрыш."""
    cleanup_db()
    raffle1 = add_raffle()
    # Регистрируем юзера и банем его
    with Session(SYNC_ENGINE) as s:
        s.add(User(telegram_id=USER_ID, raffle_id=raffle1, fio="Бан Бан.Б.", university="ВУЗ", is_banned=True))
        s.commit()
    # Создаём новый розыгрыш
    add_raffle()
    msg = make_msg("/start")
    await user_start(msg, fresh_state())
    assert "заблокированы" in str(msg.answer.call_args)


async def test_full_happy_path():
    cleanup_db()
    add_raffle(require_subscribe=True, channel_username="@raffle_chan")
    state = fresh_state()
    await user_start(make_msg("/start"), state)
    assert await state.get_state() == UserRegistration.check_sub.state
    await check_sub_callback(make_cb("check_subscription"), make_bot(status="administrator"), state)
    assert await state.get_state() == UserRegistration.fio.state
    await process_fio(make_msg("Кузнецов К.К."), state)
    assert await state.get_state() == UserRegistration.university.state
    await process_university(make_msg("СПбГУ"), state, make_send_bot())
    u = last_user()
    assert u.fio == "Кузнецов К.К."
    assert u.university == "СПбГУ"


async def test_second_raffle_allows_reregistration():
    """ФИКС БАГА: юзер мог зарегистрироваться в каждом новом розыгрыше, а не один раз навсегда."""
    cleanup_db()
    raffle1 = add_raffle()
    state = fresh_state()
    await user_start(make_msg("/start"), state)
    await process_fio(make_msg("Тестов Т.Т."), state)
    await process_university(make_msg("ВУЗ-1"), state, make_send_bot())

    with Session(SYNC_ENGINE) as s:
        first = s.execute(select(User).where(User.raffle_id == raffle1)).scalars().first()
    assert first is not None, "первая заявка не записалась"

    # Первый розыгрыш завершён, создан второй
    with Session(SYNC_ENGINE) as s:
        s.execute(Raffle.__table__.update().values(is_finished=True))
        s.commit()
    raffle2 = add_raffle()

    # Тот же юзер регается во втором розыгрыше
    state2 = fresh_state()
    msg2 = make_msg("/start")
    await user_start(msg2, state2)
    assert "уже зарегистрированы" not in str(msg2.answer.call_args), \
        f"юзера ошибочно заблокировали: {msg2.answer.call_args}"
    assert await state2.get_state() == UserRegistration.fio.state

    await process_fio(make_msg("Тестов Т.Т."), state2)
    await process_university(make_msg("ВУЗ-2"), state2, make_send_bot())

    with Session(SYNC_ENGINE) as s:
        tickets = s.execute(
            select(User).where(User.telegram_id == USER_ID).order_by(User.id)
        ).scalars().all()
    assert len(tickets) == 2, f"ожидали 2 заявки, получили {len(tickets)}"
    assert [t.raffle_id for t in tickets] == [raffle1, raffle2]
    assert [t.university for t in tickets] == ["ВУЗ-1", "ВУЗ-2"]


async def test_duplicate_ticket_same_raffle_rejected():
    """Составной unique (telegram_id, raffle_id) не даст записать дубль в один розыгрыш."""
    from sqlalchemy.exc import IntegrityError
    cleanup_db()
    raffle_id = add_raffle()
    with Session(SYNC_ENGINE) as s:
        s.add(User(telegram_id=USER_ID, raffle_id=raffle_id, fio="А А.А.", university="В"))
        s.commit()
    try:
        with Session(SYNC_ENGINE) as s:
            s.add(User(telegram_id=USER_ID, raffle_id=raffle_id, fio="Б Б.Б.", university="Г"))
            s.commit()
        return False, "дубликат прошёл, constraint не работает"
    except IntegrityError:
        return True, "IntegrityError — дубликат отклонён"
    finally:
        cleanup_db()


async def test_nontext_message_in_fio():
    state = fresh_state()
    await state.set_state(UserRegistration.fio)
    msg = make_msg(None)
    try:
        await process_fio(msg, state)
        assert "отправьте ФИО" in str(msg.answer.call_args)
        return True, "OK — бот просит отправить текст"
    except AttributeError as e:
        return False, f"AttributeError: {e}"


async def test_empty_text_in_fio():
    state = fresh_state()
    await state.set_state(UserRegistration.fio)
    msg = make_msg("   ")
    await process_fio(msg, state)
    assert "отправьте ФИО" in str(msg.answer.call_args)


async def test_empty_text_in_university():
    state = fresh_state()
    await state.set_state(UserRegistration.university)
    msg = make_msg("   ")
    await process_university(msg, state, make_send_bot())
    assert "отправьте название" in str(msg.answer.call_args)


async def test_username_saved_to_db():
    """message.from_user.username должен сохраняться (нужен фронтендеру: «место + имя + ник»)."""
    cleanup_db()
    raffle_id = add_raffle()
    state = fresh_state()
    await state.set_state(UserRegistration.university)
    await state.update_data(fio="Ников Н.Н.", raffle_id=raffle_id, require_subscribe=False)
    await process_university(make_msg("МГУ", username="ivan_the_user"), state, make_send_bot())
    u = last_user()
    assert u.username == "ivan_the_user", f"username не сохранён: {u.username!r}"


async def test_username_none_when_absent():
    cleanup_db()
    raffle_id = add_raffle()
    state = fresh_state()
    await state.set_state(UserRegistration.university)
    await state.update_data(fio="БезНика Б.Б.", raffle_id=raffle_id, require_subscribe=False)
    await process_university(make_msg("МГУ", username=None), state, make_send_bot())
    assert last_user().username is None


async def test_antifraud_alert_fires():
    """>5 регистраций за минуту — алерт всем админам.

    Тест также ловит баг с таймзоной: если границку считать datetime.now() на хосте
    (+03:00), а created_at пишется сервером MySQL (UTC), счётчик всегда 0 и алерт
    не срабатывает никогда.
    """
    cleanup_db()
    raffle_id = add_raffle()
    with Session(SYNC_ENGINE) as s:
        for i in range(6):
            s.add(User(telegram_id=900000 + i, raffle_id=raffle_id,
                       fio=f"Феик{i} Ф.Ф.", university="Бот"))
        s.commit()

    bot = make_send_bot()
    state = fresh_state()
    await state.set_state(UserRegistration.university)
    await state.update_data(fio="Седьмой С.С.", raffle_id=raffle_id, require_subscribe=False)
    await process_university(make_msg("ВУЗ"), state, bot)

    assert bot.send_message.call_count == len(ADMIN_IDS), \
        f"ожидали алерт каждому из {len(ADMIN_IDS)} админов, получили {bot.send_message.call_count}"
    text = bot.send_message.call_args.kwargs["text"]
    assert "АЛЕРТ АНТИФРОД" in text
    assert "7" in text, f"в алерте должно быть число регистраций: {text}"


async def test_antifraud_silent_below_threshold():
    cleanup_db()
    raffle_id = add_raffle()
    bot = make_send_bot()
    state = fresh_state()
    await state.set_state(UserRegistration.university)
    await state.update_data(fio="Обычный О.О.", raffle_id=raffle_id, require_subscribe=False)
    await process_university(make_msg("ВУЗ"), state, bot)
    bot.send_message.assert_not_called()


async def run_all():
    """Все тесты выполняются в одном event loop, чтобы пул SQLAlchemy не рвал мёртвые коннекты."""
    tests = [
        ("Нет активного розыгрыша", test_no_active_raffle),
        ("Юзер уже зарегистрирован (этот розыгрыш)", test_already_registered),
        ("raffle_id + require_subscribe в стейте", test_start_stores_raffle_id_in_state),
        ("Start без подписки", test_start_no_subscribe),
        ("Start с подпиской @формат", test_start_subscribe_at),
        ("Start с подпиской t.me", test_start_subscribe_tme),
        ("Подписка подтверждена", test_check_sub_success),
        ("Подписка не подтверждена", test_check_sub_not_subscribed),
        ("Ошибка get_chat_member", test_check_sub_error),
        ("Ввод ФИО", test_process_fio),
        ("ФИО >250 обрезается", test_process_fio_truncates),
        ("ВУЗ -> запись в БД (is_subscribed=False)", test_process_university_writes_db),
        ("ВУЗ -> запись в БД (is_subscribed=True)", test_process_university_is_subscribed_true),
        ("Сохранение username", test_username_saved_to_db),
        ("username=None если ника нет", test_username_none_when_absent),
        ("Антифрод: алерт при всплеске", test_antifraud_alert_fires),
        ("Антифрод: тише порога — тихо", test_antifraud_silent_below_threshold),
        ("Забаненный юзер заблокирован", test_banned_user_blocked),
        ("Полный happy path", test_full_happy_path),
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

    ok, detail = await test_nontext_message_in_fio()
    print(f"  {'OK  ' if ok else 'FAIL'} Нетекстовое сообщение в ФИО: {detail}")
    passed += 1 if ok else 0
    failed += 0 if ok else 1

    for name, fn in [
        ("Пустой текст в ФИО", test_empty_text_in_fio),
        ("Пустой текст в ВУЗ", test_empty_text_in_university),
    ]:
        try:
            await fn()
            print(f"  OK   {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
            failed += 1

    for name, fn in [
        ("Второй розыгрыш — повторная регистрация", test_second_raffle_allows_reregistration),
    ]:
        try:
            await fn()
            print(f"  OK   {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {name}: {type(e).__name__}: {e}")
            failed += 1

    ok, detail = await test_duplicate_ticket_same_raffle_rejected()
    print(f"  {'OK  ' if ok else 'FAIL'} Дубль в один розыгрыш отклонён: {detail}")
    passed += 1 if ok else 0
    failed += 0 if ok else 1

    from bot.database.engine import engine
    await engine.dispose()
    print(f"\nИтого: {passed} passed, {failed} failed")
    return failed


if __name__ == "__main__":
    import sys
    sys.exit(1 if asyncio.run(run_all()) else 0)
