"""Candidate selection against a real catalog_items, and the empty-catalog refusal."""
from datetime import datetime, timezone

import pytest

from app.models.database import (
    AsyncSessionLocal,
    CatalogItem,
    ChannelRole,
    DeviceVerdict,
    MatchTier,
    Photo,
    PhotoStatus,
)
from app.services.reconcile import CatalogNeverScanned, ReconcileService

ARCHIVE = -1002637897512
MIRROR = -1004367643112
SCANNED = datetime(2026, 7, 20, tzinfo=timezone.utc)
OLDER = datetime(2026, 7, 1, tzinfo=timezone.utc)


async def _row(**kwargs):
    defaults = dict(
        channel_id=ARCHIVE,
        channel_role=ChannelRole.ARCHIVE,
        media_kind="document",
        message_date=SCANNED,
    )
    defaults.update(kwargs)
    async with AsyncSessionLocal() as session:
        session.add(CatalogItem(**defaults))
        await session.commit()


def _entry(name="a.jpg", size=100, **kwargs):
    entry = {"relpath": f"DCIM/{name}", "name": name, "size": size,
             "mtime": OLDER.isoformat(), "sha256": None}
    entry.update(kwargs)
    return entry


async def test_a_match_in_an_archive_channel_is_archived(clean_db):
    await _row(tg_message_id=1, file_name="a.jpg", file_size=100)
    [decision] = await ReconcileService().evaluate([_entry()])
    assert decision.verdict is DeviceVerdict.ARCHIVED
    assert decision.tier is MatchTier.NAME_SIZE


async def test_a_match_only_in_the_mirror_channel_is_not_proof(clean_db):
    """The browse channel holds Telegram-recompressed copies, not originals."""
    await _row(tg_message_id=2, channel_id=MIRROR, channel_role=ChannelRole.MIRROR,
               file_name="a.jpg", file_size=100)
    await _row(tg_message_id=3, file_name="unrelated.jpg", file_size=1)
    [decision] = await ReconcileService().evaluate([_entry()])
    assert decision.verdict is DeviceVerdict.NOT_ARCHIVED


async def test_a_native_photo_row_is_never_proof(clean_db):
    await _row(tg_message_id=4, media_kind="photo", file_name="a.jpg", file_size=100)
    await _row(tg_message_id=5, file_name="unrelated.jpg", file_size=1)
    [decision] = await ReconcileService().evaluate([_entry()])
    assert decision.verdict is DeviceVerdict.NOT_ARCHIVED


async def test_a_chunk_part_row_is_never_a_candidate(clean_db):
    """A local file named like a chunk is not an archived original."""
    await _row(tg_message_id=6, artifact="chunk",
               file_name="movie.mp4.part001-of-003", file_size=1_950_000_000)
    [decision] = await ReconcileService().evaluate(
        [_entry(name="movie.mp4.part001-of-003", size=1_950_000_000)]
    )
    assert decision.verdict is DeviceVerdict.NOT_ARCHIVED


async def test_a_resolved_manifest_makes_the_chunked_original_findable(clean_db):
    await _row(tg_message_id=7, artifact="manifest",
               file_name="movie.mp4.manifest.json", file_size=800,
               chunked_original_name="movie.mp4", chunked_total_size=5_000_000_000,
               chunked_sha256="c" * 64)
    [decision] = await ReconcileService().evaluate(
        [_entry(name="movie.mp4", size=5_000_000_000)]
    )
    assert decision.verdict is DeviceVerdict.ARCHIVED
    assert decision.tier is MatchTier.NAME_SIZE
    assert decision.tg_message_id == 7


async def test_an_unresolved_manifest_leaves_the_original_invisible(clean_db):
    await _row(tg_message_id=8, artifact="manifest",
               file_name="movie.mp4.manifest.json", file_size=800)
    [decision] = await ReconcileService().evaluate(
        [_entry(name="movie.mp4", size=5_000_000_000)]
    )
    assert decision.verdict is DeviceVerdict.NOT_ARCHIVED


async def test_the_pipeline_overlay_is_read_from_photos(clean_db):
    await _row(tg_message_id=9, file_name="b.jpg", file_size=100)
    async with AsyncSessionLocal() as session:
        session.add(Photo(mega_path="/phone_bkp/b.jpg", status=PhotoStatus.SKIPPED))
        await session.commit()

    [decision] = await ReconcileService().evaluate([_entry(name="b.jpg")])
    assert decision.verdict is DeviceVerdict.NOT_ARCHIVED
    assert decision.reason == "unsupported_type"


async def test_an_unscanned_catalog_refuses_to_answer(clean_db):
    """Answering from an empty table would read as 'nothing you own is backed up'."""
    with pytest.raises(CatalogNeverScanned):
        await ReconcileService().evaluate([_entry()])
