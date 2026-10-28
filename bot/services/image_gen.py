import asyncio
import io
import json
import os

from PIL import Image, ImageDraw, ImageFont

# Дважды dirname: .../bot/services/image_gen.py -> .../bot/services -> .../bot.
# Абсолютные пути нужны, чтобы генерация работала при запуске бота из любой папки.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
FONT_PATH = os.path.join(ASSETS_DIR, "font.ttf")
PERSONAL_BG = os.path.join(ASSETS_DIR, "personal_bg.png")
LAYOUT_JSON_PATH = os.path.join(ASSETS_DIR, "table_layout.json")


def _get_layout_data():
    """Читает table_layout.json с координатами ячеек таблицы победителей.

    Возвращает dict вида {"t3": {...}, "t5": {...}} или None, если файл
    отсутствует. В последнем случае вызывающий код уходит в текстовый
    фолбэк вместо падения с исключением.
    """
    if not os.path.exists(LAYOUT_JSON_PATH):
        print(f"❌ ОШИБКА: Файл {LAYOUT_JSON_PATH} не найден!")
        return None
    with open(LAYOUT_JSON_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _draw_summary_image(winners: list) -> io.BytesIO:
    """Рисует общую таблицу победителей поверх PNG-макета нужного размера.

    Аргументы:
        winners: список объектов победителей, у которых читаются атрибуты
            ``fio``, ``username`` (может быть None) и ``university``.
            Порядок списка = порядок мест в таблице.

    Возвращает:
        io.BytesIO с готовым PNG; буфер уже перемотан в начало, его можно
        передавать в aiogram сразу без дополнительного seek().

    Шаблоны (t3/t5/t10/t15) задают только координаты текста — сама сетка,
    шапка и фон находятся в файле фона table_<N>.jpg.
    """
    layouts = _get_layout_data()
    winners_count = len(winners)

    if layouts:
        # Ключи JSON имеют вид "t3", "t5" -> приводим их к числам для сравнения
        available_keys = [int(k.replace("t", "")) for k in layouts.keys()]
        suitable_keys = [k for k in available_keys if k >= winners_count]
        # Берём минимальный подходящий шаблон (не больше нужного), а если
        # победителей больше всех шаблонов — самый вместительный из имеющихся
        target_num = min(suitable_keys) if suitable_keys else max(available_keys)
        target_key = f"t{target_num}"
        layout = layouts.get(target_key)
        # Формируем точное имя файла, основываясь на картинках (table_3.jpg, table_5.jpg и т.д.)
        current_bg_path = os.path.join(ASSETS_DIR, f"table_{target_num}.jpg")
    else:
        layout = None
        current_bg_path = ""

    if layout:
        try:
            img = Image.open(current_bg_path).convert("RGB")
        except FileNotFoundError:
            print(f"⚠️ ФОН НЕ НАЙДЕН: {current_bg_path}. Рисую заглушку.")
            # Размер берём из layout, чтобы координаты текста не ушли за холст
            img = Image.new("RGB", (layout["w"], layout["h"]), color=(50, 50, 50))

        d = ImageDraw.Draw(img)

        try:
            # fontPx в JSON дробный (например 120.7), а truetype требует int
            font = ImageFont.truetype(FONT_PATH, int(layout["fontPx"]))
        except OSError:
            print("❌ ШРИФТ НЕ НАЙДЕН: Использую микро-шрифт по умолчанию!")
            font = ImageFont.load_default()

        for i, w in enumerate(winners):
            if i >= len(layout["rows"]):
                # Победителей больше, чем строк в шаблоне — лишние отбрасываем,
                # иначе полезли бы за нижний край макета
                break

            row = layout["rows"][i]

            # В JSON каждая ячейка — это прямоугольник [x, y, w, h].
            # Pillow рисует текст по левому-верхнему углу, поэтому нужны
            # только первые два числа; ширина и высота здесь не используются.
            num_x, num_y = row["num"][0], row["num"][1]
            fam_x, fam_y = row["fam"][0], row["fam"][1]
            name_x, name_y = row["name"][0], row["name"][1]

            d.text((num_x, num_y), str(i + 1), fill=(255, 255, 255), font=font)
            d.text((fam_x, fam_y), w.fio, fill=(255, 255, 255), font=font)

            # Ник показывает только если он реально заполнен: пустая строка или
            # None дают фолбэк на ВУЗ. getattr — защита от объектов без поля username.
            username_text = f"@{w.username}" if getattr(w, "username", None) else w.university
            d.text((name_x, name_y), username_text, fill=(255, 255, 255), font=font)
    else:
        # Аварийный вариант без макета: обычная текстовая простыня 800x600
        img = Image.new("RGB", (800, 600), color=(50, 50, 50))
        d = ImageDraw.Draw(img)
        font = ImageFont.load_default()
        d.text((50, 50), "Итоги розыгрыша:", fill=(255, 255, 255), font=font)
        y = 100
        for i, w in enumerate(winners, 1):
            username_text = f"@{w.username}" if getattr(w, "username", None) else w.university
            d.text((50, y), f"{i}. {w.fio}\n{username_text}", fill=(200, 255, 200), font=font)
            y += 40

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _draw_personal_image(fio: str, rank: int, username: str = None) -> io.BytesIO:
    """Рисует персональную карточку победителя поверх personal_bg.png.

    Аргументы:
        fio: фамилия и имя победителя.
        rank: занятое место (число, подставляется в текст поздравления).
        username: ник в Telegram. Сейчас в отрисовку не попадает — параметр
            сохранён ради совместимой сигнатуры с generate_personal_image.

    Возвращает:
        io.BytesIO с PNG-картинкой, перемотанный в начало.
    """
    try:
        img = Image.open(PERSONAL_BG).convert("RGB")
    except FileNotFoundError:
        # Заглушка ровно с габаритами макета карточки, чтобы текст не обрезался
        img = Image.new("RGB", (800, 400), color=(73, 109, 137))

    d = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype(FONT_PATH, 48)
    except OSError:
        font = ImageFont.load_default()

    # Координаты (50, 150) подобраны вручную под personal_bg.png,
    # в отличие от общей таблицы они НЕ берутся из table_layout.json
    d.text((50, 150), f"Поздравляем!\n{fio}\nВы заняли {rank} место!", fill=(255, 255, 0), font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


async def generate_summary_image(winners: list) -> io.BytesIO:
    """Асинхронная обёртка над _draw_summary_image для вызова из хендлеров.

    Рисование в Pillow — блокирующая CPU операция на десятки/сотни мс
    (макеты 4547x2695), поэтому оно уводится в отдельный поток через
    asyncio.to_thread и не приостанавливает event loop бота.
    """
    return await asyncio.to_thread(_draw_summary_image, winners)


async def generate_personal_image(fio: str, rank: int, username: str = None) -> io.BytesIO:
    """Асинхронная обёртка над _draw_personal_image (см. generate_summary_image)."""
    return await asyncio.to_thread(_draw_personal_image, fio, rank, username)
