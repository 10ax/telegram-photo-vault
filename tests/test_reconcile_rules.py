"""The verdict table, one case per row. No database, no fakes, no network.

decide() is pure on purpose: every rule that decides whether a photo may be
deleted is exercised here, in isolation, at the cost of nothing.
"""
from datetime import datetime, timezone

import pytest

from app.models.database import DeviceVerdict, MatchTier, PhotoStatus
from app.services.reconcile import Candidate, decide

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
CATALOG_NEWEST = datetime(2026, 7, 20, tzinfo=timezone.utc)

PXL = Candidate(
    tg_message_id=48211,
    channel_id=-1002637897512,
    file_name="PXL_20260713_115033830.jpg",
    file_size=3_412_887,
    sha256=None,
)


def _decide(**overrides):
    kwargs = dict(
        name="PXL_20260713_115033830.jpg",
        size=3_412_887,
        sha256=None,
        mtime=NOW,
        pipeline_status=None,
        candidates=[PXL],
        catalog_newest=CATALOG_NEWEST,
    )
    kwargs.update(overrides)
    return decide(**kwargs)


def test_exact_name_and_size_is_archived_at_name_size_tier():
    result = _decide()
    assert result.verdict is DeviceVerdict.ARCHIVED
    assert result.tier is MatchTier.NAME_SIZE
    assert result.tg_message_id == 48211


def test_a_hash_match_is_archived_at_hash_tier():
    hashed = Candidate(1, -1, "movie.mp4", 5_000_000_000, "b" * 64)
    result = _decide(name="movie.mp4", size=5_000_000_000, sha256="b" * 64, candidates=[hashed])
    assert result.verdict is DeviceVerdict.ARCHIVED
    assert result.tier is MatchTier.HASH


def test_a_hash_match_wins_even_when_the_size_disagrees():
    """Content proof outranks metadata: a size that disagrees is the metadata's problem."""
    hashed = Candidate(1, -1, "movie.mp4", 999, "b" * 64)
    result = _decide(name="movie.mp4", size=5_000_000_000, sha256="b" * 64, candidates=[hashed])
    assert result.verdict is DeviceVerdict.ARCHIVED
    assert result.tier is MatchTier.HASH


def test_same_name_different_size_is_ambiguous():
    result = _decide(size=99)
    assert result.verdict is DeviceVerdict.AMBIGUOUS
    assert result.reason == "size_mismatch"
    assert result.tg_message_id == 48211, "the candidate to verify must be named"


def test_a_case_only_match_is_ambiguous_never_archived():
    """/sdcard is case-insensitive and Telegram is not: report it, do not trust it."""
    result = _decide(name="pxl_20260713_115033830.JPG")
    assert result.verdict is DeviceVerdict.AMBIGUOUS
    assert result.reason == "case_only_match"


def test_no_candidate_is_not_archived():
    result = _decide(candidates=[])
    assert result.verdict is DeviceVerdict.NOT_ARCHIVED


@pytest.mark.parametrize(
    "status",
    [
        PhotoStatus.PENDING,
        PhotoStatus.DOWNLOADED,
        PhotoStatus.CHUNK_UPLOADING,
        PhotoStatus.TG_UPLOADED,
        PhotoStatus.COMPRESSED,
        PhotoStatus.ODROID_UPLOADED,
    ],
)
def test_a_file_still_in_the_pipeline_is_in_flight(status):
    result = _decide(pipeline_status=status)
    assert result.verdict is DeviceVerdict.IN_FLIGHT
    assert result.reason == "pipeline_in_progress"


def test_a_failed_file_is_not_archived_and_says_why():
    result = _decide(pipeline_status=PhotoStatus.FAILED)
    assert result.verdict is DeviceVerdict.NOT_ARCHIVED
    assert result.reason == "pipeline_failed"


def test_a_skipped_file_is_not_archived_even_though_photos_has_a_row():
    """The 93 SKIPPED rows exist in photos and were never archived at all."""
    result = _decide(pipeline_status=PhotoStatus.SKIPPED)
    assert result.verdict is DeviceVerdict.NOT_ARCHIVED
    assert result.reason == "unsupported_type"


def test_completed_corroborates_but_does_not_decide():
    result = _decide(pipeline_status=PhotoStatus.COMPLETED)
    assert result.verdict is DeviceVerdict.ARCHIVED
    assert result.tier is MatchTier.NAME_SIZE


def test_completed_without_a_catalog_row_is_ambiguous_not_archived():
    """The 62 files in the measurements: the honest answer points at a rescan."""
    result = _decide(pipeline_status=PhotoStatus.COMPLETED, candidates=[])
    assert result.verdict is DeviceVerdict.AMBIGUOUS
    assert result.reason == "completed_but_absent_from_catalog"


def test_a_file_newer_than_the_catalog_cannot_be_archived_by_name_and_size():
    result = _decide(mtime=datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert result.verdict is DeviceVerdict.IN_FLIGHT
    assert result.reason == "catalog_older_than_file"


def test_a_missing_mtime_fails_closed():
    """The client supplies mtime and can get it wrong. Absent means unsafe."""
    result = _decide(mtime=None)
    assert result.verdict is DeviceVerdict.IN_FLIGHT
    assert result.reason == "catalog_older_than_file"


def test_freshness_does_not_demote_a_hash_match():
    """A content hash is proof; dates cannot undermine it."""
    hashed = Candidate(1, -1, "movie.mp4", 5_000_000_000, "b" * 64)
    result = _decide(
        name="movie.mp4",
        size=5_000_000_000,
        sha256="b" * 64,
        candidates=[hashed],
        mtime=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    assert result.verdict is DeviceVerdict.ARCHIVED
    assert result.tier is MatchTier.HASH


def test_a_zero_byte_file_is_never_archived_by_name_and_size():
    """Every empty file shares a size, so a size match proves nothing about content."""
    empty = Candidate(1, -1, "empty.jpg", 0, None)
    result = _decide(name="empty.jpg", size=0, candidates=[empty])
    assert result.verdict is DeviceVerdict.AMBIGUOUS
    assert result.reason == "zero_byte_file"


def test_a_contradicting_hash_is_ambiguous_never_archived():
    """The server holds proof the bytes differ; a same name+size match must not override it."""
    hashed = Candidate(1, -1, "movie.mp4", 5_000_000_000, "b" * 64)
    result = _decide(
        name="movie.mp4", size=5_000_000_000, sha256="a" * 64, candidates=[hashed]
    )
    assert result.verdict is DeviceVerdict.AMBIGUOUS
    assert result.reason == "hash_mismatch"
    assert result.tg_message_id == 1
    assert result.channel_id == -1


def test_an_unknown_catalog_freshness_fails_closed():
    """No newest-message date on record is exactly as unsafe as a stale one."""
    result = _decide(catalog_newest=None)
    assert result.verdict is DeviceVerdict.IN_FLIGHT
    assert result.reason == "catalog_older_than_file"
