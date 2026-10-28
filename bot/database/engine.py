"""Асинхронный движок SQLAlchemy и фабрика сессий (общие для всего бота)."""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.config import DATABASE_URL

engine = create_async_engine(DATABASE_URL, echo=False)

# expire_on_commit=False оставляет прочитанные объекты (например, raffle)
# доступными после commit — иначе следующий lazy-load вне сессии уронит MissingGreenlet
async_session = async_sessionmaker(engine, expire_on_commit=False)
