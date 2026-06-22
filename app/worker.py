from __future__ import annotations

import asyncio
import logging
import os
import traceback
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.database import AsyncSessionLocal, Photo, PhotoStatus
from app.services.image import compress_to_webp
from app.services.mega import MegaCmdError, MegaService
from app.services.sftp import SFTPService
from app.services.telegram import TelegramService

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = (
    PhotoStatus.PENDING,
    PhotoStatus.DOWNLOADED,
    PhotoStatus.TG_UPLOADED,
    PhotoStatus.COMPRESSED,
    PhotoStatus.ODROID_UPLOADED,
)


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
        poll_delay: float = 5.0,
        per_file_delay: float = 1.0,
        max_retries: int = 3,
        batch_size: int = 50,
    ) -> None:
        self.mega_service = mega_service
        self.telegram_service = telegram_service
        self.sftp_service = sftp_service
        self.odroid_remote_dir = odroid_remote_dir
        self.download_root = Path(download_root)
        self.compressed_root = Path(compressed_root)
        self.poll_delay = poll_delay
        self.per_file_delay = per_file_delay
        self.max_retries = max_retries
        self.batch_size = batch_size

        self.download_root.mkdir(parents=True, exist_ok=True)
        self.compressed_root.mkdir(parents=True, exist_ok=True)

    async def run_forever(self) -> None:
        while True:
            try:
                await self._discover_new_files()

                photo_ids = await self._fetch_active_photo_ids()
                if not photo_ids:
                    await asyncio.sleep(self.poll_delay)
                    continue

                for photo_id in photo_ids:
                    await self._process_photo_by_id(photo_id)
                    await asyncio.sleep(self.per_file_delay)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Worker loop failed; retrying after delay.")
                await asyncio.sleep(self.poll_delay)

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

            session.add_all(Photo(mega_path=path, status=PhotoStatus.PENDING) for path in new_paths)
            inserted = len(new_paths)

            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                inserted = 0
                for path in new_paths:
                    session.add(Photo(mega_path=path, status=PhotoStatus.PENDING))
                    try:
                        await session.commit()
                        inserted += 1
                    except IntegrityError:
                        await session.rollback()

        if inserted:
            logger.info("Discovered %s new photo(s) from MEGA.", inserted)
        return inserted

    async def _fetch_active_photo_ids(self) -> list[int]:
        async with AsyncSessionLocal() as session:
            result = await session.scalars(
                select(Photo.id)
                .where(Photo.status.in_(ACTIVE_STATUSES))
                .order_by(Photo.created_at, Photo.id)
                .limit(self.batch_size)
            )
            return list(result)

    async def _process_photo_by_id(self, photo_id: int) -> None:
        async with AsyncSessionLocal() as session:
            photo = await session.get(Photo, photo_id)
            if photo is None or photo.status not in ACTIVE_STATUSES:
                return

            try:
                await self._run_step(photo)
                photo.retry_count = 0
                photo.error_log = None
            except asyncio.CancelledError:
                raise
            except Exception:
                photo.retry_count += 1
                photo.error_log = traceback.format_exc()
                if photo.retry_count >= self.max_retries:
                    photo.status = PhotoStatus.FAILED
                logger.exception("Failed processing photo id=%s", photo.id)

            await session.commit()

    async def _run_step(self, photo: Photo) -> None:
        if photo.status == PhotoStatus.PENDING:
            await self._handle_pending(photo)
            return

        if photo.status == PhotoStatus.DOWNLOADED:
            await self._handle_downloaded(photo)
            return

        if photo.status == PhotoStatus.TG_UPLOADED:
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

    async def _handle_downloaded(self, photo: Photo) -> None:
        if not photo.local_path:
            raise ValueError(f"Photo id={photo.id} is DOWNLOADED but local_path is empty.")

        local_path = Path(photo.local_path)
        if not local_path.is_file():
            raise FileNotFoundError(f"Downloaded file not found: {local_path}")

        message = await self.telegram_service.upload_document(local_path)
        photo.tg_message_id = message.id
        photo.status = PhotoStatus.TG_UPLOADED

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
