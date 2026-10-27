import io
import os

import asyncio
from PIL import Image, ImageDraw, ImageFont

# Пути к ресурсам из папки bot/assets/
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(BASE_DIR, 'assets')
FONT_PATH = os.path.join(ASSETS_DIR, 'font.ttf')
SUMMARY_BG = os.path.join(ASSETS_DIR, 'summary_bg.png')
PERSONAL_BG = os.path.join(ASSETS_DIR, 'personal_bg.png')


def _load_font(size: int):
    """Шрифт из assets, с откатом на дефолтный шрифт Pillow."""
    if os.path.exists(FONT_PATH):
        return ImageFont.truetype(FONT_PATH, size)
    return ImageFont.load_default(size)


def _load_background(path: str, size: tuple, color: tuple):
    """Фон из assets, с откатом на сплошную заливку."""
    if os.path.exists(path):
        bg = Image.open(path)
        bg = bg.convert('RGB')
        if bg.size != size:
            bg = bg.resize(size)
        return bg
    return Image.new('RGB', size, color=color)


def _draw_summary_image(winners: list) -> io.BytesIO:
    """Генерирует общую таблицу победителей для админов (Заглушка)."""
    size = (800, 600)
    img = _load_background(SUMMARY_BG, size, (50, 50, 50))
    d = ImageDraw.Draw(img)
    font = _load_font(24)

    # Здесь Матвей подставит свои координаты и стили
    d.text((50, 50), "Итоги розыгрыша:", font=font, fill=(255, 255, 255))
    y = 100
    for i, w in enumerate(winners, 1):
        # Если добавлен username, его тоже нужно выводить
        nick = f"  @{w.username}" if getattr(w, 'username', None) else ''
        line = f"{i}. {w.fio}\n   {w.university}{nick}"
        d.text((50, y), line, font=font, fill=(200, 255, 200))
        y += 40

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


def _draw_personal_image(fio: str, rank: int, username: str | None = None) -> io.BytesIO:
    """Генерирует персональную карточку победителя (Заглушка)."""
    size = (800, 400)
    img = _load_background(PERSONAL_BG, size, (73, 109, 137))
    d = ImageDraw.Draw(img)
    font = _load_font(28)

    # Здесь Матвей подставит свой шаблон и координаты
    nick = f" @{username}" if username else ''
    text = f"Поздравляем!\n{fio}{nick}\nВы заняли {rank} место!"
    d.text((50, 150), text, font=font, fill=(255, 255, 0))

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


async def generate_summary_image(winners: list) -> io.BytesIO:
    return await asyncio.to_thread(_draw_summary_image, winners)


async def generate_personal_image(fio: str, rank: int, username: str | None = None) -> io.BytesIO:
    return await asyncio.to_thread(_draw_personal_image, fio, rank, username)
