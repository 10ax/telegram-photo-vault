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
import traceback
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


def _backup_state_channel_id(conn: sqlite3.Connection, path: Path) -> int | None:
    """`meta.channel_id` from the backup script's state DB, or None.

    Its own `meta` table is where `scripts/backup_local_folder.py` records the
    channel it uploaded to. A state DB written before that table existed simply
    has no answer here, which is a fallback to hash matching, not an error.
    """
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'channel_id'").fetchone()
    except sqlite3.Error:
        row = None
    if row is None or row[0] is None:
        logger.info(
            "Catalog match: backup state DB at %s records no channel id; matching on "
            "sha256 alone, because a message id means nothing without its channel.",
            path,
        )
        return None
    value = str(row[0]).strip()
    if not value.lstrip("-").isdigit():
        logger.warning(
            "Catalog match: backup state DB at %s has a non-numeric channel id %r; "
            "matching on sha256 alone.",
            path,
            value,
        )
        return None
    return int(value)


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


class CatalogBusyError(RuntimeError):
    """A catalog task is already running; a second would race it."""


class CatalogService:
    def __init__(
        self,
        client,
        channels: Sequence[ChannelSpec],
        *,
        scan_delay_seconds: float = 0.0,
        worker_channel_id: int | None = None,
        backup_state_db: str | Path | None = None,
    ) -> None:
        self.client = client
        self.channels = tuple(channels)
        self.scan_delay_seconds = scan_delay_seconds
        # The channel the MEGA worker itself uploads to. Message ids restart at
        # 1 in every channel, so provenance matching without this would
        # mis-attribute rows across channels rather than fail to match.
        self.worker_channel_id = worker_channel_id
        self.backup_state_db = backup_state_db

        self.activity: str | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._result: dict[str, object] | None = None
        self._last_result: dict[str, object] | None = None

    @property
    def archive_channels(self) -> tuple[ChannelSpec, ...]:
        return tuple(c for c in self.channels if c.role is ChannelRole.ARCHIVE)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status_snapshot(self) -> dict[str, object]:
        return {
            "running": self.running,
            "activity": self.activity if self.running else None,
            "last_error": self.last_error,
            "result": self._result if self.running else self._last_result,
            "channels": [
                {"channel_id": spec.channel_id, "role": spec.role.value}
                for spec in self.channels
            ],
        }

    def start_scan(self) -> None:
        """Walk every configured channel's history, then attribute provenance.

        A real channel is 18,000+ messages, which is minutes of paced Telegram
        traffic — far longer than any HTTP client will wait, and two of them at
        once would write the same rows from two directions. So this follows
        RecoveryService: one background task per call, 409 while it runs.
        """
        self._start(self._scan_and_match(), "scanning channel history")

    def _start(self, coroutine, activity: str) -> None:
        if self.running:
            coroutine.close()
            raise CatalogBusyError("A catalog task is already running.")
        self.activity = activity
        self._result = None
        self._task = asyncio.create_task(self._guarded(coroutine), name="catalog")

    async def _guarded(self, coroutine) -> None:
        try:
            await coroutine
            self.last_error = None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Catalog task failed.")
            self.last_error = traceback.format_exc()
        finally:
            self.activity = None
            # Keep whatever the run got through, so a failure is still readable.
            if self._result is not None:
                self._last_result = self._result

    async def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _scan_and_match(self) -> None:
        scanned = await self.scan_all()
        self._result = {"scanned": scanned, "matched": None}
        matched = await self.match_all()
        self._result = {"scanned": scanned, "matched": matched}

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
        """Attribute rows to the MEGA worker by tg_message_id, in its own channel.

        photos.tg_message_id is unique *within the worker's channel* and means
        nothing outside it: every channel's ids start at 1, so the iPhone
        migration channel holds a message 42 as surely as the main archive
        does. Matching without the channel predicate would stamp one file's
        sha256 and message id onto an unrelated row — and those two columns are
        what the strongest verdict tier and the deletion audit are built on.
        There is no sha256 fallback because the worker never re-uploads a file
        under a new message without also updating its row.
        """
        if self.worker_channel_id is None:
            logger.info(
                "Catalog match: no worker channel id configured, so no row can be "
                "attributed to the worker without risking a cross-channel collision."
            )
            return 0

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
                    select(CatalogItem).where(
                        CatalogItem.source == CatalogSource.UNKNOWN,
                        CatalogItem.channel_id == self.worker_channel_id,
                    )
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
        """Attribute rows to scripts/backup_local_folder.py, in its own channel.

        That script keeps its own stdlib sqlite3 state DB, never the app's, so
        this reads it directly and read-only. A missing file is a normal
        configuration state, not an error: report zero and move on.

        The script records the channel it wrote to in that DB's `meta` table,
        and its message ids only mean anything there — every channel numbers
        its messages from 1. With no channel id recorded (an older state DB),
        a bare message id is not evidence of anything, so only sha256 is used:
        content identity holds wherever the bytes sit.
        """
        path = Path(state_db_path)
        if not path.exists():
            logger.info("Catalog match: no backup state DB at %s, skipping.", path)
            return 0

        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                channel_id = _backup_state_channel_id(conn, path)
                rows = conn.execute(
                    "SELECT rel_path, sha256, tg_message_id FROM files "
                    "WHERE tg_message_id IS NOT NULL"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.warning("Catalog match: could not read backup state DB at %s: %s", path, exc)
            return 0

        by_message = (
            {int(mid): (rel, sha) for rel, sha, mid in rows} if channel_id is not None else {}
        )
        by_sha = {sha: (rel, int(mid)) for rel, sha, mid in rows if sha}

        matched = 0
        async with AsyncSessionLocal() as session:
            items = (
                await session.scalars(
                    select(CatalogItem).where(CatalogItem.source == CatalogSource.UNKNOWN)
                )
            ).all()

            for item in items:
                hit = by_message.get(item.tg_message_id) if item.channel_id == channel_id else None
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
        """Both provenance passes; the state DB defaults to the configured one."""
        state_db = state_db_path if state_db_path is not None else self.backup_state_db
        return {
            "worker": await self.match_worker(),
            "backup_script": (await self.match_backup_db(state_db) if state_db else 0),
        }
