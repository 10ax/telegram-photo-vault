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


def _state_db(path, rows, channel_id=None):
    """The shape scripts/backup_local_folder.py writes: files, plus a meta table.

    `channel_id=None` reproduces a state DB written before that script recorded
    the channel it uploaded to.
    """
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE files (rel_path TEXT PRIMARY KEY, size INTEGER, sha256 TEXT, "
        "status TEXT, tg_message_id INTEGER)"
    )
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    if channel_id is not None:
        conn.execute("INSERT INTO meta VALUES ('channel_id', ?)", (str(channel_id),))
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

    assert await CatalogService(None, [], worker_channel_id=-100).match_worker() == 1

    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.source is CatalogSource.WORKER
    assert item.photo_id is not None
    assert item.sha256 == "a" * 64


async def test_a_message_no_local_record_claims_stays_unknown(clean_db):
    """15,193 of 18,294 rows are in this state. It is the legacy, not a failure."""
    await _item(tg_message_id=999, file_name="mystery.jpg")

    assert await CatalogService(None, [], worker_channel_id=-100).match_worker() == 0

    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.source is CatalogSource.UNKNOWN
    assert item.photo_id is None


async def test_the_backup_script_db_attributes_its_own_channel(clean_db, tmp_path):
    """Its own, and only its own: the same message id sits in the main archive too."""
    state = tmp_path / "iphone.db"
    _state_db(
        state,
        [("100APPLE/IMG_0014.JPG", 2_400_000, "b" * 64, "VERIFIED", 7)],
        channel_id=-200,
    )
    await _item(channel_id=-200, tg_message_id=7, file_name="IMG_0014.JPG")
    await _item(channel_id=-100, tg_message_id=7, file_name="PXL_20260713_115033830.jpg")

    assert await CatalogService(None, []).match_backup_db(state) == 1

    async with AsyncSessionLocal() as session:
        rows = {
            i.channel_id: i for i in (await session.scalars(select(CatalogItem))).all()
        }
    assert rows[-200].source is CatalogSource.BACKUP_SCRIPT
    assert rows[-200].backup_rel_path == "100APPLE/IMG_0014.JPG"
    assert rows[-200].sha256 == "b" * 64
    # The main archive's message 7 is a different file entirely. Stamping the
    # iPhone file's hash onto it would poison the tier the lookup trusts most.
    assert rows[-100].source is CatalogSource.UNKNOWN
    assert rows[-100].backup_rel_path is None
    assert rows[-100].sha256 is None


async def test_matching_falls_back_to_sha256_when_message_ids_disagree(clean_db, tmp_path):
    """A re-upload changes the message id but not the bytes."""
    state = tmp_path / "iphone.db"
    _state_db(
        state,
        [("100APPLE/IMG_0015.JPG", 10, "c" * 64, "VERIFIED", 11)],
        channel_id=-200,
    )
    await _item(channel_id=-200, tg_message_id=99, file_name="IMG_0015.JPG", sha256="c" * 64)

    assert await CatalogService(None, []).match_backup_db(state) == 1

    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.source is CatalogSource.BACKUP_SCRIPT
    assert item.backup_rel_path == "100APPLE/IMG_0015.JPG"


async def test_the_worker_claims_only_its_own_channels_copy_of_a_message_id(clean_db):
    """Every channel numbers its messages from 1, so id 42 exists in all three."""
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
    await _item(channel_id=-100, tg_message_id=42, file_name="a.jpg")
    await _item(channel_id=-200, tg_message_id=42, file_name="IMG_0014.JPG")

    assert await CatalogService(None, [], worker_channel_id=-100).match_worker() == 1

    async with AsyncSessionLocal() as session:
        rows = {i.channel_id: i for i in (await session.scalars(select(CatalogItem))).all()}
    assert rows[-100].source is CatalogSource.WORKER
    assert rows[-100].sha256 == "a" * 64
    assert rows[-200].source is CatalogSource.UNKNOWN
    assert rows[-200].sha256 is None


async def test_a_worker_with_no_channel_configured_attributes_nothing(clean_db):
    """Without a channel, a message id is not evidence — so it claims nothing."""
    async with AsyncSessionLocal() as session:
        session.add(
            Photo(
                mega_path="/phone_bkp/a.jpg",
                status=PhotoStatus.COMPLETED,
                media_type=MediaType.IMAGE,
                tg_message_id=42,
            )
        )
        await session.commit()
    await _item(tg_message_id=42, file_name="a.jpg")

    assert await CatalogService(None, []).match_worker() == 0

    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.source is CatalogSource.UNKNOWN


async def test_a_state_db_without_a_channel_id_ignores_message_ids(clean_db, tmp_path):
    """An older state DB: the bare id could belong to any channel, so it is not used."""
    state = tmp_path / "legacy.db"
    _state_db(state, [("100APPLE/IMG_0016.JPG", 10, "d" * 64, "VERIFIED", 3)])
    await _item(channel_id=-200, tg_message_id=3, file_name="IMG_0016.JPG")

    assert await CatalogService(None, []).match_backup_db(state) == 0

    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.source is CatalogSource.UNKNOWN


async def test_a_state_db_without_a_channel_id_still_matches_on_sha256(clean_db, tmp_path):
    """Content identity holds wherever the bytes sit, so the hash path survives."""
    state = tmp_path / "legacy.db"
    _state_db(state, [("100APPLE/IMG_0017.JPG", 10, "e" * 64, "VERIFIED", 3)])
    await _item(channel_id=-200, tg_message_id=77, file_name="IMG_0017.JPG", sha256="e" * 64)

    assert await CatalogService(None, []).match_backup_db(state) == 1

    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.source is CatalogSource.BACKUP_SCRIPT
    assert item.backup_rel_path == "100APPLE/IMG_0017.JPG"


async def test_match_all_uses_the_state_db_the_service_was_built_with(clean_db, tmp_path):
    """BACKUP_STATE_DB is a constructor kwarg now, not a getenv inside a handler."""
    state = tmp_path / "iphone.db"
    _state_db(
        state,
        [("100APPLE/IMG_0018.JPG", 10, "f" * 64, "VERIFIED", 4)],
        channel_id=-200,
    )
    await _item(channel_id=-200, tg_message_id=4, file_name="IMG_0018.JPG")

    service = CatalogService(None, [], worker_channel_id=-100, backup_state_db=state)
    assert await service.match_all() == {"worker": 0, "backup_script": 1}


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

    service = CatalogService(None, [], worker_channel_id=-100)
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
