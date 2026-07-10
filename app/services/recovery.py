from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import traceback
from pathlib import Path

from pyrogram.errors import FloodWait
from pyrogram.types import Message
from sqlalchemy import select

from app.models.database import AsyncSessionLocal, RecoveryItem, RecoveryStatus
from app.services.telegram import (
    TelegramService,
    _parse_filename_datetime,
    build_caption,
    format_date_caption,
)

logger = logging.getLogger(__name__)

MEDIA_KINDS = ("photo", "video", "document", "animation")

# Kinds that render as a thumbnail gallery / play inline in the browse channel.
# Documents are intentionally excluded (they show as files, not a photo grid);
# they stay date-searchable in the main channel via the tidy.
BROWSE_MEDIA_KINDS = ("photo", "video", "animation")

# Messages this project created for chunked uploads; never "tidy" those.
CHUNK_PART_RE = re.compile(r"\.part\d+-of-\d+$")
MANIFEST_SUFFIX = ".manifest.json"

# A message is already tidy when its caption carries the full hashtag scheme
# (#YYYY #MM_YYYY #YYYY_MM_DD) — regardless of media type.
TIDY_CAPTION_RE = re.compile(r"#\d{4}\s+#\d{2}_\d{4}\s+#\d{4}_\d{2}_\d{2}")

# Only image *documents* keep usable EXIF: Telegram strips it from `photo`
# messages, and the datetime extractor cannot read video/animation metadata.
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp", ".bmp", ".gif"}

# Telegram hard limit on caption length.
CAPTION_LIMIT = 1024

# Balanced defaults (overridable via env / constructor).
DEFAULT_BATCH_SIZE = 300
DEFAULT_MIN_FREE_BYTES = 10 * 1024**3  # keep at least 10 GiB free on the download fs
DEFAULT_BATCH_MAX_DOWNLOAD_BYTES = 5 * 1024**3  # cap bytes downloaded per batch


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.\-]", "_", Path(name).name) or "file"


def _caption_from_metadata(item: RecoveryItem) -> str | None:
    """Derive the date caption without downloading: filename date, then post date."""
    if item.file_name:
        parsed = _parse_filename_datetime(item.file_name)
        if parsed is not None:
            return format_date_caption(parsed)
    if item.message_date is not None:
        return format_date_caption(item.message_date)
    return None


def _merge_caption(existing: str | None, hashtags: str) -> str | None:
    """Combine any existing caption with the date hashtags.

    Returns None when the message already carries the date tags (no edit needed).
    Existing free-text is preserved and the tags are appended on a new line,
    trimming the original only if the result would exceed Telegram's limit.
    """
    existing = (existing or "").strip()
    if not existing:
        return hashtags
    if TIDY_CAPTION_RE.search(existing):
        return None
    merged = f"{existing}\n\n{hashtags}"
    if len(merged) > CAPTION_LIMIT:
        keep = CAPTION_LIMIT - len(hashtags) - 2
        merged = f"{existing[:keep].rstrip()}\n\n{hashtags}" if keep > 0 else hashtags
    return merged


class RecoveryBusyError(RuntimeError):
    pass


class RecoveryService:
    """Adds date-hashtag captions to existing channel media, gradually.

    Strategy is *hybrid*: for the vast majority of items the capture date is
    read from the filename (or the message post date) with no download, and the
    caption is edited in place. Only image documents whose filename lacks a date
    are downloaded so their EXIF can be read; those downloads are batched and
    guarded by a free-space floor, and the temp file is deleted immediately.

    Work is processed in bounded batches so the tidy-up can run a little at a
    time. The per-message state lives in the recovery_items table, so scans and
    runs are fully resumable.
    """

    def __init__(
        self,
        telegram_service: TelegramService,
        *,
        download_root: str | Path = "/data/recovery",
        delay_seconds: float = 5.0,
        max_retries: int = 3,
        kinds: tuple[str, ...] = MEDIA_KINDS,
        delete_old: bool = True,
        batch_size: int = DEFAULT_BATCH_SIZE,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        batch_max_download_bytes: int = DEFAULT_BATCH_MAX_DOWNLOAD_BYTES,
    ) -> None:
        self.telegram = telegram_service
        self.download_root = Path(download_root)
        self.delay_seconds = delay_seconds
        self.max_retries = max_retries
        self.kinds = tuple(kind for kind in kinds if kind in MEDIA_KINDS)
        # Retained for config compatibility; the in-place tidy edits captions and
        # never deletes originals, so this is currently informational only.
        self.delete_old = delete_old
        self.batch_size = batch_size
        self.min_free_bytes = min_free_bytes
        self.batch_max_download_bytes = batch_max_download_bytes

        self.activity: str | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._batch: dict[str, object] | None = None
        self._last_batch: dict[str, object] | None = None

        self.download_root.mkdir(parents=True, exist_ok=True)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status_snapshot(self) -> dict[str, object]:
        try:
            free = shutil.disk_usage(self.download_root).free
        except OSError:
            free = None
        return {
            "running": self.running,
            "activity": self.activity if self.running else None,
            "last_error": self.last_error,
            "batch": self._batch if self.running else self._last_batch,
            "batch_size": self.batch_size,
            "disk": {
                "download_root": str(self.download_root),
                "free_bytes": free,
                "min_free_bytes": self.min_free_bytes,
                "below_floor": free is not None and free < self.min_free_bytes,
            },
        }

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def start_scan(self) -> None:
        self._start(self._scan(), "scanning channel history")

    def start_run(
        self,
        dry_run: bool,
        *,
        limit: int | None = None,
        max_download_bytes: int | None = None,
    ) -> None:
        label = "planning (dry run)" if dry_run else "tidying"
        self._start(self._process(dry_run, limit, max_download_bytes), label)

    def start_backfill(
        self, *, limit: int | None = None, max_video_bytes: int | None = None
    ) -> None:
        self._start(self._backfill(limit, max_video_bytes), "backfilling browse channel")

    def _start(self, coroutine, activity: str) -> None:
        if self.running:
            coroutine.close()
            raise RecoveryBusyError("A recovery task is already running.")
        self.activity = activity
        self._task = asyncio.create_task(self._guarded(coroutine), name="recovery")

    async def _guarded(self, coroutine) -> None:
        try:
            await coroutine
            self.last_error = None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Recovery task failed.")
            self.last_error = traceback.format_exc()
        finally:
            self.activity = None

    # -- scan ---------------------------------------------------------------

    async def _scan(self) -> None:
        client = self.telegram.client
        channel_id = self.telegram.channel_id
        scanned = ingested = 0

        async for message in client.get_chat_history(channel_id):
            scanned += 1
            info = self._media_info(message)
            if info is None:
                continue

            kind, file_name, file_size = info
            if self._is_vault_artifact(file_name, message.caption):
                continue

            status = (
                RecoveryStatus.SKIPPED if self._is_tidy(message) else RecoveryStatus.SCANNED
            )
            if await self._upsert_item(message, kind, file_name, file_size, status):
                ingested += 1

            if scanned % 500 == 0:
                logger.info("Recovery scan: %s messages seen, %s ingested.", scanned, ingested)

        logger.info("Recovery scan finished: %s messages, %s new items.", scanned, ingested)

    def _media_info(self, message: Message) -> tuple[str, str | None, int | None] | None:
        if message.photo is not None and "photo" in self.kinds:
            return "photo", None, message.photo.file_size
        if message.video is not None and "video" in self.kinds:
            return "video", message.video.file_name, message.video.file_size
        if message.animation is not None and "animation" in self.kinds:
            return "animation", message.animation.file_name, message.animation.file_size
        if message.document is not None and "document" in self.kinds:
            return "document", message.document.file_name, message.document.file_size
        return None

    @staticmethod
    def _is_vault_artifact(file_name: str | None, caption: str | None) -> bool:
        if file_name:
            stem = file_name.strip()
            if CHUNK_PART_RE.search(stem) or stem.endswith(MANIFEST_SUFFIX):
                return True
        if caption and ("#chunked" in caption or "#manifest" in caption):
            return True
        return False

    @staticmethod
    def _is_tidy(message: Message) -> bool:
        caption = getattr(message, "caption", None) or ""
        return TIDY_CAPTION_RE.search(caption) is not None

    async def _upsert_item(
        self,
        message: Message,
        kind: str,
        file_name: str | None,
        file_size: int | None,
        status: RecoveryStatus,
    ) -> bool:
        async with AsyncSessionLocal() as session:
            existing = await session.scalar(
                select(RecoveryItem.id).where(RecoveryItem.tg_message_id == message.id)
            )
            if existing is not None:
                return False

            session.add(
                RecoveryItem(
                    tg_message_id=message.id,
                    media_kind=kind,
                    file_name=file_name,
                    file_size=file_size,
                    message_date=message.date,
                    status=status,
                )
            )
            await session.commit()
            return True

    # -- processing ---------------------------------------------------------

    async def _process(
        self, dry_run: bool, limit: int | None, max_download_bytes: int | None
    ) -> None:
        limit = limit or self.batch_size
        max_download_bytes = max_download_bytes or self.batch_max_download_bytes
        statuses = [RecoveryStatus.SCANNED, RecoveryStatus.PLANNED]

        async with AsyncSessionLocal() as session:
            item_ids = list(
                await session.scalars(
                    select(RecoveryItem.id)
                    .where(RecoveryItem.status.in_(statuses))
                    .order_by(RecoveryItem.tg_message_id)
                    .limit(limit)
                )
            )

        batch: dict[str, object] = {
            "dry_run": dry_run,
            "total": len(item_ids),
            "processed": 0,
            "captioned": 0,
            "planned": 0,
            "already": 0,
            "skipped": 0,
            "deferred": 0,
            "failed": 0,
            "downloaded_bytes": 0,
        }
        self._batch = batch
        logger.info(
            "Tidy %s: batch of %s item(s).", "dry run" if dry_run else "run", len(item_ids)
        )

        for item_id in item_ids:
            outcome = await self._process_item(item_id, dry_run)
            batch["processed"] = int(batch["processed"]) + 1
            for key in ("captioned", "planned", "already", "skipped", "deferred", "failed"):
                if outcome.get(key):
                    batch[key] = int(batch[key]) + 1
            batch["downloaded_bytes"] = int(batch["downloaded_bytes"]) + int(
                outcome.get("downloaded_bytes", 0) or 0
            )

            if int(batch["downloaded_bytes"]) >= max_download_bytes:
                logger.info(
                    "Batch download cap reached (%.1f GB); stopping batch early.",
                    int(batch["downloaded_bytes"]) / 1024**3,
                )
                break
            if self.delay_seconds > 0:
                await asyncio.sleep(self.delay_seconds)

        self._batch = None
        self._last_batch = batch
        logger.info("Tidy batch finished: %s", batch)

    async def _process_item(self, item_id: int, dry_run: bool) -> dict[str, object]:
        async with AsyncSessionLocal() as session:
            item = await session.get(RecoveryItem, item_id)
            if item is None:
                return {}

            outcome: dict[str, object] = {}
            try:
                outcome = await self._tidy_item(item, dry_run)
            except asyncio.CancelledError:
                raise
            except FloodWait as exc:
                # Rate limiting is not the item's fault: wait it out, no retry cost.
                wait_seconds = float(getattr(exc, "value", 30) or 30)
                logger.warning("FloodWait: sleeping %.0fs.", wait_seconds)
                await session.commit()
                await asyncio.sleep(wait_seconds + 1)
                return {"floodwait": True}
            except Exception:
                item.retry_count += 1
                item.error_log = traceback.format_exc()
                if item.retry_count >= self.max_retries:
                    item.status = RecoveryStatus.FAILED
                logger.exception("Tidy failed for message id=%s", item.tg_message_id)
                outcome = {"failed": True}

            await session.commit()
            return outcome

    async def _tidy_item(self, item: RecoveryItem, dry_run: bool) -> dict[str, object]:
        client = self.telegram.client
        channel_id = self.telegram.channel_id
        downloaded_bytes = 0

        need_exif = item.planned_caption is None and self._needs_exif(item)
        need_message = need_exif or not dry_run

        message = None
        if need_message:
            message = await client.get_messages(channel_id, item.tg_message_id)
            if self._is_gone(message):
                item.status = RecoveryStatus.SKIPPED
                item.error_log = "Source message no longer exists."
                return {"skipped": True}

        hashtags = item.planned_caption
        if hashtags is None:
            if need_exif:
                if not self._space_for(item.file_size):
                    free_gb = self._free_bytes() / 1024**3
                    logger.info(
                        "Deferring id=%s (%.0f MB): free=%.1f GB would breach %.1f GB floor.",
                        item.tg_message_id,
                        (item.file_size or 0) / 1024**2,
                        free_gb,
                        self.min_free_bytes / 1024**3,
                    )
                    return {"deferred": True}
                path = await self._download(client, message, item)
                try:
                    downloaded_bytes = path.stat().st_size
                    hashtags = await build_caption(path, fallback=item.message_date)
                finally:
                    self._remove(path)
            else:
                hashtags = _caption_from_metadata(item)
            item.planned_caption = hashtags

        if hashtags is None:
            item.status = RecoveryStatus.SKIPPED
            item.error_log = "No date could be derived (no filename/EXIF/message date)."
            return {"skipped": True, "downloaded_bytes": downloaded_bytes}

        if dry_run:
            item.status = RecoveryStatus.PLANNED
            return {"planned": True, "downloaded_bytes": downloaded_bytes}

        merged = _merge_caption(getattr(message, "caption", None), hashtags)
        if merged is None:
            item.status = RecoveryStatus.COMPLETED
            return {"already": True, "downloaded_bytes": downloaded_bytes}

        await self.telegram.edit_caption(item.tg_message_id, merged)
        item.status = RecoveryStatus.COMPLETED
        return {"captioned": True, "downloaded_bytes": downloaded_bytes}

    # -- browse backfill ----------------------------------------------------

    async def _backfill(self, limit: int | None, max_video_bytes: int | None) -> None:
        if self.telegram.browse_channel_id is None:
            raise RuntimeError("No browse channel configured (set BROWSE_CHANNEL_ID).")
        limit = limit or self.batch_size

        async with AsyncSessionLocal() as session:
            item_ids = list(
                await session.scalars(
                    select(RecoveryItem.id)
                    .where(
                        RecoveryItem.media_kind.in_(BROWSE_MEDIA_KINDS),
                        RecoveryItem.browse_tg_message_id.is_(None),
                    )
                    .order_by(RecoveryItem.tg_message_id)
                    .limit(limit)
                )
            )

        batch: dict[str, object] = {
            "mode": "backfill",
            "total": len(item_ids),
            "processed": 0,
            "copied": 0,
            "skipped": 0,
            "failed": 0,
        }
        self._batch = batch
        logger.info("Browse backfill: %s candidate item(s).", len(item_ids))

        for item_id in item_ids:
            outcome = await self._backfill_item(item_id, max_video_bytes)
            batch["processed"] = int(batch["processed"]) + 1
            for key in ("copied", "skipped", "failed"):
                if outcome.get(key):
                    batch[key] = int(batch[key]) + 1
            if self.delay_seconds > 0:
                await asyncio.sleep(self.delay_seconds)

        self._batch = None
        self._last_batch = batch
        logger.info("Browse backfill finished: %s", batch)

    async def _backfill_item(
        self, item_id: int, max_video_bytes: int | None
    ) -> dict[str, object]:
        async with AsyncSessionLocal() as session:
            item = await session.get(RecoveryItem, item_id)
            if item is None or item.browse_tg_message_id is not None:
                return {}

            if (
                max_video_bytes
                and item.media_kind == "video"
                and (item.file_size or 0) > max_video_bytes
            ):
                return {"skipped": True}

            outcome: dict[str, object] = {}
            try:
                caption = item.planned_caption or _caption_from_metadata(item)
                copied = await self.telegram.copy_to_browse(
                    self.telegram.channel_id, item.tg_message_id, caption=caption
                )
                if copied is None:
                    return {"skipped": True}
                item.browse_tg_message_id = copied.id
                outcome = {"copied": True}
            except asyncio.CancelledError:
                raise
            except FloodWait as exc:
                wait_seconds = float(getattr(exc, "value", 30) or 30)
                logger.warning("FloodWait (backfill): sleeping %.0fs.", wait_seconds)
                await asyncio.sleep(wait_seconds + 1)
                return {"floodwait": True}
            except Exception:
                item.retry_count += 1
                item.error_log = traceback.format_exc()
                logger.exception("Backfill failed for message id=%s", item.tg_message_id)
                outcome = {"failed": True}

            await session.commit()
            return outcome

    @staticmethod
    def _needs_exif(item: RecoveryItem) -> bool:
        if item.media_kind != "document":
            return False
        name = item.file_name or ""
        if _parse_filename_datetime(name):
            return False
        return Path(name).suffix.lower() in IMAGE_SUFFIXES

    def _free_bytes(self) -> int:
        return shutil.disk_usage(self.download_root).free

    def _space_for(self, file_size: int | None) -> bool:
        return self._free_bytes() - (file_size or 0) >= self.min_free_bytes

    async def _download(self, client, message: Message, item: RecoveryItem) -> Path:
        base_name = item.file_name or f"{item.media_kind}_{item.tg_message_id}"
        target = self.download_root / f"{item.tg_message_id}_{_safe_name(base_name)}"
        downloaded = await client.download_media(message, file_name=str(target))
        if not downloaded:
            raise RuntimeError(f"download_media returned nothing for {item.tg_message_id}")
        return Path(downloaded)

    @staticmethod
    def _is_gone(message: Message | None) -> bool:
        return message is None or getattr(message, "empty", False)

    @staticmethod
    def _remove(path: Path) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
