from aiogram.fsm.state import State, StatesGroup


class UserRegistration(StatesGroup):
    check_sub = State()
    fio = State()
    university = State()
