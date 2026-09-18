"""Characterisation tests for the decisions the tidy makes before it touches Telegram.

Each of these is a rule that decides whether a channel message is touched at
all, what caption it gets, and whether anything is downloaded. The functional
end-to-end flow lives in tests/test_recovery_flow.py; this file pins the rules
in isolation, including the edges that flow never reaches.
"""
from datetime import datetime

import pytest

from app.models.database import RecoveryItem, RecoveryStatus
from app.services.recovery import (
    CAPTION_LIMIT,
    DEFAULT_BATCH_MAX_DOWNLOAD_BYTES,
    DEFAULT_BATCH_SIZE,
    DEFAULT_MIN_FREE_BYTES,
    MEDIA_KINDS,
    RecoveryService,
    _caption_from_metadata,
    _merge_caption,
    _safe_name,
)
from app.services.telegram import TelegramService

HASHTAGS = "#2024 #06_2024 #2024_06_12"


def _service(tmp_path, **kwargs):
    telegram = TelegramService(object(), -100111, upload_delay_seconds=0)
    return RecoveryService(telegram, download_root=tmp_path / "recovery", **kwargs)


def _item(**kwargs) -> RecoveryItem:
    return RecoveryItem(media_kind=kwargs.pop("media_kind", "document"), **kwargs)


# -- caption merging --------------------------------------------------------


def test_an_empty_caption_becomes_just_the_hashtags():
    assert _merge_caption(None, HASHTAGS) == HASHTAGS
    assert _merge_caption("", HASHTAGS) == HASHTAGS
    assert _merge_caption("   ", HASHTAGS) == HASHTAGS


def test_existing_free_text_is_preserved_above_the_hashtags():
    assert _merge_caption("Holiday", HASHTAGS) == f"Holiday\n\n{HASHTAGS}"
    assert _merge_caption("  Holiday  ", HASHTAGS) == f"Holiday\n\n{HASHTAGS}"


def test_an_already_tidy_caption_asks_for_no_edit():
    # None is the "nothing to do" signal, not an error.
    assert _merge_caption("#2020 #01_2020 #2020_01_02", HASHTAGS) is None
    assert _merge_caption(f"Beach trip\n\n{HASHTAGS}", HASHTAGS) is None


def test_a_partial_hashtag_set_is_not_treated_as_tidy():
    assert _merge_caption("#2024", HASHTAGS) == f"#2024\n\n{HASHTAGS}"
    assert _merge_caption("#2024 #06_2024", HASHTAGS) == f"#2024 #06_2024\n\n{HASHTAGS}"


def test_a_long_caption_is_trimmed_to_fit_telegrams_limit():
    existing = "x" * CAPTION_LIMIT

    merged = _merge_caption(existing, HASHTAGS)

    assert len(merged) <= CAPTION_LIMIT
    assert merged.endswith(f"\n\n{HASHTAGS}")
    assert merged.startswith("xxxx")


def test_hashtags_win_outright_when_there_is_no_room_for_the_original():
    huge_tags = "#" * CAPTION_LIMIT
    assert _merge_caption("some text", huge_tags) == huge_tags


# -- deriving the date ------------------------------------------------------


def test_a_date_in_the_filename_beats_the_post_date():
    item = _item(
        file_name="IMG_20240612_193000.jpg",
        message_date=datetime(2022, 3, 5, 10, 0, 0),
    )
    assert _caption_from_metadata(item) == HASHTAGS


def test_the_post_date_is_used_when_the_filename_has_none():
    item = _item(file_name="IMG_1234.jpg", message_date=datetime(2022, 3, 5, 10, 0, 0))
    assert _caption_from_metadata(item) == "#2022 #03_2022 #2022_03_05"


def test_no_filename_and_no_post_date_means_no_caption():
    assert _caption_from_metadata(_item(file_name=None, message_date=None)) is None


# -- what the tidy refuses to touch -----------------------------------------


@pytest.mark.parametrize(
    "file_name",
    ["movie.mp4.part001-of-012", "movie.mp4.part0001-of-1000", "movie.mp4.manifest.json"],
)
def test_our_own_chunk_artifacts_are_never_tidied(file_name):
    assert RecoveryService._is_vault_artifact(file_name, None) is True


def test_vault_artifacts_are_also_recognised_by_their_caption_tags():
    assert RecoveryService._is_vault_artifact(None, "#2024 #chunked #part001_of_012") is True
    assert RecoveryService._is_vault_artifact(None, "#2024 #manifest") is True


@pytest.mark.parametrize("file_name", ["IMG_1.jpg", "holiday.part.of.the.trip.jpg", None])
def test_ordinary_media_is_not_mistaken_for_a_vault_artifact(file_name):
    assert RecoveryService._is_vault_artifact(file_name, "Holiday") is False


def test_a_message_is_tidy_only_with_the_full_hashtag_scheme():
    class Message:
        def __init__(self, caption):
            self.caption = caption

    assert RecoveryService._is_tidy(Message("#2024 #06_2024 #2024_06_12")) is True
    assert RecoveryService._is_tidy(Message("holiday #2024 #06_2024 #2024_06_12")) is True
    assert RecoveryService._is_tidy(Message("#2024 #06_2024")) is False
    assert RecoveryService._is_tidy(Message(None)) is False


# -- what gets downloaded ---------------------------------------------------


@pytest.mark.parametrize(
    "kind, file_name, expected",
    [
        # Only image documents can carry readable EXIF...
        ("document", "IMG_1234.jpg", True),
        ("document", "scan.HEIC", True),
        # ...and only when the filename does not already answer the question.
        ("document", "IMG_20240612_193000.jpg", False),
        ("document", "movie.mp4", False),
        ("document", "notes.pdf", False),
        ("document", "", False),
        # Telegram strips EXIF from native photos, and videos have none to read.
        ("photo", None, False),
        ("video", "clip.mov", False),
        ("animation", "loop.gif", False),
    ],
)
def test_only_dateless_image_documents_are_worth_downloading(kind, file_name, expected):
    assert RecoveryService._needs_exif(_item(media_kind=kind, file_name=file_name)) is expected


def test_the_free_space_floor_blocks_a_download_that_would_breach_it(tmp_path):
    service = _service(tmp_path, min_free_bytes=10**18)
    assert service._space_for(1) is False
    assert service._space_for(None) is False

    roomy = _service(tmp_path, min_free_bytes=0)
    assert roomy._space_for(1) is True


def test_download_filenames_are_sanitised(tmp_path):
    assert _safe_name("../../etc/passwd") == "passwd"
    assert _safe_name("holiday photo (1).jpg") == "holiday_photo__1_.jpg"
    assert _safe_name("/") == "file"


# -- service configuration --------------------------------------------------


def test_unknown_media_kinds_are_dropped_from_the_configured_set(tmp_path):
    service = _service(tmp_path, kinds=("photo", "sticker", "document"))
    assert service.kinds == ("photo", "document")
    assert set(MEDIA_KINDS) == {"photo", "video", "document", "animation"}


def test_defaults_match_the_documented_values(tmp_path):
    service = _service(tmp_path)
    assert service.batch_size == DEFAULT_BATCH_SIZE == 300
    assert service.min_free_bytes == DEFAULT_MIN_FREE_BYTES == 10 * 1024**3
    assert (
        service.batch_max_download_bytes == DEFAULT_BATCH_MAX_DOWNLOAD_BYTES == 5 * 1024**3
    )


def test_the_download_root_is_created_eagerly(tmp_path):
    root = tmp_path / "recovery"
    assert not root.exists()
    _service(tmp_path)
    assert root.is_dir()


def test_status_snapshot_reports_the_disk_headroom(tmp_path):
    idle = _service(tmp_path, min_free_bytes=0).status_snapshot()
    assert idle["running"] is False
    assert idle["activity"] is None
    assert idle["disk"]["download_root"] == str(tmp_path / "recovery")
    assert idle["disk"]["below_floor"] is False
    assert idle["disk"]["free_bytes"] > 0

    blocked = _service(tmp_path, min_free_bytes=10**18).status_snapshot()
    assert blocked["disk"]["below_floor"] is True


def test_legacy_statuses_still_exist_but_are_no_longer_produced():
    # /api/status reports counts for every enum member, and old rows may still
    # carry these; nothing in the in-place tidy writes them any more.
    assert {status.value for status in RecoveryStatus} >= {
        "DOWNLOADED",
        "REUPLOADED",
        "DUPLICATE",
    }
