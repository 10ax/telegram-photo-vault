from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from PIL import Image
from pyrogram import Client
from pyrogram.types import Message

EXIF_DATETIME_TAGS = (36867, 36868, 306)


def _parse_exif_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None

    try:
        return datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def _extract_datetime_sync(file_path: str | Path) -> datetime:
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

    return datetime.fromtimestamp(path.stat().st_mtime)


def _format_caption(photo_datetime: datetime) -> str:
    return (
        f"#{photo_datetime:%Y} "
        f"#{photo_datetime:%m_%Y} "
        f"#{photo_datetime:%Y_%m_%d}"
    )


async def build_caption(file_path: str | Path) -> str:
    photo_datetime = await asyncio.to_thread(_extract_datetime_sync, file_path)
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

    async def upload_document(self, file_path: str | Path) -> Message:
        path = Path(file_path)
        caption = await build_caption(path)

        message = await self.client.send_document(
            chat_id=self.channel_id,
            document=str(path),
            caption=caption,
        )

        if self.upload_delay_seconds > 0:
            await asyncio.sleep(self.upload_delay_seconds)
        return message
