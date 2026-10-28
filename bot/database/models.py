"""ORM-модели SQLAlchemy: участники (User), розыгрыши (Raffle), победители (Winner)."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    # Один и тот же telegram_id может участвовать в разных розыгрышах,
    # поэтому уникальность — по паре (telegram_id, raffle_id), а не по одному полю.
    __table_args__ = (UniqueConstraint("telegram_id", "raffle_id", name="uix_telegram_raffle"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    raffle_id: Mapped[int] = mapped_column(ForeignKey("raffles.id", ondelete="CASCADE"))
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fio: Mapped[str] = mapped_column(String(255))
    university: Mapped[str] = mapped_column(String(255))
    user_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_subscribed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class Raffle(Base):
    __tablename__ = "raffles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    winner_slots: Mapped[int] = mapped_column(Integer)
    require_subscribe: Mapped[bool] = mapped_column(Boolean, default=False)
    channel_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    require_comment: Mapped[bool] = mapped_column(Boolean, default=False)
    comment_min_length: Mapped[int] = mapped_column(Integer, default=0)
    is_finished: Mapped[bool] = mapped_column(Boolean, default=False)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Winner(Base):
    __tablename__ = "winners"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    raffle_id: Mapped[int] = mapped_column(ForeignKey("raffles.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
