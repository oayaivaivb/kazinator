import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Парсим строку с ID админов в список целых чисел
_admin_ids_str = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(uid.strip()) for uid in _admin_ids_str.split(",") if uid.strip().isdigit()]
