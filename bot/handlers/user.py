"""Хендлеры пользователя: старт, проверка подписки и пошаговая регистрация."""

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select, text

from bot.config import ADMIN_IDS
from bot.database.engine import async_session
from bot.database.models import Raffle, User
from bot.states.user import UserRegistration

user_router = Router()

USER_HELP_TEXT = (
    "🎉 <b>Как участвовать в розыгрыше</b>\n\n"
    "1. Запустите регистрацию командой /start.\n"
    "2. Если условие розыгрыша — подписка на канал, подпишитесь и нажмите «✅ Я подписался».\n"
    "3. Введите ФИО, затем название ВУЗа.\n\n"
    "С одного аккаунта — одна заявка на конкретный розыгрыш.\n"
    "Победителей определяет случайный рандомайзер после окончания приёма заявок.\n\n"
    "🏆 Победителям приходит персональная картинка-поздравление."
)


# /help регистрируем до шагов анкеты: иначе ввод проглотит хендлер UserRegistration.fio.
# Состояние не трогаем — после справки анкета продолжится с того же шага.
@user_router.message(Command("help", ignore_case=True))
async def user_help(message: Message):
    """Короткая инструкция для участника."""
    await message.answer(USER_HELP_TEXT, parse_mode="HTML")


@user_router.message(CommandStart())
async def user_start(message: Message, state: FSMContext):
    """/start: находит активный розыгрыш и запускает регистрацию."""
    await state.clear()

    async with async_session() as session:
        # Ищем последний активный розыгрыш
        result = await session.execute(select(Raffle).where(Raffle.is_finished == False).order_by(Raffle.id.desc()))
        active_raffle = result.scalars().first()

        if not active_raffle:
            return await message.answer("😔 В данный момент нет активных розыгрышей.")

        # Глобальная проверка на бан (по всем заявкам)
        banned_check = await session.execute(
            select(User).where(User.telegram_id == message.from_user.id, User.is_banned == True)
        )
        if banned_check.scalars().first():
            return await message.answer("❌ Вы заблокированы и не можете принимать участие в розыгрышах.")

        # Проверяем, не зарегистрирован ли уже юзер именно в этом розыгрыше
        user_result = await session.execute(
            select(User).where(
                User.telegram_id == message.from_user.id,
                User.raffle_id == active_raffle.id,
            )
        )
        existing_user = user_result.scalars().first()
        if existing_user:
            return await message.answer("✅ Вы уже зарегистрированы в розыгрыше!")

        # Сохраняем данные розыгрыша в стейт для следующих шагов
        await state.update_data(
            raffle_id=active_raffle.id,
            channel=active_raffle.channel_username,
            require_subscribe=active_raffle.require_subscribe,
        )

    if active_raffle.require_subscribe and active_raffle.channel_username:
        # Формируем корректную ссылку для кнопки
        url = active_raffle.channel_username
        if url.startswith("@"):
            url = f"https://t.me/{url[1:]}"

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📢 Перейдите в канал", url=url)],
                [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subscription")],
            ]
        )
        await message.answer("Для участия необходимо подписаться на наш канал!", reply_markup=keyboard)
        await state.set_state(UserRegistration.check_sub)
    else:
        await message.answer("📝 Давайте зарегистрируемся!\n\nВведите ваши ФИО:")
        await state.set_state(UserRegistration.fio)


@user_router.callback_query(F.data == "check_subscription", UserRegistration.check_sub)
async def check_sub_callback(callback: CallbackQuery, bot: Bot, state: FSMContext):
    """Кнопка «Я подписался»: проверяет членство в канале через getChatMember."""
    data = await state.get_data()
    channel = data.get("channel")

    # Для API getChatMember нужен формат @username
    if channel.startswith("https://t.me/"):
        channel = "@" + channel.split("/")[-1]

    try:
        member = await bot.get_chat_member(chat_id=channel, user_id=callback.from_user.id)
        if member.status in ["member", "administrator", "creator"]:
            await callback.message.edit_text("✅ Подписка подтверждена!\n\nВведите ваши ФИО:")
            await state.set_state(UserRegistration.fio)
        else:
            # Показываем всплывающее окно (alert), чтобы не засорять историю чата
            await callback.answer("❌ Вы еще не подписались на канал!", show_alert=True)
    except Exception:
        await callback.message.answer("⚠️ Ошибка проверки подписки. Возможно, бот не добавлен в администраторы канала.")
        await callback.answer()


@user_router.message(UserRegistration.fio)
async def process_fio(message: Message, state: FSMContext):
    """Сбор ФИО. Защита от пустых сообщений (стикеры/фото без текста) и слишком длинного ввода."""
    if not message.text or not message.text.strip():
        return await message.answer("Пожалуйста, отправьте ФИО обычным текстом.")
    fio = message.text.strip()[:250]
    await state.update_data(fio=fio)
    await message.answer("🎓 Отлично! Теперь введите название вашего ВУЗа:")
    await state.set_state(UserRegistration.university)


@user_router.message(UserRegistration.university)
async def process_university(message: Message, state: FSMContext, bot: Bot):
    """Финальный шаг регистрации: запись в БД + антифрод-алерт админам."""
    if not message.text or not message.text.strip():
        return await message.answer("Пожалуйста, отправьте название ВУЗа обычным текстом.")
    data = await state.get_data()
    fio = data.get("fio")
    raffle_id = data.get("raffle_id")
    university = message.text.strip()[:250]

    async with async_session() as session:
        new_user = User(
            telegram_id=message.from_user.id,
            username=message.from_user.username,
            raffle_id=raffle_id,
            fio=fio,
            university=university,
            is_subscribed=data.get("require_subscribe", False),
            is_banned=False,
        )
        session.add(new_user)
        await session.commit()

    await message.answer("🎉 Вы успешно зарегистрированы в розыгрыше! Желаем удачи.")
    await state.clear()

    # --- АНТИФРОД: Проверка всплеска регистраций ---
    # Окно считаем на стороне БД (NOW() - INTERVAL), а не datetime.now() на хосте:
    # created_at пишется через func.now() сервером MySQL (в контейнере это UTC),
    # а часовой пояс хоста +03:00 — при сравнении хостного времени с серверным
    # условие никогда не выполнится и алерт не сработает вообще.
    async with async_session() as fraud_session:
        recent_regs = await fraud_session.scalar(
            select(func.count()).select_from(User).where(User.created_at >= text("NOW() - INTERVAL 1 MINUTE"))
        )

    if recent_regs and recent_regs > 5:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=f"⚠️ <b>АЛЕРТ АНТИФРОД</b>\n"
                    f"Замечена аномальная активность: {recent_regs} регистраций за последнюю минуту!",
                    parse_mode="HTML",
                )
            except Exception:
                pass
