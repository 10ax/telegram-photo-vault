from __future__ import annotations

import os
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SqlEnum,
    Float,
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
    tg_media_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Native captioned copy mirrored to the shared browse channel (best-effort).
    browse_tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
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
    # Set once this item has been copied into the shared browse gallery channel.
    browse_tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
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


class ChannelRole(str, Enum):
    """What a channel is for.

    ARCHIVE holds originals and is a gallery source. MIRROR holds native
    Telegram copies of things already archived elsewhere: it is scanned so the
    report can show drift, and is never enriched or exported.
    """

    ARCHIVE = "ARCHIVE"
    MIRROR = "MIRROR"


class CatalogSource(str, Enum):
    WORKER = "WORKER"
    BACKUP_SCRIPT = "BACKUP_SCRIPT"
    UNKNOWN = "UNKNOWN"


class CatalogItem(Base):
    """One media message in one channel.

    The channel is the archive; this table is a cache of what we know about it.
    That is why the key is the message and not a local path: a row can only be
    created by seeing the message, and a row that stops matching a message is
    exactly the signal the reconciliation report exists to surface.
    """

    __tablename__ = "catalog_items"
    __table_args__ = (
        UniqueConstraint("channel_id", "tg_message_id", name="uq_catalog_channel_message"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    tg_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_role: Mapped[ChannelRole] = mapped_column(
        SqlEnum(ChannelRole, name="channel_role", native_enum=False),
        default=ChannelRole.ARCHIVE,
        nullable=False,
    )

    media_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Per-message classification: a chunked upload is N "chunk" rows plus one
    # "manifest" row. Counting those as N+1 photos would corrupt every number in
    # the report, so they are labelled here and excluded by the counters.
    artifact: Mapped[str | None] = mapped_column(String(16), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # When it was posted, which for a migrated archive is not when it was shot.
    message_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gps_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    camera_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # NULL means never attempted. Set even on failure, so a permanently
    # unreadable file costs one fetch rather than one per run, forever.
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enrich_error: Mapped[str | None] = mapped_column(String(255), nullable=True)

    source: Mapped[CatalogSource] = mapped_column(
        SqlEnum(CatalogSource, name="catalog_source", native_enum=False),
        default=CatalogSource.UNKNOWN,
        nullable=False,
        index=True,
    )
    photo_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("photos.id"), nullable=True)
    backup_rel_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    exported_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
        "tg_media_message_id": "BIGINT",
        "browse_tg_message_id": "BIGINT",
    },
    "recovery_items": {
        "browse_tg_message_id": "BIGINT",
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
