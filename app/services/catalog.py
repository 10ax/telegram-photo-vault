"""One index over every channel this vault writes to.

The governing principle is that the channel is the archive and this table is a
cache of what we know about it. Scans therefore only ever add or refresh facts
that come from the message itself; anything derived later (EXIF, export state)
is owned by other operations and is never cleared by a rescan.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import select

from app.models.database import (
    AsyncSessionLocal,
    CatalogItem,
    ChannelRole,
)

logger = logging.getLogger(__name__)

MEDIA_KINDS = ("photo", "video", "document", "animation")

# Same contract as app/services/chunking.py: name.partNNN-of-MMM plus a JSON
# manifest. These are vault artifacts, not user media, and must not be counted
# as photos by anything downstream.
CHUNK_PART_RE = re.compile(r"\.part\d+-of-\d+$")
MANIFEST_SUFFIX = ".manifest.json"

PROGRESS_EVERY = 500


def classify_artifact(file_name: str | None) -> str | None:
    if not file_name:
        return None
    stem = file_name.strip()
    if CHUNK_PART_RE.search(stem):
        return "chunk"
    if stem.endswith(MANIFEST_SUFFIX):
        return "manifest"
    return None


@dataclass(frozen=True)
class ChannelSpec:
    channel_id: int | str
    role: ChannelRole


def _media_info(message) -> tuple[str, str | None, int | None, str | None] | None:
    """(kind, file_name, file_size, mime_type) or None for a non-media message."""
    if getattr(message, "photo", None) is not None:
        # Native photos carry no file name: Telegram re-encoded them on upload.
        return "photo", None, message.photo.file_size, "image/jpeg"
    if getattr(message, "video", None) is not None:
        video = message.video
        return "video", video.file_name, video.file_size, getattr(video, "mime_type", None)
    if getattr(message, "animation", None) is not None:
        anim = message.animation
        return "animation", anim.file_name, anim.file_size, getattr(anim, "mime_type", None)
    if getattr(message, "document", None) is not None:
        doc = message.document
        return "document", doc.file_name, doc.file_size, getattr(doc, "mime_type", None)
    return None


class CatalogService:
    def __init__(
        self,
        client,
        channels: Sequence[ChannelSpec],
        *,
        scan_delay_seconds: float = 0.0,
    ) -> None:
        self.client = client
        self.channels = tuple(channels)
        self.scan_delay_seconds = scan_delay_seconds

    @property
    def archive_channels(self) -> tuple[ChannelSpec, ...]:
        return tuple(c for c in self.channels if c.role is ChannelRole.ARCHIVE)

    async def scan_channel(self, spec: ChannelSpec) -> dict[str, int]:
        scanned = ingested = updated = 0

        async for message in self.client.get_chat_history(spec.channel_id):
            scanned += 1
            info = _media_info(message)
            if info is None:
                continue

            kind, file_name, file_size, mime_type = info
            outcome = await self._upsert(spec, message, kind, file_name, file_size, mime_type)
            if outcome == "ingested":
                ingested += 1
            elif outcome == "updated":
                updated += 1

            if scanned % PROGRESS_EVERY == 0:
                logger.info(
                    "Catalog scan %s: %s seen, %s new, %s refreshed.",
                    spec.channel_id,
                    scanned,
                    ingested,
                    updated,
                )

        logger.info(
            "Catalog scan %s finished: %s messages, %s new, %s refreshed.",
            spec.channel_id,
            scanned,
            ingested,
            updated,
        )
        return {"scanned": scanned, "ingested": ingested, "updated": updated}

    async def scan_all(self) -> dict[str, dict[str, int]]:
        results: dict[str, dict[str, int]] = {}
        for spec in self.channels:
            results[str(spec.channel_id)] = await self.scan_channel(spec)
            if self.scan_delay_seconds > 0:
                await asyncio.sleep(self.scan_delay_seconds)
        return results

    async def _upsert(
        self,
        spec: ChannelSpec,
        message,
        kind: str,
        file_name: str | None,
        file_size: int | None,
        mime_type: str | None,
    ) -> str:
        channel_id = int(spec.channel_id)
        async with AsyncSessionLocal() as session:
            existing = await session.scalar(
                select(CatalogItem).where(
                    CatalogItem.channel_id == channel_id,
                    CatalogItem.tg_message_id == message.id,
                )
            )

            if existing is None:
                session.add(
                    CatalogItem(
                        channel_id=channel_id,
                        tg_message_id=message.id,
                        channel_role=spec.role,
                        media_kind=kind,
                        artifact=classify_artifact(file_name),
                        file_name=file_name,
                        file_size=file_size,
                        mime_type=mime_type,
                        message_date=getattr(message, "date", None),
                    )
                )
                await session.commit()
                return "ingested"

            # Refresh only facts that come from the message. Enrichment and
            # export state belong to other operations and are left alone.
            changed = False
            for field, value in (
                ("media_kind", kind),
                ("file_name", file_name),
                ("file_size", file_size),
                ("mime_type", mime_type),
                ("message_date", getattr(message, "date", None)),
                ("channel_role", spec.role),
                ("artifact", classify_artifact(file_name)),
            ):
                if value is not None and getattr(existing, field) != value:
                    setattr(existing, field, value)
                    changed = True

            if changed:
                await session.commit()
                return "updated"
            return "unchanged"
