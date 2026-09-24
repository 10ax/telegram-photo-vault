"""One index over every channel this vault writes to.

The governing principle is that the channel is the archive and this table is a
cache of what we know about it. Scans therefore only ever add or refresh facts
that come from the message itself; anything derived later (EXIF, export state)
is owned by other operations and is never cleared by a rescan.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from sqlalchemy import func, select

from app.models.database import (
    AsyncSessionLocal,
    CatalogItem,
    CatalogSource,
    ChannelRole,
    Photo,
)
from app.services.chunking import MANIFEST_KIND

logger = logging.getLogger(__name__)

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


def parse_manifest(payload: bytes) -> tuple[str, int, str]:
    """(original_filename, total_size, sha256) from a manifest's bytes.

    Raises ValueError on anything that is not a manifest this repo wrote. The
    on-channel format is a contract: read it strictly rather than guessing.
    """
    document = json.loads(payload.decode("utf-8"))
    if document.get("kind") != MANIFEST_KIND:
        raise ValueError(f"not a chunked-file manifest: kind={document.get('kind')!r}")

    name = document["original_filename"]
    total_size = document["total_size"]
    sha256 = document["sha256"]
    if not isinstance(name, str) or not name:
        raise ValueError("original_filename missing or empty")
    if not isinstance(total_size, int) or total_size <= 0:
        raise ValueError(f"total_size not a positive integer: {total_size!r}")
    if not isinstance(sha256, str) or len(sha256) != 64:
        raise ValueError(f"sha256 not a 64-character digest: {sha256!r}")
    return name, total_size, sha256


@dataclass(frozen=True)
class ChannelSpec:
    channel_id: int
    role: ChannelRole


def channel_spec_or_none(
    var_name: str, value: int | str, role: ChannelRole
) -> ChannelSpec | None:
    """Build a `ChannelSpec`, or `None` (after logging) if `value` isn't numeric.

    `catalog_items.channel_id` is a `BigInteger` column, so a channel addressed
    by username (e.g. `"@somechannel"`) cannot be catalogued. The composition
    root uses this so a deployment with a username-only channel id still
    starts — that channel is just skipped by the catalog, not the app.
    """
    if isinstance(value, int):
        return ChannelSpec(value, role)
    logger.warning(
        "%s=%r is not numeric; the catalog cannot index a channel addressed by "
        "username, so it is being skipped.",
        var_name,
        value,
    )
    return None


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

    async def resolve_manifests(self, limit: int = 50) -> dict[str, int]:
        """Read unresolved manifests and record the original each one describes.

        Bounded and resumable like every other loop that touches Telegram. A
        manifest that cannot be parsed records the reason and is never retried:
        one fetch per broken file, not one per run forever.
        """
        archive_ids = [int(spec.channel_id) for spec in self.archive_channels]
        if not archive_ids:
            return {"resolved": 0, "failed": 0, "remaining": 0}

        pending = (
            select(CatalogItem)
            .where(
                CatalogItem.artifact == "manifest",
                CatalogItem.channel_id.in_(archive_ids),
                CatalogItem.chunked_original_name.is_(None),
                CatalogItem.enrich_error.is_(None),
            )
            .order_by(CatalogItem.id)
        )

        async with AsyncSessionLocal() as session:
            rows = list((await session.scalars(pending.limit(limit))).all())

        resolved = failed = 0
        for row in rows:
            try:
                message = await self.client.get_messages(row.channel_id, row.tg_message_id)
                buffer = await self.client.download_media(message, in_memory=True)
                name, total_size, sha256 = parse_manifest(buffer.getvalue())
            except Exception as exc:  # noqa: BLE001 - recorded, not raised
                failed += 1
                async with AsyncSessionLocal() as session:
                    item = await session.get(CatalogItem, row.id)
                    item.enrich_error = f"manifest unreadable: {exc}"[:255]
                    await session.commit()
            else:
                resolved += 1
                async with AsyncSessionLocal() as session:
                    item = await session.get(CatalogItem, row.id)
                    item.chunked_original_name = name
                    item.chunked_total_size = total_size
                    item.chunked_sha256 = sha256
                    await session.commit()

            if self.scan_delay_seconds > 0:
                await asyncio.sleep(self.scan_delay_seconds)

        async with AsyncSessionLocal() as session:
            remaining = await session.scalar(
                select(func.count()).select_from(pending.subquery())
            )

        return {"resolved": resolved, "failed": failed, "remaining": int(remaining or 0)}

    async def _upsert(
        self,
        spec: ChannelSpec,
        message,
        kind: str,
        file_name: str | None,
        file_size: int | None,
        mime_type: str | None,
    ) -> str:
        channel_id = spec.channel_id
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

    async def match_worker(self) -> int:
        """Attribute rows to the MEGA worker by tg_message_id.

        photos.tg_message_id is unique per archive channel, so this is an exact
        join; there is no sha256 fallback because the worker never re-uploads a
        file under a new message without also updating its row.
        """
        matched = 0
        async with AsyncSessionLocal() as session:
            photos = {
                photo.tg_message_id: photo
                for photo in (
                    await session.scalars(select(Photo).where(Photo.tg_message_id.is_not(None)))
                ).all()
            }
            if not photos:
                return 0

            items = (
                await session.scalars(
                    select(CatalogItem).where(CatalogItem.source == CatalogSource.UNKNOWN)
                )
            ).all()

            for item in items:
                photo = photos.get(item.tg_message_id)
                if photo is None:
                    continue
                item.source = CatalogSource.WORKER
                item.photo_id = photo.id
                if item.sha256 is None:
                    item.sha256 = photo.sha256
                matched += 1

            if matched:
                await session.commit()
        return matched

    async def match_backup_db(self, state_db_path: str | Path) -> int:
        """Attribute rows to scripts/backup_local_folder.py.

        That script keeps its own stdlib sqlite3 state DB, never the app's, so
        this reads it directly and read-only. A missing file is a normal
        configuration state, not an error: report zero and move on.
        """
        path = Path(state_db_path)
        if not path.exists():
            logger.info("Catalog match: no backup state DB at %s, skipping.", path)
            return 0

        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT rel_path, sha256, tg_message_id FROM files "
                    "WHERE tg_message_id IS NOT NULL"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.warning("Catalog match: could not read backup state DB at %s: %s", path, exc)
            return 0

        by_message = {int(mid): (rel, sha) for rel, sha, mid in rows}
        by_sha = {sha: (rel, int(mid)) for rel, sha, mid in rows if sha}

        matched = 0
        async with AsyncSessionLocal() as session:
            items = (
                await session.scalars(
                    select(CatalogItem).where(CatalogItem.source == CatalogSource.UNKNOWN)
                )
            ).all()

            for item in items:
                hit = by_message.get(item.tg_message_id)
                if hit is not None:
                    rel_path, sha = hit
                elif item.sha256 and item.sha256 in by_sha:
                    rel_path, _ = by_sha[item.sha256]
                    sha = item.sha256
                else:
                    continue

                item.source = CatalogSource.BACKUP_SCRIPT
                item.backup_rel_path = rel_path
                if item.sha256 is None:
                    item.sha256 = sha
                matched += 1

            if matched:
                await session.commit()
        return matched

    async def match_all(self, state_db_path: str | Path | None = None) -> dict[str, int]:
        return {
            "worker": await self.match_worker(),
            "backup_script": (
                await self.match_backup_db(state_db_path) if state_db_path else 0
            ),
        }
