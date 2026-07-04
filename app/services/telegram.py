from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path

from PIL import Image
from pillow_heif import register_heif_opener
from pyrogram import Client
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


def _extract_datetime_sync(file_path: str | Path, fallback: datetime | None = None) -> datetime:
    path = Path(file_path)

    try:
        with Image.open(path) as image:
            exif = image.getexif()
            for tag in EXIF_DATETIME_TAGS:
                parsed = _parse_exif_datetime(exif.get(tag))
                if parsed is not None:
                    return parsed
    except Exception:
        pass

    from_name = _parse_filename_datetime(path.name)
    if from_name is not None:
        return from_name

    if fallback is not None:
        return fallback

    return datetime.fromtimestamp(path.stat().st_mtime)


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
