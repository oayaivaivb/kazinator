"""Локальный тест отрисовки: генерирует summary.png и personal.png из заглушек."""

import os

from bot.services.image_gen import _draw_personal_image, _draw_summary_image


# Имитируем класс пользователя из базы данных
class MockWinner:
    """Минимальный маппинг ORM-User для вызова image_gen вне бота."""

    def __init__(self, fio, university, username=None):
        self.fio = fio
        self.university = university
        self.username = username


def run_test():
    print("Генерация тестовых изображений...")

    # Создаем 3 тестовых победителей (t3 шаблон)
    # Один из них без username, чтобы проверить фолбэк на ВУЗ
    winners = [
        MockWinner(fio="Иванов Иван", username="ivanov_tg", university="МГУ"),
        MockWinner(fio="Петрова Анна", username="anna_petrova", university="ВШЭ"),
        MockWinner(fio="Смирнов Алексей", username=None, university="МГТУ им. Баумана"),
    ]

    # 1. Тест общей таблицы
    summary_buf = _draw_summary_image(winners)
    with open("test_summary.png", "wb") as f:
        f.write(summary_buf.getvalue())
    print("✅ test_summary.png сохранен")

    # 2. Тест персональной карточки (username пока не участвует в отрисовке,
    # но передаём для совместимости с реальной сигнатурой)
    personal_buf = _draw_personal_image(fio="Иванов Иван", rank=1, username="ivanov_tg")
    with open("test_personal.png", "wb") as f:
        f.write(personal_buf.getvalue())
    print("✅ test_personal.png сохранен")


if __name__ == "__main__":
    # Убеждаемся, что папка assets существует, чтобы тест не упал
    os.makedirs("bot/assets", exist_ok=True)
    run_test()
