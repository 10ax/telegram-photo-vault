"""Provenance: which local record, if any, claims each message in the channel."""
import sqlite3

from sqlalchemy import select

from app.models.database import (
    AsyncSessionLocal,
    CatalogItem,
    CatalogSource,
    ChannelRole,
    MediaType,
    Photo,
    PhotoStatus,
)
from app.services.catalog import CatalogService


async def _item(**kwargs) -> None:
    defaults = {
        "channel_id": -100,
        "channel_role": ChannelRole.ARCHIVE,
        "media_kind": "document",
    }
    async with AsyncSessionLocal() as session:
        session.add(CatalogItem(**{**defaults, **kwargs}))
        await session.commit()


def _state_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE files (rel_path TEXT PRIMARY KEY, size INTEGER, sha256 TEXT, "
        "status TEXT, tg_message_id INTEGER)"
    )
    conn.executemany("INSERT INTO files VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


async def test_a_message_the_worker_uploaded_is_attributed_to_the_worker(clean_db):
    async with AsyncSessionLocal() as session:
        session.add(
            Photo(
                mega_path="/phone_bkp/a.jpg",
                status=PhotoStatus.COMPLETED,
                media_type=MediaType.IMAGE,
                tg_message_id=42,
                sha256="a" * 64,
            )
        )
        await session.commit()
    await _item(tg_message_id=42, file_name="a.jpg")

    assert await CatalogService(None, []).match_worker() == 1

    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.source is CatalogSource.WORKER
    assert item.photo_id is not None
    assert item.sha256 == "a" * 64


async def test_a_message_no_local_record_claims_stays_unknown(clean_db):
    """15,193 of 18,294 rows are in this state. It is the legacy, not a failure."""
    await _item(tg_message_id=999, file_name="mystery.jpg")

    assert await CatalogService(None, []).match_worker() == 0

    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.source is CatalogSource.UNKNOWN
    assert item.photo_id is None


async def test_the_backup_script_db_attributes_its_own_channel(clean_db, tmp_path):
    state = tmp_path / "iphone.db"
    _state_db(state, [("100APPLE/IMG_0014.JPG", 2_400_000, "b" * 64, "VERIFIED", 7)])
    await _item(channel_id=-200, tg_message_id=7, file_name="IMG_0014.JPG")

    assert await CatalogService(None, []).match_backup_db(state) == 1

    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.source is CatalogSource.BACKUP_SCRIPT
    assert item.backup_rel_path == "100APPLE/IMG_0014.JPG"
    assert item.sha256 == "b" * 64


async def test_matching_falls_back_to_sha256_when_message_ids_disagree(clean_db, tmp_path):
    """A re-upload changes the message id but not the bytes."""
    state = tmp_path / "iphone.db"
    _state_db(state, [("100APPLE/IMG_0015.JPG", 10, "c" * 64, "VERIFIED", 11)])
    await _item(channel_id=-200, tg_message_id=99, file_name="IMG_0015.JPG", sha256="c" * 64)

    assert await CatalogService(None, []).match_backup_db(state) == 1

    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.source is CatalogSource.BACKUP_SCRIPT
    assert item.backup_rel_path == "100APPLE/IMG_0015.JPG"


async def test_matching_is_idempotent(clean_db):
    async with AsyncSessionLocal() as session:
        session.add(
            Photo(
                mega_path="/phone_bkp/b.jpg",
                status=PhotoStatus.COMPLETED,
                media_type=MediaType.IMAGE,
                tg_message_id=5,
            )
        )
        await session.commit()
    await _item(tg_message_id=5, file_name="b.jpg")

    service = CatalogService(None, [])
    assert await service.match_worker() == 1
    assert await service.match_worker() == 0


async def test_a_missing_state_db_is_reported_not_raised(clean_db, tmp_path):
    assert await CatalogService(None, []).match_backup_db(tmp_path / "nope.db") == 0


async def test_an_unreadable_state_db_is_reported_not_raised(clean_db, tmp_path):
    """A state DB that exists but is corrupt or has the wrong schema must not crash a run."""
    state = tmp_path / "corrupt.db"
    state.write_bytes(b"not a sqlite database")
    await _item(tg_message_id=999, file_name="mystery.jpg")

    assert await CatalogService(None, []).match_backup_db(state) == 0

    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.source is CatalogSource.UNKNOWN
