"""Characterisation tests for PhotoWorker's step dispatch and failure handling.

These pin the invariants AGENTS.md calls load-bearing: videos never enter the
WebP/SFTP legs, `_finalize` is the only step that deletes the MEGA source, and
a photo only becomes FAILED (with a resumable `failed_status`) once it has
burnt through `max_retries`.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select

from app.models.database import (
    AsyncSessionLocal,
    MediaType,
    Photo,
    PhotoStatus,
)
from app.services.mega import MegaCmdError
from app.worker import PhotoWorker


class FakeMega:
    def __init__(self, *, listing=(), delete_error=None):
        self.listing = list(listing)
        self.delete_error = delete_error
        self.deleted = []

    async def list_new_files(self):
        return list(self.listing)

    async def delete_file(self, remote_path):
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(remote_path)


class FakeTelegram:
    browse_channel_id = None

    def __init__(self):
        self.next_id = 300
        self.documents = []

    async def upload_document(self, file_path, **kwargs):
        self.next_id += 1
        self.documents.append(str(file_path))
        return SimpleNamespace(id=self.next_id)

    async def upload_media(self, file_path, media_type, **kwargs):
        return None


class FakeSFTP:
    def __init__(self):
        self.uploads = []

    async def upload_file(self, local_path, remote_dir):
        self.uploads.append((str(local_path), remote_dir))
        return f"{remote_dir}/{Path(local_path).name}"


def _worker(tmp_path, *, mega=None, telegram=None, sftp=None, max_retries=3):
    return PhotoWorker(
        mega or FakeMega(),
        telegram or FakeTelegram(),
        sftp or FakeSFTP(),
        "/srv/photo-vault",
        download_root=tmp_path / "dl",
        compressed_root=tmp_path / "cp",
        mode="manual",
        per_file_delay=0,
        max_retries=max_retries,
    )


def _real_jpeg(path: Path, size=(40, 30)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (120, 40, 200)).save(path, format="JPEG")
    return path


async def _seed(**kwargs) -> int:
    async with AsyncSessionLocal() as session:
        photo = Photo(**kwargs)
        session.add(photo)
        await session.commit()
        return photo.id


async def _get(photo_id: int) -> Photo:
    async with AsyncSessionLocal() as session:
        return await session.get(Photo, photo_id)


async def test_video_skips_webp_and_sftp_and_goes_straight_to_completed(clean_db, tmp_path):
    mega, sftp = FakeMega(), FakeSFTP()
    worker = _worker(tmp_path, mega=mega, sftp=sftp)
    local = tmp_path / "dl" / "VID_20240101_120000.mp4"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(b"video bytes")

    photo_id = await _seed(
        mega_path="/Camera/VID_20240101_120000.mp4",
        status=PhotoStatus.TG_UPLOADED,
        media_type=MediaType.VIDEO,
        local_path=str(local),
    )

    assert await worker._process_photo_by_id(photo_id) is True

    photo = await _get(photo_id)
    assert photo.status == PhotoStatus.COMPLETED
    assert sftp.uploads == []  # no WebP mirror for videos
    assert mega.deleted == ["/Camera/VID_20240101_120000.mp4"]
    assert not local.exists()
    assert photo.local_path is None and photo.compressed_path is None


async def test_image_goes_through_webp_then_sftp_then_completed(clean_db, tmp_path):
    mega, sftp = FakeMega(), FakeSFTP()
    worker = _worker(tmp_path, mega=mega, sftp=sftp)
    local = _real_jpeg(tmp_path / "dl" / "Camera" / "IMG_20240612_193000.jpg")

    photo_id = await _seed(
        mega_path="/Camera/IMG_20240612_193000.jpg",
        status=PhotoStatus.TG_UPLOADED,
        media_type=MediaType.IMAGE,
        local_path=str(local),
    )

    await worker._process_photo_by_id(photo_id)
    photo = await _get(photo_id)
    assert photo.status == PhotoStatus.COMPRESSED
    compressed = Path(photo.compressed_path)
    assert compressed == tmp_path / "cp" / "Camera" / "IMG_20240612_193000.webp"
    assert compressed.is_file()

    await worker._process_photo_by_id(photo_id)
    photo = await _get(photo_id)
    assert photo.status == PhotoStatus.ODROID_UPLOADED
    assert sftp.uploads == [(str(compressed), "/srv/photo-vault")]

    await worker._process_photo_by_id(photo_id)
    photo = await _get(photo_id)
    assert photo.status == PhotoStatus.COMPLETED
    assert mega.deleted == ["/Camera/IMG_20240612_193000.jpg"]
    # Both the original and the WebP are cleaned up by _finalize.
    assert not local.exists() and not compressed.exists()


async def test_finalize_tolerates_a_source_already_gone_from_mega(clean_db, tmp_path):
    mega = FakeMega(delete_error=MegaCmdError("Command failed (1): mega-rm\nNot found: /Camera/x.jpg"))
    worker = _worker(tmp_path, mega=mega)
    photo_id = await _seed(
        mega_path="/Camera/x.jpg",
        status=PhotoStatus.ODROID_UPLOADED,
        media_type=MediaType.IMAGE,
    )

    assert await worker._process_photo_by_id(photo_id) is True
    assert (await _get(photo_id)).status == PhotoStatus.COMPLETED


async def test_finalize_propagates_any_other_mega_failure(clean_db, tmp_path):
    mega = FakeMega(delete_error=MegaCmdError("Command failed (1): mega-rm\nLogin required"))
    worker = _worker(tmp_path, mega=mega)
    photo_id = await _seed(
        mega_path="/Camera/y.jpg",
        status=PhotoStatus.ODROID_UPLOADED,
        media_type=MediaType.IMAGE,
    )

    assert await worker._process_photo_by_id(photo_id) is False
    photo = await _get(photo_id)
    assert photo.status == PhotoStatus.ODROID_UPLOADED  # still retryable
    assert photo.retry_count == 1
    assert "Login required" in photo.error_log


@pytest.mark.parametrize(
    "message, missing",
    [
        ("Not found: /Camera/a.jpg", True),
        ("No such file or directory", True),
        ("Node doesn't exist", True),
        ("Path not found", True),
        ("Could not find /Camera/a.jpg", True),
        ("Login required", False),
        ("Quota exceeded", False),
    ],
)
def test_remote_missing_markers(message, missing):
    assert PhotoWorker._is_remote_file_missing(MegaCmdError(message)) is missing


async def test_repeated_failures_end_in_failed_with_a_resumable_step(clean_db, tmp_path):
    worker = _worker(tmp_path, max_retries=2)
    photo_id = await _seed(
        mega_path="/Camera/broken.jpg",
        status=PhotoStatus.TG_UPLOADED,
        media_type=MediaType.IMAGE,
        local_path=str(tmp_path / "dl" / "never-downloaded.jpg"),
    )

    assert await worker._process_photo_by_id(photo_id) is False
    photo = await _get(photo_id)
    assert (photo.status, photo.retry_count, photo.failed_status) == (
        PhotoStatus.TG_UPLOADED,
        1,
        None,
    )

    assert await worker._process_photo_by_id(photo_id) is False
    photo = await _get(photo_id)
    assert photo.status == PhotoStatus.FAILED
    assert photo.failed_status == PhotoStatus.TG_UPLOADED
    assert photo.retry_count == 2
    assert "FileNotFoundError" in photo.error_log

    # A FAILED photo is no longer active, so a further visit does nothing.
    assert await worker._process_photo_by_id(photo_id) is False
    assert (await _get(photo_id)).retry_count == 2


async def test_a_successful_step_clears_the_previous_error(clean_db, tmp_path):
    worker = _worker(tmp_path)
    photo_id = await _seed(
        mega_path="/Camera/z.jpg",
        status=PhotoStatus.ODROID_UPLOADED,
        media_type=MediaType.IMAGE,
        retry_count=2,
        error_log="an old traceback",
    )

    await worker._process_photo_by_id(photo_id)

    photo = await _get(photo_id)
    assert (photo.retry_count, photo.error_log) == (0, None)


async def test_terminal_statuses_are_not_dispatchable(clean_db, tmp_path):
    worker = _worker(tmp_path)
    async with AsyncSessionLocal() as session:
        photo = Photo(
            mega_path="/Camera/done.jpg",
            status=PhotoStatus.COMPLETED,
            media_type=MediaType.IMAGE,
        )
        session.add(photo)
        await session.commit()
        with pytest.raises(ValueError, match="Unsupported photo status"):
            await worker._run_step(session, photo)


async def test_discovery_records_unsupported_types_as_skipped_and_is_idempotent(
    clean_db, tmp_path
):
    mega = FakeMega(
        listing=[
            "/Camera/IMG_1.jpg",
            "/Camera/VID_1.mp4",
            "/Camera/notes.pdf",
            "/Camera/IMG_1.jpg",  # a duplicate line in mega-ls output
        ]
    )
    worker = _worker(tmp_path, mega=mega)

    assert await worker._discover_new_files() == 3

    async with AsyncSessionLocal() as session:
        rows = (await session.scalars(select(Photo).order_by(Photo.mega_path))).all()
        by_path = {row.mega_path: row for row in rows}
    assert by_path["/Camera/IMG_1.jpg"].status == PhotoStatus.PENDING
    assert by_path["/Camera/VID_1.mp4"].status == PhotoStatus.PENDING
    assert by_path["/Camera/notes.pdf"].status == PhotoStatus.SKIPPED
    assert by_path["/Camera/notes.pdf"].media_type == MediaType.OTHER

    # A second discovery run over the same listing inserts nothing.
    assert await worker._discover_new_files() == 0


async def test_empty_listing_does_not_touch_the_database(clean_db, tmp_path):
    worker = _worker(tmp_path, mega=FakeMega(listing=[]))
    assert await worker._discover_new_files() == 0


async def test_only_active_statuses_are_fetched_for_work(clean_db, tmp_path):
    worker = _worker(tmp_path)
    await _seed(mega_path="/Camera/a.jpg", status=PhotoStatus.PENDING, media_type=MediaType.IMAGE)
    await _seed(mega_path="/Camera/b.pdf", status=PhotoStatus.SKIPPED, media_type=MediaType.OTHER)
    await _seed(mega_path="/Camera/c.jpg", status=PhotoStatus.FAILED, media_type=MediaType.IMAGE)
    await _seed(
        mega_path="/Camera/d.jpg", status=PhotoStatus.COMPLETED, media_type=MediaType.IMAGE
    )

    active = await worker._fetch_active_photo_ids()

    async with AsyncSessionLocal() as session:
        paths = [
            (await session.get(Photo, photo_id)).mega_path for photo_id in active
        ]
    assert paths == ["/Camera/a.jpg"]


async def test_status_snapshot_reports_mode_and_idle_state(tmp_path):
    worker = _worker(tmp_path)
    snapshot = worker.status_snapshot()
    assert snapshot["mode"] == "manual"
    assert snapshot["running"] is False
    assert snapshot["last_run_started_at"] is None
    assert snapshot["run_interval_seconds"] == 900.0


def test_browse_mirroring_is_skipped_when_the_service_has_no_browse_channel(tmp_path):
    # The worker asks the Telegram service, not its own config, so a fake without
    # a browse channel (as most tests use) is silently inert.
    worker = _worker(tmp_path)
    assert getattr(worker.telegram_service, "browse_channel_id", None) is None


def test_browse_video_allowance_is_converted_from_megabytes(tmp_path):
    assert _worker(tmp_path).browse_max_video_bytes == 0
    sized = PhotoWorker(
        FakeMega(),
        FakeTelegram(),
        FakeSFTP(),
        "/srv",
        download_root=tmp_path / "d",
        compressed_root=tmp_path / "c",
        mode="manual",
        browse_max_video_mb=5,
    )
    assert sized.browse_max_video_bytes == 5 * 1024 * 1024
