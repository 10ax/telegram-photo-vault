from __future__ import annotations

import asyncio
import io
import re
from datetime import datetime
from pathlib import Path

from PIL import Image
from pillow_heif import register_heif_opener
from pyrogram import Client, enums
from pyrogram.types import Message

register_heif_opener()

EXIF_DATETIME_TAGS = (36867, 36868, 306)

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


class TelegramService:
    def __init__(
        self,
        client: Client,
        channel_id: int | str,
        *,
        upload_delay_seconds: float = 5.0,
    ) -> None:
        self.client = client
        self.channel_id = channel_id
        self.upload_delay_seconds = upload_delay_seconds

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
