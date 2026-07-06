from __future__ import annotations

import asyncio
import json
import logging
import os
import traceback
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.models.database import (
    AsyncSessionLocal,
    ChunkStatus,
    MediaType,
    Photo,
    PhotoStatus,
    UploadChunk,
)
from app.services.chunking import (
    ChunkWindow,
    DEFAULT_CHUNK_SIZE,
    build_chunk_caption,
    build_manifest,
    build_manifest_caption,
    chunk_name,
    compute_hashes,
    manifest_name,
    plan_chunks,
)
from app.services.image import compress_to_webp
from app.services.media import detect_media_type
from app.services.mega import MegaCmdError, MegaService
from app.services.sftp import SFTPService
from app.services.telegram import TelegramService, extract_datetime_with_source, format_date_caption

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = (
    PhotoStatus.PENDING,
    PhotoStatus.DOWNLOADED,
    PhotoStatus.CHUNK_UPLOADING,
    PhotoStatus.TG_UPLOADED,
    PhotoStatus.COMPRESSED,
    PhotoStatus.ODROID_UPLOADED,
)

WORKER_MODES = ("interval", "manual")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class PhotoWorker:
    def __init__(
        self,
        mega_service: MegaService,
        telegram_service: TelegramService,
        sftp_service: SFTPService,
        odroid_remote_dir: str,
        *,
        download_root: str | Path = "./data/tmp",
        compressed_root: str | Path = "./data/compressed",
        mode: str = "interval",
        run_interval: float = 900.0,
        per_file_delay: float = 1.0,
        max_retries: int = 3,
        batch_size: int = 50,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_threshold: int = 1_950_000_000,
    ) -> None:
        if mode not in WORKER_MODES:
            raise ValueError(f"Unsupported worker mode: {mode!r} (expected one of {WORKER_MODES})")

        self.mega_service = mega_service
        self.telegram_service = telegram_service
        self.sftp_service = sftp_service
        self.odroid_remote_dir = odroid_remote_dir
        self.download_root = Path(download_root)
        self.compressed_root = Path(compressed_root)
        self.mode = mode
        self.run_interval = run_interval
        self.per_file_delay = per_file_delay
        self.max_retries = max_retries
        self.batch_size = batch_size
        self.chunk_size = chunk_size
        self.chunk_threshold = chunk_threshold

        self.running = False
        self.last_run_started_at: datetime | None = None
        self.last_run_finished_at: datetime | None = None
        self.next_run_at: datetime | None = None
        self.last_run_error: str | None = None
        self._wake = asyncio.Event()

        self.download_root.mkdir(parents=True, exist_ok=True)
        self.compressed_root.mkdir(parents=True, exist_ok=True)

    def trigger(self) -> None:
        self._wake.set()

    def status_snapshot(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "run_interval_seconds": self.run_interval,
            "running": self.running,
            "last_run_started_at": _iso(self.last_run_started_at),
            "last_run_finished_at": _iso(self.last_run_finished_at),
            "next_run_at": _iso(self.next_run_at),
            "last_run_error": self.last_run_error,
        }

    async def run_forever(self) -> None:
        while True:
            self._wake.clear()
            await self._run_guarded()

            if self.mode == "interval":
                self.next_run_at = _utcnow() + timedelta(seconds=self.run_interval)
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self.run_interval)
            else:
                self.next_run_at = None
                await self._wake.wait()

    async def _run_guarded(self) -> None:
        self.running = True
        self.last_run_started_at = _utcnow()
        try:
            await self.run_once()
            self.last_run_error = None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Worker run failed.")
            self.last_run_error = traceback.format_exc()
        finally:
            self.running = False
            self.last_run_finished_at = _utcnow()

    async def run_once(self) -> None:
        await self._discover_new_files()

        # Drain the pipeline: keep making passes while at least one photo advances.
        # A pass with zero progress means every remaining photo is erroring; leave
        # them for the next scheduled run instead of spinning.
        while True:
            photo_ids = await self._fetch_active_photo_ids()
            if not photo_ids:
                return

            progressed = False
            for photo_id in photo_ids:
                progressed = await self._process_photo_by_id(photo_id) or progressed
                if self.per_file_delay > 0:
                    await asyncio.sleep(self.per_file_delay)

            if not progressed:
                return

    async def _discover_new_files(self) -> int:
        remote_paths = sorted(set(await self.mega_service.list_new_files()))
        if not remote_paths:
            return 0

        async with AsyncSessionLocal() as session:
            existing_paths_result = await session.scalars(
                select(Photo.mega_path).where(Photo.mega_path.in_(remote_paths))
            )
            existing_paths = set(existing_paths_result.all())
            new_paths = [path for path in remote_paths if path not in existing_paths]

            if not new_paths:
                return 0

            session.add_all(self._new_photo(path) for path in new_paths)
            inserted = len(new_paths)

            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                inserted = 0
                for path in new_paths:
                    session.add(self._new_photo(path))
                    try:
                        await session.commit()
                        inserted += 1
                    except IntegrityError:
                        await session.rollback()

        if inserted:
            logger.info("Discovered %s new file(s) from MEGA.", inserted)
        return inserted

    @staticmethod
    def _new_photo(mega_path: str) -> Photo:
        media_type = detect_media_type(mega_path)
        status = PhotoStatus.SKIPPED if media_type == MediaType.OTHER else PhotoStatus.PENDING
        if status == PhotoStatus.SKIPPED:
            logger.info("Skipping unsupported file type: %s", mega_path)
        return Photo(mega_path=mega_path, status=status, media_type=media_type)

    async def _fetch_active_photo_ids(self) -> list[int]:
        async with AsyncSessionLocal() as session:
            result = await session.scalars(
                select(Photo.id)
                .where(Photo.status.in_(ACTIVE_STATUSES))
                .order_by(Photo.created_at, Photo.id)
                .limit(self.batch_size)
            )
            return list(result)

    async def _process_photo_by_id(self, photo_id: int) -> bool:
        async with AsyncSessionLocal() as session:
            photo = await session.get(Photo, photo_id)
            if photo is None or photo.status not in ACTIVE_STATUSES:
                return False

            try:
                await self._run_step(session, photo)
                photo.retry_count = 0
                photo.error_log = None
                stepped = True
            except asyncio.CancelledError:
                raise
            except Exception:
                stepped = False
                photo.retry_count += 1
                photo.error_log = traceback.format_exc()
                if photo.retry_count >= self.max_retries:
                    photo.failed_status = photo.status
                    photo.status = PhotoStatus.FAILED
                logger.exception("Failed processing photo id=%s", photo.id)

            await session.commit()
            return stepped

    async def _run_step(self, session, photo: Photo) -> None:
        if photo.status == PhotoStatus.PENDING:
            await self._handle_pending(photo)
            return

        if photo.status == PhotoStatus.DOWNLOADED:
            await self._handle_downloaded(session, photo)
            return

        if photo.status == PhotoStatus.CHUNK_UPLOADING:
            await self._handle_chunk_uploading(session, photo)
            return

        if photo.status == PhotoStatus.TG_UPLOADED:
            if photo.media_type == MediaType.VIDEO:
                # Videos are archived to Telegram only: no WebP/SFTP mirror.
                await self._finalize(photo)
            else:
                await self._handle_tg_uploaded(photo)
            return

        if photo.status == PhotoStatus.COMPRESSED:
            await self._handle_compressed(photo)
            return

        if photo.status == PhotoStatus.ODROID_UPLOADED:
            await self._handle_odroid_uploaded(photo)
            return

        raise ValueError(f"Unsupported photo status: {photo.status}")

    async def _handle_pending(self, photo: Photo) -> None:
        local_target = self._build_download_path(photo.mega_path)
        downloaded_path = await self.mega_service.download_file(photo.mega_path, local_target)

        photo.local_path = str(downloaded_path)
        photo.status = PhotoStatus.DOWNLOADED

    async def _handle_downloaded(self, session, photo: Photo) -> None:
        if not photo.local_path:
            raise ValueError(f"Photo id={photo.id} is DOWNLOADED but local_path is empty.")

        local_path = Path(photo.local_path)
        if not local_path.is_file():
            raise FileNotFoundError(f"Downloaded file not found: {local_path}")

        total_size = local_path.stat().st_size
        if total_size > self.chunk_threshold:
            await self._prepare_chunks(session, photo, local_path, total_size)
            return

        message = await self.telegram_service.upload_document(local_path)
        photo.tg_message_id = message.id

        media_message = await self.telegram_service.upload_media(local_path, photo.media_type)
        if media_message is not None:
            photo.tg_media_message_id = media_message.id

        photo.status = PhotoStatus.TG_UPLOADED

    async def _prepare_chunks(
        self, session, photo: Photo, local_path: Path, total_size: int
    ) -> None:
        logger.info(
            "File %s is %s bytes (> %s): splitting into chunks.",
            local_path.name,
            total_size,
            self.chunk_threshold,
        )
        whole_sha, chunk_hashes = await asyncio.to_thread(
            compute_hashes, local_path, self.chunk_size
        )
        plan = plan_chunks(total_size, self.chunk_size)
        if len(plan) != len(chunk_hashes):
            raise RuntimeError(
                f"Chunk plan/hash mismatch for {local_path}: {len(plan)} vs {len(chunk_hashes)}"
            )

        # Idempotency: a retried prepare replaces any rows from a partial attempt.
        await session.execute(delete(UploadChunk).where(UploadChunk.photo_id == photo.id))

        count = len(plan)
        for spec, sha in zip(plan, chunk_hashes):
            session.add(
                UploadChunk(
                    photo_id=photo.id,
                    part_index=spec["index"],
                    part_count=count,
                    offset=spec["offset"],
                    size=spec["size"],
                    sha256=sha,
                    filename=chunk_name(local_path.name, spec["index"], count),
                )
            )

        photo.is_chunked = True
        photo.sha256 = whole_sha
        photo.total_size = total_size
        photo.status = PhotoStatus.CHUNK_UPLOADING

    async def _handle_chunk_uploading(self, session, photo: Photo) -> None:
        if not photo.local_path:
            raise ValueError(f"Photo id={photo.id} is CHUNK_UPLOADING but local_path is empty.")

        local_path = Path(photo.local_path)
        if not local_path.is_file():
            raise FileNotFoundError(f"Source file for chunk upload not found: {local_path}")

        capture_datetime, capture_source = await extract_datetime_with_source(local_path)
        date_caption = format_date_caption(capture_datetime)

        chunk = await session.scalar(
            select(UploadChunk)
            .where(UploadChunk.photo_id == photo.id, UploadChunk.status == ChunkStatus.PENDING)
            .order_by(UploadChunk.part_index)
            .limit(1)
        )

        if chunk is None:
            # All chunks are up: the manifest is the commit marker for the set.
            await self._upload_manifest(
                session, photo, local_path, date_caption, capture_datetime, capture_source
            )
            return

        if chunk.tg_message_id is None:
            # Close the crash window between send and commit: reuse an
            # already-uploaded chunk instead of duplicating it.
            existing = await self.telegram_service.find_document_by_name(chunk.filename)
            if existing is not None:
                logger.info("Chunk %s already in channel; reusing message %s.", chunk.filename, existing.id)
                chunk.tg_message_id = existing.id
                chunk.status = ChunkStatus.UPLOADED
                return

        caption = build_chunk_caption(
            date_caption,
            index=chunk.part_index,
            count=chunk.part_count,
            original_filename=local_path.name,
            total_size=photo.total_size or 0,
            sha256=photo.sha256 or "",
        )
        window = ChunkWindow(local_path, chunk.offset, chunk.size, chunk.filename)
        try:
            message = await self.telegram_service.upload_file_object(window, caption)
        finally:
            window.close()

        chunk.tg_message_id = message.id
        chunk.status = ChunkStatus.UPLOADED
        logger.info("Uploaded chunk %s (%s/%s).", chunk.filename, chunk.part_index, chunk.part_count)

    async def _upload_manifest(
        self,
        session,
        photo: Photo,
        local_path: Path,
        date_caption: str,
        capture_datetime: datetime,
        capture_source: str,
    ) -> None:
        chunks = (
            await session.scalars(
                select(UploadChunk)
                .where(UploadChunk.photo_id == photo.id)
                .order_by(UploadChunk.part_index)
            )
        ).all()
        if not chunks:
            raise RuntimeError(f"Photo id={photo.id} is CHUNK_UPLOADING but has no chunk rows.")

        manifest = build_manifest(
            original_filename=local_path.name,
            total_size=photo.total_size or local_path.stat().st_size,
            sha256=photo.sha256 or "",
            chunk_size=self.chunk_size,
            chunks=[
                {
                    "index": chunk.part_index,
                    "filename": chunk.filename,
                    "offset": chunk.offset,
                    "size": chunk.size,
                    "sha256": chunk.sha256,
                    "tg_message_id": chunk.tg_message_id,
                }
                for chunk in chunks
            ],
            mega_path=photo.mega_path,
            mtime_utc=datetime.fromtimestamp(local_path.stat().st_mtime, tz=timezone.utc),
            capture_datetime=capture_datetime,
            capture_datetime_source=capture_source,
        )

        message = await self.telegram_service.upload_bytes(
            json.dumps(manifest, indent=2).encode("utf-8"),
            file_name=manifest_name(local_path.name),
            caption=build_manifest_caption(date_caption, original_filename=local_path.name),
        )
        photo.manifest_tg_message_id = message.id
        photo.tg_message_id = message.id
        photo.status = PhotoStatus.TG_UPLOADED
        logger.info("Uploaded manifest for %s (%s chunks).", local_path.name, len(chunks))

    async def _handle_tg_uploaded(self, photo: Photo) -> None:
        if not photo.local_path:
            raise ValueError(f"Photo id={photo.id} is TG_UPLOADED but local_path is empty.")

        local_path = Path(photo.local_path)
        if not local_path.is_file():
            raise FileNotFoundError(f"Input file for compression not found: {local_path}")

        compressed_path = self._build_compressed_path(local_path)
        await compress_to_webp(local_path, compressed_path)

        photo.compressed_path = str(compressed_path)
        photo.status = PhotoStatus.COMPRESSED

    async def _handle_compressed(self, photo: Photo) -> None:
        if not photo.compressed_path:
            raise ValueError(f"Photo id={photo.id} is COMPRESSED but compressed_path is empty.")

        compressed_path = Path(photo.compressed_path)
        if not compressed_path.is_file():
            raise FileNotFoundError(f"Compressed file not found: {compressed_path}")

        await self.sftp_service.upload_file(compressed_path, self.odroid_remote_dir)
        photo.status = PhotoStatus.ODROID_UPLOADED

    async def _handle_odroid_uploaded(self, photo: Photo) -> None:
        await self._finalize(photo)

    async def _finalize(self, photo: Photo) -> None:
        try:
            await self.mega_service.delete_file(photo.mega_path)
        except MegaCmdError as exc:
            if self._is_remote_file_missing(exc):
                logger.warning(
                    "Remote file already missing on MEGA for photo id=%s (%s). Continuing.",
                    photo.id,
                    photo.mega_path,
                )
            else:
                raise

        if photo.local_path:
            self._remove_local_file(photo.local_path)

        if photo.compressed_path:
            self._remove_local_file(photo.compressed_path)

        photo.local_path = None
        photo.compressed_path = None
        photo.status = PhotoStatus.COMPLETED

    def _build_download_path(self, mega_path: str) -> Path:
        relative_path = PurePosixPath(mega_path.lstrip("/"))
        if not relative_path.name:
            raise ValueError(f"Invalid mega_path: {mega_path}")

        local_target = self.download_root.joinpath(*relative_path.parts)
        local_target.parent.mkdir(parents=True, exist_ok=True)
        return local_target

    def _build_compressed_path(self, local_path: Path) -> Path:
        try:
            relative_path = local_path.relative_to(self.download_root)
        except ValueError:
            relative_path = Path(local_path.name)

        target = self.compressed_root / relative_path
        target = target.with_suffix(".webp")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    @staticmethod
    def _remove_local_file(path: str | Path) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            return

    @staticmethod
    def _is_remote_file_missing(exc: MegaCmdError) -> bool:
        error_text = str(exc).lower()
        markers = (
            "not found",
            "no such file",
            "doesn't exist",
            "does not exist",
            "path not found",
            "could not find",
        )
        return any(marker in error_text for marker in markers)
