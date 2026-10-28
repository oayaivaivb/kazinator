"""FSM-состояния администратора (по шагам сценариев)."""

from aiogram.fsm.state import State, StatesGroup


class NewRaffle(StatesGroup):
    """Пошаговое создание розыгрыша: слоты -> дедлайн -> канал -> подтверждение."""
    winner_slots = State()
    deadline = State()
    channel = State()
    confirm = State()


class BanUser(StatesGroup):
    waiting_for_id = State()
