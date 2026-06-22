from __future__ import annotations

import os
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SqlEnum, Integer, String, Text, func
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/telegram_photo_vault.db")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(AsyncAttrs, DeclarativeBase):
    pass


class PhotoStatus(str, Enum):
    PENDING = "PENDING"
    DOWNLOADED = "DOWNLOADED"
    TG_UPLOADED = "TG_UPLOADED"
    COMPRESSED = "COMPRESSED"
    ODROID_UPLOADED = "ODROID_UPLOADED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Photo(Base):
    __tablename__ = "photos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mega_path: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False, index=True)
    local_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    compressed_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[PhotoStatus] = mapped_column(
        SqlEnum(PhotoStatus, name="photo_status", native_enum=False),
        default=PhotoStatus.PENDING,
        nullable=False,
        index=True,
    )
    tg_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_log: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
