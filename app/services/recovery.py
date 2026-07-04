from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import traceback
from pathlib import Path

from pyrogram.errors import FloodWait
from pyrogram.types import Message
from sqlalchemy import select

from app.models.database import AsyncSessionLocal, RecoveryItem, RecoveryStatus
from app.services.telegram import TelegramService, build_caption

logger = logging.getLogger(__name__)

MEDIA_KINDS = ("photo", "video", "document", "animation")

# Messages this project created for chunked uploads; never "tidy" those.
CHUNK_PART_RE = re.compile(r"\.part\d+-of-\d+$")
MANIFEST_SUFFIX = ".manifest.json"

# A message is already tidy when it is a document whose caption carries the
# full hashtag scheme (#YYYY #MM_YYYY #YYYY_MM_DD).
TIDY_CAPTION_RE = re.compile(r"#\d{4}\s+#\d{2}_\d{4}\s+#\d{4}_\d{2}_\d{2}")


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.\-]", "_", Path(name).name) or "file"


class RecoveryBusyError(RuntimeError):
    pass


class RecoveryService:
    """Scans the channel history and re-uploads media as tidy captioned documents.

    All operations run in a single background task at a time; the state machine
    per message lives in the recovery_items table so scans and runs are resumable.
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
    ) -> None:
        self.telegram = telegram_service
        self.download_root = Path(download_root)
        self.delay_seconds = delay_seconds
        self.max_retries = max_retries
        self.kinds = tuple(kind for kind in kinds if kind in MEDIA_KINDS)
        self.delete_old = delete_old

        self.activity: str | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task[None] | None = None

        self.download_root.mkdir(parents=True, exist_ok=True)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status_snapshot(self) -> dict[str, object]:
        return {
            "running": self.running,
            "activity": self.activity if self.running else None,
            "delete_old": self.delete_old,
            "last_error": self.last_error,
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

    def start_run(self, dry_run: bool) -> None:
        label = "processing (dry run)" if dry_run else "processing"
        self._start(self._process_all(dry_run), label)

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
        if message.document is None:
            return False
        caption = message.caption or ""
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

    async def _process_all(self, dry_run: bool) -> None:
        processable = [RecoveryStatus.SCANNED, RecoveryStatus.DOWNLOADED]
        if not dry_run:
            processable.append(RecoveryStatus.PLANNED)

        async with AsyncSessionLocal() as session:
            item_ids = list(
                await session.scalars(
                    select(RecoveryItem.id)
                    .where(RecoveryItem.status.in_(processable))
                    .order_by(RecoveryItem.tg_message_id)
                )
            )

        logger.info("Recovery run (dry_run=%s): %s item(s) to process.", dry_run, len(item_ids))
        for item_id in item_ids:
            await self._process_item(item_id, dry_run)
            if self.delay_seconds > 0:
                await asyncio.sleep(self.delay_seconds)

    async def _process_item(self, item_id: int, dry_run: bool) -> None:
        async with AsyncSessionLocal() as session:
            item = await session.get(RecoveryItem, item_id)
            if item is None:
                return

            try:
                await self._run_item_steps(session, item, dry_run)
            except asyncio.CancelledError:
                raise
            except FloodWait as exc:
                # Rate limiting is not the item's fault: wait it out, no retry cost.
                wait_seconds = float(getattr(exc, "value", 30) or 30)
                logger.warning("FloodWait: sleeping %.0fs.", wait_seconds)
                await session.commit()
                await asyncio.sleep(wait_seconds + 1)
                return
            except Exception:
                item.retry_count += 1
                item.error_log = traceback.format_exc()
                if item.retry_count >= self.max_retries:
                    item.status = RecoveryStatus.FAILED
                logger.exception("Recovery failed for message id=%s", item.tg_message_id)

            await session.commit()

    async def _run_item_steps(self, session, item: RecoveryItem, dry_run: bool) -> None:
        client = self.telegram.client
        channel_id = self.telegram.channel_id

        if item.status in (RecoveryStatus.SCANNED, RecoveryStatus.DOWNLOADED):
            local_path = Path(item.local_path) if item.local_path else None
            if local_path is None or not local_path.is_file():
                message = await client.get_messages(channel_id, item.tg_message_id)
                if message is None or getattr(message, "empty", False):
                    item.status = RecoveryStatus.SKIPPED
                    item.error_log = "Source message no longer exists."
                    return

                base_name = item.file_name or f"{item.media_kind}_{item.tg_message_id}"
                target = self.download_root / f"{item.tg_message_id}_{_safe_name(base_name)}"
                downloaded = await client.download_media(message, file_name=str(target))
                if not downloaded:
                    raise RuntimeError(f"download_media returned nothing for {item.tg_message_id}")
                local_path = Path(downloaded)
                item.local_path = str(local_path)

            item.sha256 = await asyncio.to_thread(_sha256_file, local_path)
            item.status = RecoveryStatus.DOWNLOADED

            if await self._is_duplicate(item):
                item.status = RecoveryStatus.DUPLICATE
                self._cleanup_local(item)
                return

            item.planned_caption = await build_caption(local_path, fallback=item.message_date)
            if dry_run:
                item.status = RecoveryStatus.PLANNED
                return

        if item.status == RecoveryStatus.PLANNED:
            if dry_run:
                return
            if not item.local_path or not Path(item.local_path).is_file():
                # Local copy vanished between dry run and real run: start over.
                item.status = RecoveryStatus.SCANNED
                return

        if item.status in (RecoveryStatus.PLANNED, RecoveryStatus.DOWNLOADED):
            caption = item.planned_caption or await build_caption(
                item.local_path, fallback=item.message_date
            )
            upload_name = item.file_name or self._generated_name(item)
            message = await self.telegram.upload_document(
                item.local_path, caption=caption, file_name=upload_name
            )
            item.new_tg_message_id = message.id
            item.status = RecoveryStatus.REUPLOADED
            # Commit before deleting the original: a crash here must never
            # re-upload (duplicate) or lose track of the replacement message.
            await session.commit()

        if item.status == RecoveryStatus.REUPLOADED:
            if self.delete_old:
                await self.telegram.client.delete_messages(channel_id, item.tg_message_id)
            self._cleanup_local(item)
            item.status = RecoveryStatus.COMPLETED

    async def _is_duplicate(self, item: RecoveryItem) -> bool:
        if not item.sha256:
            return False
        async with AsyncSessionLocal() as session:
            other = await session.scalar(
                select(RecoveryItem.id).where(
                    RecoveryItem.sha256 == item.sha256,
                    RecoveryItem.id != item.id,
                    RecoveryItem.status.notin_(
                        [RecoveryStatus.FAILED, RecoveryStatus.SKIPPED, RecoveryStatus.DUPLICATE]
                    ),
                    RecoveryItem.id < item.id,
                )
            )
            return other is not None

    def _generated_name(self, item: RecoveryItem) -> str:
        extension = ".jpg" if item.media_kind == "photo" else ".bin"
        if item.local_path:
            suffix = Path(item.local_path).suffix
            if suffix:
                extension = suffix
        date_part = f"{item.message_date:%Y%m%d_%H%M%S}" if item.message_date else "unknown"
        return f"{item.media_kind}_{date_part}_{item.tg_message_id}{extension}"

    @staticmethod
    def _cleanup_local(item: RecoveryItem) -> None:
        if item.local_path:
            try:
                os.remove(item.local_path)
            except FileNotFoundError:
                pass
            item.local_path = None
