from __future__ import annotations

import asyncio
import hashlib
import io
import re
from datetime import datetime
from pathlib import Path

from PIL import Image
from pillow_heif import register_heif_opener
from pyrogram import Client, enums
from pyrogram.errors import MessageNotModified
from pyrogram.types import Message

register_heif_opener()

EXIF_DATETIME_TAGS = (36867, 36868, 306)

# Telegram streams file bytes in fixed 1 MiB chunks; a smaller range cannot be
# requested. Fingerprinting fetches exactly the first and last whole chunk.
STREAM_CHUNK_BYTES = 1024 * 1024

# Dates embedded in camera filenames, e.g. IMG_20240612_193000.jpg, VID-20230101-WA0001.mp4,
# 2024-06-12 19.30.00.jpg. Time components are optional.
FILENAME_DATETIME_RE = re.compile(
    r"((?:19|20)\d{2})[-_.]?(\d{2})[-_.]?(\d{2})"
    r"(?:[-_ .T]?(\d{2})[-_.:]?(\d{2})[-_.:]?(\d{2})?)?"
)


def _parse_exif_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None

    try:
        return datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def _parse_filename_datetime(name: str) -> datetime | None:
    for match in FILENAME_DATETIME_RE.finditer(name):
        year, month, day, hour, minute, second = match.groups()
        try:
            return datetime(
                int(year),
                int(month),
                int(day),
                int(hour) if hour else 0,
                int(minute) if minute else 0,
                int(second) if second else 0,
            )
        except ValueError:
            continue
    return None


def _extract_datetime_with_source_sync(
    file_path: str | Path, fallback: datetime | None = None
) -> tuple[datetime, str]:
    path = Path(file_path)

    try:
        with Image.open(path) as image:
            exif = image.getexif()
            for tag in EXIF_DATETIME_TAGS:
                parsed = _parse_exif_datetime(exif.get(tag))
                if parsed is not None:
                    return parsed, "exif"
    except Exception:
        pass

    from_name = _parse_filename_datetime(path.name)
    if from_name is not None:
        return from_name, "filename"

    if fallback is not None:
        return fallback, "fallback"

    return datetime.fromtimestamp(path.stat().st_mtime), "mtime"


def _extract_datetime_sync(file_path: str | Path, fallback: datetime | None = None) -> datetime:
    return _extract_datetime_with_source_sync(file_path, fallback)[0]


async def extract_datetime_with_source(
    file_path: str | Path, fallback: datetime | None = None
) -> tuple[datetime, str]:
    return await asyncio.to_thread(_extract_datetime_with_source_sync, file_path, fallback)


def format_date_caption(photo_datetime: datetime) -> str:
    return _format_caption(photo_datetime)


def _format_caption(photo_datetime: datetime) -> str:
    return (
        f"#{photo_datetime:%Y} "
        f"#{photo_datetime:%m_%Y} "
        f"#{photo_datetime:%Y_%m_%d}"
    )


async def build_caption(file_path: str | Path, fallback: datetime | None = None) -> str:
    photo_datetime = await asyncio.to_thread(_extract_datetime_sync, file_path, fallback)
    return _format_caption(photo_datetime)


def _archived_file_size(message) -> int | None:
    """The size Telegram itself reports for a message's media, or None.

    Only the kinds this vault archives as originals are consulted. A native
    `photo` message is a re-encoded mirror and never proof of an original, so
    it yields None here too — and None fails every size comparison closed.
    """
    for attribute in ("document", "video", "animation"):
        media = getattr(message, attribute, None)
        if media is not None:
            size = getattr(media, "file_size", None)
            return int(size) if size is not None else None
    return None


class ArchivedMessageMissing(RuntimeError):
    """The channel message fingerprint_message was asked for no longer exists.

    get_messages returns None for a single missing/deleted id rather than
    raising — silently proceeding would surface as a bare AttributeError deep
    inside stream_media, which looks like (and could mask) an unrelated bug.
    """


class TelegramService:
    def __init__(
        self,
        client: Client,
        channel_id: int | str,
        *,
        upload_delay_seconds: float = 5.0,
        browse_channel_id: int | str | None = None,
    ) -> None:
        self.client = client
        self.channel_id = channel_id
        self.upload_delay_seconds = upload_delay_seconds
        # Optional shared "browse" channel: native captioned copies for humans to
        # scroll/search in the Telegram app (the main channel holds the archival
        # documents + chunk parts + manifests). None disables mirroring.
        self.browse_channel_id = browse_channel_id

    async def upload_document(
        self,
        file_path: str | Path,
        *,
        caption: str | None = None,
        file_name: str | None = None,
        caption_fallback: datetime | None = None,
    ) -> Message:
        path = Path(file_path)
        if caption is None:
            caption = await build_caption(path, fallback=caption_fallback)

        message = await self.client.send_document(
            chat_id=self.channel_id,
            document=str(path),
            caption=caption,
            file_name=file_name,
            # Never let Telegram interpret the payload: without this it recognises
            # GIFs and short soundless mp4s (Pixel/Samsung motion photos) as
            # animations, transcodes them — losing the original bytes, so the
            # archive no longer matches its sha256 — and auto-adds each one to the
            # account's saved-GIFs library. Both happened before this was set.
            force_document=True,
        )

        if self.upload_delay_seconds > 0:
            await asyncio.sleep(self.upload_delay_seconds)
        return message

    async def upload_file_object(self, file_object, caption: str) -> Message:
        """Upload a binary file-like object (with .name) as a document."""
        message = await self.client.send_document(
            chat_id=self.channel_id,
            document=file_object,
            caption=caption,
            file_name=file_object.name,
            # Same reason as upload_document: chunk parts and manifests must land
            # as untouched bytes, whatever their extension looks like.
            force_document=True,
        )

        if self.upload_delay_seconds > 0:
            await asyncio.sleep(self.upload_delay_seconds)
        return message

    async def upload_bytes(self, data: bytes, *, file_name: str, caption: str) -> Message:
        buffer = io.BytesIO(data)
        buffer.name = file_name
        return await self.upload_file_object(buffer, caption)

    async def upload_media(
        self,
        file_path: str | Path,
        media_type,
        *,
        caption: str | None = None,
        caption_fallback: datetime | None = None,
    ) -> Message | None:
        """Upload file as native media (photo or video). Returns None for unsupported types."""
        from app.models.database import MediaType

        path = Path(file_path)
        if caption is None:
            caption = await build_caption(path, fallback=caption_fallback)

        try:
            if media_type == MediaType.IMAGE:
                message = await self.client.send_photo(
                    chat_id=self.channel_id,
                    photo=str(path),
                    caption=caption,
                )
            elif media_type == MediaType.VIDEO:
                message = await self.client.send_video(
                    chat_id=self.channel_id,
                    video=str(path),
                    caption=caption,
                )
            else:
                return None
        except Exception:
            return None

        if self.upload_delay_seconds > 0:
            await asyncio.sleep(self.upload_delay_seconds)
        return message

    async def edit_caption(self, message_id: int, caption: str) -> bool:
        """Set an existing message's caption in place, keeping the media untouched.

        This is the cheap "tidy" path: no download, no re-upload, no delete —
        just the caption. Returns True when the caption is applied (or already
        identical). Pacing is left to the caller so batch runs control throughput.
        """
        try:
            await self.client.edit_message_caption(
                chat_id=self.channel_id,
                message_id=message_id,
                caption=caption,
            )
        except MessageNotModified:
            pass
        return True

    async def publish_browse(
        self,
        file_path: str | Path,
        media_type,
        *,
        caption: str | None = None,
        caption_fallback: datetime | None = None,
    ) -> Message | None:
        """Mirror a native captioned photo/video to the shared browse channel.

        Best-effort: returns None if browsing is disabled, the type is
        unsupported, or the send fails — a browse-copy failure must never break
        the archival pipeline.
        """
        if self.browse_channel_id is None:
            return None

        from app.models.database import MediaType

        path = Path(file_path)
        if caption is None:
            caption = await build_caption(path, fallback=caption_fallback)

        try:
            if media_type == MediaType.IMAGE:
                message = await self.client.send_photo(
                    chat_id=self.browse_channel_id, photo=str(path), caption=caption
                )
            elif media_type == MediaType.VIDEO:
                message = await self.client.send_video(
                    chat_id=self.browse_channel_id, video=str(path), caption=caption
                )
            else:
                return None
        except Exception:
            return None

        if self.upload_delay_seconds > 0:
            await asyncio.sleep(self.upload_delay_seconds)
        return message

    async def copy_to_browse(
        self, from_chat_id: int | str, message_id: int, *, caption: str | None = None
    ) -> Message | None:
        """Server-side copy of an existing media message into the browse channel.

        Uses Telegram's copy (no download, no re-upload — the bytes never touch
        this machine). Returns None when browsing is disabled. Errors (incl.
        FloodWait) propagate so the caller can pace/retry.
        """
        if self.browse_channel_id is None:
            return None
        return await self.client.copy_message(
            chat_id=self.browse_channel_id,
            from_chat_id=from_chat_id,
            message_id=message_id,
            caption=caption,
        )

    async def find_document_by_name(self, file_name: str) -> Message | None:
        """Best-effort channel search for a document with this exact filename.

        Used to close the crash window between sending a chunk and committing
        its message id: on retry, an already-uploaded chunk is reused instead of
        duplicated. Any search failure is treated as not-found.
        """
        try:
            async for message in self.client.search_messages(
                self.channel_id,
                query=file_name,
                filter=enums.MessagesFilter.DOCUMENT,
                limit=10,
            ):
                document = getattr(message, "document", None)
                if document is not None and document.file_name == file_name:
                    return message
        except Exception:
            return None
        return None

    async def partial_fingerprint(
        self, message, *, file_size: int, window: int = 262_144
    ) -> dict[str, str]:
        """Hash the first and last `window` bytes of an archived file.

        Settles an ambiguous local file against its archived copy without
        downloading it. Telegram streams in 1 MiB chunks, so the head is the
        start of the first chunk; the tail is genuinely the file's last
        `window` bytes, which can straddle a chunk boundary when the final
        chunk is shorter than the window (a 1,100,000-byte file ends in a
        51,424-byte chunk). Hashing that short chunk instead would make a
        byte-identical file fail to match, so the two chunks the range falls
        in are read and sliced. `window` is clamped to one chunk, which is
        what keeps "at most two chunks" true whatever the caller asks for.
        """
        window = max(1, min(window, STREAM_CHUNK_BYTES))
        head = await self._read_chunk(message, 0)

        start = max(file_size - window, 0)
        first_index = start // STREAM_CHUNK_BYTES
        last_index = max((file_size - 1) // STREAM_CHUNK_BYTES, 0)

        spans: list[bytes] = []
        for index in range(first_index, last_index + 1):
            spans.append(head if index == 0 else await self._read_chunk(message, index))
        tail = b"".join(spans)

        return {
            "head_sha256": hashlib.sha256(head[:window]).hexdigest(),
            "tail_sha256": hashlib.sha256(tail[-window:]).hexdigest(),
        }

    async def fingerprint_message(
        self, channel_id: int, message_id: int, *, file_size: int, window: int = 262_144
    ) -> dict[str, object]:
        """Fetch one archived message, fingerprint it, and report its own size.

        The size comes from the message's own media, never from the caller:
        equal head and tail digests are not identity on their own, and the
        endpoint that authorises a deletion has to be able to compare the
        archived length with the local one. `file_size` is the caller's claim
        and is used only as a fallback for choosing the tail's chunk range
        when the message carries no size at all.
        """
        message = await self.client.get_messages(channel_id, message_id)
        if message is None:
            raise ArchivedMessageMissing(
                f"Message {message_id} in channel {channel_id} was not found or has been deleted."
            )
        archived_file_size = _archived_file_size(message)
        fingerprint = await self.partial_fingerprint(
            message,
            file_size=archived_file_size if archived_file_size is not None else file_size,
            window=window,
        )
        return {**fingerprint, "archived_file_size": archived_file_size}

    async def _read_chunk(self, message, index: int) -> bytes:
        buffer = bytearray()
        async for part in self.client.stream_media(message, offset=index, limit=1):
            buffer.extend(part)
        return bytes(buffer)
