from __future__ import annotations

import os
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SqlEnum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
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
    CHUNK_UPLOADING = "CHUNK_UPLOADING"
    TG_UPLOADED = "TG_UPLOADED"
    COMPRESSED = "COMPRESSED"
    ODROID_UPLOADED = "ODROID_UPLOADED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class MediaType(str, Enum):
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    OTHER = "OTHER"


class RecoveryStatus(str, Enum):
    SCANNED = "SCANNED"
    DOWNLOADED = "DOWNLOADED"
    PLANNED = "PLANNED"
    REUPLOADED = "REUPLOADED"
    COMPLETED = "COMPLETED"
    SKIPPED = "SKIPPED"
    DUPLICATE = "DUPLICATE"
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
    media_type: Mapped[MediaType] = mapped_column(
        SqlEnum(MediaType, name="media_type", native_enum=False),
        default=MediaType.IMAGE,
        nullable=False,
    )
    # Step the photo was in when it was marked FAILED; used to resume on retry.
    failed_status: Mapped[PhotoStatus | None] = mapped_column(
        SqlEnum(PhotoStatus, name="failed_photo_status", native_enum=False),
        nullable=True,
    )
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_chunked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    total_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    manifest_tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
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


class ChunkStatus(str, Enum):
    PENDING = "PENDING"
    UPLOADED = "UPLOADED"


class UploadChunk(Base):
    __tablename__ = "upload_chunks"
    __table_args__ = (UniqueConstraint("photo_id", "part_index", name="uq_chunk_photo_part"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    photo_id: Mapped[int] = mapped_column(
        ForeignKey("photos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    part_index: Mapped[int] = mapped_column(Integer, nullable=False)
    part_count: Mapped[int] = mapped_column(Integer, nullable=False)
    offset: Mapped[int] = mapped_column(BigInteger, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(600), nullable=False)
    status: Mapped[ChunkStatus] = mapped_column(
        SqlEnum(ChunkStatus, name="chunk_status", native_enum=False),
        default=ChunkStatus.PENDING,
        nullable=False,
        index=True,
    )
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class RecoveryItem(Base):
    __tablename__ = "recovery_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_message_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    media_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    file_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[RecoveryStatus] = mapped_column(
        SqlEnum(RecoveryStatus, name="recovery_status", native_enum=False),
        default=RecoveryStatus.SCANNED,
        nullable=False,
        index=True,
    )
    local_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    planned_caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
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


# Additive column migrations for databases created by older versions of the schema.
# SQLite's create_all only creates missing tables, never missing columns.
_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "photos": {
        "media_type": "VARCHAR(5) NOT NULL DEFAULT 'IMAGE'",
        "failed_status": "VARCHAR(15)",
        "is_chunked": "BOOLEAN NOT NULL DEFAULT 0",
        "sha256": "VARCHAR(64)",
        "total_size": "BIGINT",
        "manifest_tg_message_id": "BIGINT",
    },
}


async def _apply_column_migrations(conn) -> None:
    for table, columns in _COLUMN_MIGRATIONS.items():
        result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
        existing = {row[1] for row in result.fetchall()}
        if not existing:
            continue
        for column, ddl in columns.items():
            if column not in existing:
                await conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _apply_column_migrations(conn)
