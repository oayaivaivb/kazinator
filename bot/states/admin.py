from aiogram.fsm.state import State, StatesGroup


class NewRaffle(StatesGroup):
    winner_slots = State()
    deadline = State()
    channel = State()
    confirm = State()


class BanUser(StatesGroup):
    waiting_for_id = State()
