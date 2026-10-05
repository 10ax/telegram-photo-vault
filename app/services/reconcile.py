"""From a device's file inventory to a verdict per file.

`decide` is deliberately pure: no database, no network, no clock. Every rule
that decides whether a photograph may be deleted lives in one function that can
be read in one sitting and tested without a single fake.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import func, select

from app.models.database import (
    AsyncSessionLocal,
    CatalogItem,
    ChannelRole,
    DeletionAudit,
    DeviceFinding,
    DeviceSnapshot,
    DeviceVerdict,
    MatchTier,
    Photo,
    PhotoStatus,
)
from app.services.telegram import STREAM_CHUNK_BYTES

IN_FLIGHT_STATUSES = frozenset(
    {
        PhotoStatus.PENDING,
        PhotoStatus.DOWNLOADED,
        PhotoStatus.CHUNK_UPLOADING,
        PhotoStatus.TG_UPLOADED,
        PhotoStatus.COMPRESSED,
        PhotoStatus.ODROID_UPLOADED,
    }
)


class CatalogNeverScanned(RuntimeError):
    """No archive channel has ever been scanned, so no verdict can be honest."""


@dataclass(frozen=True)
class Candidate:
    """A catalog row that could hold the bytes of a local file."""

    tg_message_id: int
    channel_id: int
    file_name: str
    file_size: int | None
    sha256: str | None


@dataclass(frozen=True)
class Decision:
    verdict: DeviceVerdict
    tier: MatchTier | None = None
    reason: str | None = None
    tg_message_id: int | None = None
    channel_id: int | None = None


def _archived(tier: MatchTier, candidate: Candidate) -> Decision:
    return Decision(
        verdict=DeviceVerdict.ARCHIVED,
        tier=tier,
        tg_message_id=candidate.tg_message_id,
        channel_id=candidate.channel_id,
    )


def decide(
    *,
    name: str,
    size: int,
    sha256: str | None,
    mtime: datetime | None,
    pipeline_status: PhotoStatus | None,
    candidates: Sequence[Candidate],
    catalog_newest: datetime | None,
    freshness_gate: bool = True,
) -> Decision:
    """The verdict for one local file. Same inputs, same answer, always."""
    # 1. What the pipeline knows, which the catalog cannot know.
    if pipeline_status is not None:
        if pipeline_status in IN_FLIGHT_STATUSES:
            return Decision(DeviceVerdict.IN_FLIGHT, reason="pipeline_in_progress")
        if pipeline_status == PhotoStatus.FAILED:
            return Decision(DeviceVerdict.NOT_ARCHIVED, reason="pipeline_failed")
        if pipeline_status == PhotoStatus.SKIPPED:
            return Decision(DeviceVerdict.NOT_ARCHIVED, reason="unsupported_type")
        # COMPLETED deliberately falls through: it is corroboration, not proof.

    # 2. Content proof. A matching hash promotes; a contradicting hash is
    # disproof and disqualifies that candidate from every tier below HASH —
    # a stale name+size match must never override positive evidence that the
    # bytes differ.
    contradicted: set[Candidate] = set()
    if sha256:
        for candidate in candidates:
            if candidate.sha256:
                if candidate.sha256 == sha256:
                    return _archived(MatchTier.HASH, candidate)
                contradicted.add(candidate)

    eligible = [c for c in candidates if c not in contradicted]
    exact_name = [c for c in eligible if c.file_name == name]

    # 3. Metadata inference, which a stale catalog can undermine — unless the
    # caller has explicitly opted out of that protection (freshness_gate=False),
    # because it has no mtime to judge staleness with in the first place.
    if size > 0:
        for candidate in exact_name:
            if candidate.file_size == size:
                if freshness_gate and _newer_than_catalog(mtime, catalog_newest):
                    return Decision(
                        DeviceVerdict.IN_FLIGHT, reason="catalog_older_than_file"
                    )
                return _archived(MatchTier.NAME_SIZE, candidate)

    # 4. Everything a human should look at before deciding.
    if size <= 0 and exact_name:
        return Decision(
            DeviceVerdict.AMBIGUOUS,
            reason="zero_byte_file",
            tg_message_id=exact_name[0].tg_message_id,
            channel_id=exact_name[0].channel_id,
        )
    if exact_name:
        return Decision(
            DeviceVerdict.AMBIGUOUS,
            reason="size_mismatch",
            tg_message_id=exact_name[0].tg_message_id,
            channel_id=exact_name[0].channel_id,
        )

    contradicted_exact = [c for c in candidates if c in contradicted and c.file_name == name]
    if contradicted_exact:
        disproved = contradicted_exact[0]
        return Decision(
            DeviceVerdict.AMBIGUOUS,
            reason="hash_mismatch",
            tg_message_id=disproved.tg_message_id,
            channel_id=disproved.channel_id,
        )

    lowered = name.lower()
    for candidate in candidates:
        if candidate.file_name.lower() == lowered:
            return Decision(
                DeviceVerdict.AMBIGUOUS,
                reason="case_only_match",
                tg_message_id=candidate.tg_message_id,
                channel_id=candidate.channel_id,
            )

    if pipeline_status == PhotoStatus.COMPLETED:
        return Decision(
            DeviceVerdict.AMBIGUOUS, reason="completed_but_absent_from_catalog"
        )

    return Decision(DeviceVerdict.NOT_ARCHIVED, reason="no_match")


def _newer_than_catalog(mtime: datetime | None, catalog_newest: datetime | None) -> bool:
    """Fail closed: an unknown date is treated as newer than anything scanned."""
    if catalog_newest is None:
        return True
    if mtime is None:
        return True
    if mtime.tzinfo is None:
        mtime = mtime.replace(tzinfo=timezone.utc)
    if catalog_newest.tzinfo is None:
        catalog_newest = catalog_newest.replace(tzinfo=timezone.utc)
    return mtime >= catalog_newest


def _parse_mtime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class SnapshotNotFound(RuntimeError):
    """No snapshot exists with the given id at all."""


class SnapshotConflict(RuntimeError):
    """A snapshot that exists, but belongs to a different device or is already closed."""


_VERDICT_COLUMNS = {
    DeviceVerdict.ARCHIVED: ("archived_files", "archived_bytes"),
    DeviceVerdict.IN_FLIGHT: ("in_flight_files", "in_flight_bytes"),
    DeviceVerdict.AMBIGUOUS: ("ambiguous_files", "ambiguous_bytes"),
    DeviceVerdict.NOT_ARCHIVED: ("not_archived_files", "not_archived_bytes"),
}


class ReconcileService:
    """Fetches candidates for an inventory and asks `decide` about each entry."""

    def __init__(
        self,
        *,
        max_entries: int = 10_000,
        fingerprint_bytes: int = 262_144,
        archive_channel_ids: Sequence[int] | None = None,
    ) -> None:
        self.max_entries = max_entries
        # A window wider than one Telegram stream chunk would be silently
        # truncated by the fingerprint, and this value is published to clients
        # as the window they must hash locally — so it is clamped here, once,
        # where the effective number is decided.
        self.fingerprint_bytes = max(1, min(fingerprint_bytes, STREAM_CHUNK_BYTES))
        # The archive channels this deployment is configured with. Knowing them
        # is what lets an archive channel that has never been scanned count as
        # a hole in the frontier rather than simply not existing.
        self.archive_channel_ids = tuple(archive_channel_ids or ())

    async def catalog_freshness(self) -> dict[str, object]:
        """How current the catalog is, per archive channel and overall.

        `frontier` is the **oldest** of the per-channel last-scanned times, not
        the newest message date anywhere. It gates the `NAME_SIZE` inference:
        an entry whose `mtime` is newer than the frontier cannot be trusted to
        have been seen by the scan that vouches for its channel. Two things
        follow from basing it on scan time rather than message date:

        - A dormant channel does not pin it. The iPhone migration's newest
          message never advances, but a scan of it today is still current; a
          message-date frontier would demote every newer local file forever.
        - A channel that has not been scanned since this column existed has no
          frontier (`NULL`), so the whole value is `None` and every metadata
          match fails closed until it is scanned. `channels` says which one.

        `newest_message_date` is kept per channel for information — it is how
        far the channel's own timeline reaches, which is not freshness.
        """
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(
                    CatalogItem.channel_id,
                    func.max(CatalogItem.message_date),
                    func.max(CatalogItem.scanned_at),
                    func.count(CatalogItem.id),
                )
                .where(CatalogItem.channel_role == ChannelRole.ARCHIVE)
                .group_by(CatalogItem.channel_id)
            )
            catalogued = {
                int(channel_id): (newest, last_scanned, int(rows or 0))
                for channel_id, newest, last_scanned, rows in result.all()
            }

        # Configured channels that hold no rows yet still belong in the answer.
        channel_ids = set(catalogued) | set(self.archive_channel_ids)
        per_channel = [
            {
                "channel_id": channel_id,
                "last_scanned_at": catalogued.get(channel_id, (None, None, 0))[1],
                "newest_message_date": catalogued.get(channel_id, (None, None, 0))[0],
                "rows": catalogued.get(channel_id, (None, None, 0))[2],
            }
            for channel_id in sorted(channel_ids)
        ]

        frontiers = [entry["last_scanned_at"] for entry in per_channel]
        oldest_frontier = (
            min(frontiers) if frontiers and all(f is not None for f in frontiers) else None
        )

        return {
            "frontier": oldest_frontier,
            "archive_rows": sum(entry["rows"] for entry in per_channel),
            "fingerprint_window_bytes": self.fingerprint_bytes,
            "channels": per_channel,
        }

    async def evaluate(
        self, entries: Sequence[dict], *, freshness_gate: bool = True
    ) -> list[Decision]:
        freshness = await self.catalog_freshness()
        if freshness["archive_rows"] == 0:
            raise CatalogNeverScanned(
                "No archive channel has been scanned. Run POST /api/catalog/scan first."
            )

        catalog_newest = freshness["frontier"]
        names = {str(entry.get("name") or "") for entry in entries}
        names.discard("")
        candidates = await self._candidates_for(names)
        statuses = await self._pipeline_statuses(names)

        decisions = []
        for entry in entries:
            name = str(entry.get("name") or "")
            decisions.append(
                decide(
                    name=name,
                    size=int(entry.get("size") or 0),
                    sha256=entry.get("sha256"),
                    mtime=_parse_mtime(entry.get("mtime")),
                    freshness_gate=freshness_gate,
                    pipeline_status=statuses.get(name),
                    candidates=candidates.get(name.lower(), ()),
                    catalog_newest=catalog_newest,
                )
            )
        return decisions

    async def reconcile(
        self,
        device_id: str,
        entries: Sequence[dict],
        *,
        snapshot_id: int | None = None,
        taken_at: datetime | None = None,
        final: bool = True,
    ) -> dict[str, object]:
        """Evaluate one batch, fold it into a snapshot, keep only what matters.

        ARCHIVED entries are returned and not stored: they are the bulk, and the
        only time anyone looks at one again is through the audit, once it is gone.

        The freshness gate stays on here — unlike GET /api/vault/lookup, this
        entry carries a real device mtime, so a stale-catalog demotion is
        meaningful and must not be silently skipped.
        """
        decisions = await self.evaluate(entries, freshness_gate=True)

        async with AsyncSessionLocal() as session:
            if snapshot_id is None:
                snapshot = DeviceSnapshot(device_id=device_id, taken_at=taken_at)
                session.add(snapshot)
                await session.flush()
            else:
                snapshot = await session.get(DeviceSnapshot, snapshot_id)
                if snapshot is None:
                    raise SnapshotNotFound(f"No snapshot {snapshot_id}.")
                if snapshot.device_id != device_id:
                    raise SnapshotConflict(
                        f"Snapshot {snapshot_id} does not belong to device {device_id!r}."
                    )
                if snapshot.completed_at is not None:
                    raise SnapshotConflict(f"Snapshot {snapshot_id} is already closed.")

            for entry, decision in zip(entries, decisions, strict=True):
                size = int(entry.get("size") or 0)
                snapshot.total_files += 1
                snapshot.total_bytes += size
                files_column, bytes_column = _VERDICT_COLUMNS[decision.verdict]
                setattr(snapshot, files_column, getattr(snapshot, files_column) + 1)
                setattr(snapshot, bytes_column, getattr(snapshot, bytes_column) + size)

                if decision.verdict is not DeviceVerdict.ARCHIVED:
                    session.add(
                        DeviceFinding(
                            snapshot_id=snapshot.id,
                            relpath=str(entry.get("relpath") or ""),
                            file_name=str(entry.get("name") or ""),
                            file_size=size,
                            verdict=decision.verdict,
                            reason=decision.reason,
                        )
                    )

            if final:
                snapshot.completed_at = datetime.now(timezone.utc)

            await session.commit()
            await session.refresh(snapshot)
            summary = _summary_of(snapshot)
            new_id = snapshot.id

        return {
            "snapshot_id": new_id,
            "catalog": await self.catalog_freshness(),
            "summary": summary,
            "entries": [
                {
                    "relpath": entry.get("relpath"),
                    "verdict": decision.verdict.value,
                    "tier": decision.tier.value if decision.tier else None,
                    "reason": decision.reason,
                    "channel_id": decision.channel_id,
                    "tg_message_id": decision.tg_message_id,
                }
                for entry, decision in zip(entries, decisions, strict=True)
            ],
        }

    async def latest_snapshot(self, device_id: str) -> dict[str, object] | None:
        async with AsyncSessionLocal() as session:
            snapshot = await session.scalar(
                select(DeviceSnapshot)
                .where(DeviceSnapshot.device_id == device_id)
                .order_by(DeviceSnapshot.id.desc())
                .limit(1)
            )
            if snapshot is None:
                return None
            findings = list(
                (
                    await session.scalars(
                        select(DeviceFinding)
                        .where(DeviceFinding.snapshot_id == snapshot.id)
                        .order_by(DeviceFinding.id)
                    )
                ).all()
            )
            return {
                "snapshot": {
                    "id": snapshot.id,
                    "device_id": snapshot.device_id,
                    "taken_at": snapshot.taken_at,
                    "completed_at": snapshot.completed_at,
                    "total_files": snapshot.total_files,
                    "total_bytes": snapshot.total_bytes,
                    "archived_files": snapshot.archived_files,
                    "archived_bytes": snapshot.archived_bytes,
                    "in_flight_files": snapshot.in_flight_files,
                    "ambiguous_files": snapshot.ambiguous_files,
                    "not_archived_files": snapshot.not_archived_files,
                },
                "findings": [
                    {
                        "relpath": f.relpath,
                        "file_name": f.file_name,
                        "file_size": f.file_size,
                        "verdict": f.verdict.value,
                        "reason": f.reason,
                    }
                    for f in findings
                ],
            }

    async def record_deletions(self, device_id: str, records: Sequence[dict]) -> int:
        """Write the permanent audit. This is what makes a deletion recoverable."""
        async with AsyncSessionLocal() as session:
            for record in records:
                session.add(
                    DeletionAudit(
                        device_id=device_id,
                        relpath=str(record["relpath"]),
                        file_name=str(record["name"]),
                        file_size=int(record["size"]),
                        tier=MatchTier(record["tier"]),
                        channel_id=int(record["channel_id"]),
                        tg_message_id=int(record["tg_message_id"]),
                        deleted_at=_parse_mtime(record.get("deleted_at")),
                    )
                )
            await session.commit()
        return len(records)

    async def _candidates_for(self, names: set[str]) -> dict[str, tuple[Candidate, ...]]:
        """Candidates keyed by lower-cased name, so a case-only match is findable.

        Two sources, both restricted to archive channels: ordinary documents,
        and resolved manifests standing in for the chunked original they
        describe. Native photos and chunk parts are excluded here rather than
        later, so no rule downstream can accidentally treat one as evidence.
        """
        if not names:
            return {}

        lowered = {n.lower() for n in names}
        found: dict[str, list[Candidate]] = {}

        async with AsyncSessionLocal() as session:
            documents = await session.scalars(
                select(CatalogItem).where(
                    CatalogItem.channel_role == ChannelRole.ARCHIVE,
                    CatalogItem.artifact.is_(None),
                    CatalogItem.media_kind != "photo",
                    func.lower(CatalogItem.file_name).in_(lowered),
                )
            )
            for row in documents:
                found.setdefault(row.file_name.lower(), []).append(
                    Candidate(
                        tg_message_id=row.tg_message_id,
                        channel_id=row.channel_id,
                        file_name=row.file_name,
                        file_size=row.file_size,
                        sha256=row.sha256,
                    )
                )

            manifests = await session.scalars(
                select(CatalogItem).where(
                    CatalogItem.channel_role == ChannelRole.ARCHIVE,
                    CatalogItem.artifact == "manifest",
                    CatalogItem.media_kind != "photo",
                    CatalogItem.chunked_original_name.isnot(None),
                    func.lower(CatalogItem.chunked_original_name).in_(lowered),
                )
            )
            for row in manifests:
                found.setdefault(row.chunked_original_name.lower(), []).append(
                    Candidate(
                        tg_message_id=row.tg_message_id,
                        channel_id=row.channel_id,
                        file_name=row.chunked_original_name,
                        file_size=row.chunked_total_size,
                        sha256=row.chunked_sha256,
                    )
                )

        return {key: tuple(value) for key, value in found.items()}

    async def _pipeline_statuses(self, names: set[str]) -> dict[str, PhotoStatus]:
        """The worker's state for each name, by the basename of its mega_path.

        `mega-ls -R` is recursive, so two subfolders can share a leaf name.
        The overlay exists to veto, so a veto must never be shadowed by
        corroboration: rows are walked in mega_path order and the first
        non-COMPLETED status for a basename wins. COMPLETED is kept only
        when every colliding row is COMPLETED.
        """
        if not names:
            return {}
        async with AsyncSessionLocal() as session:
            # Materialised inside the block: a Result is only iterable while
            # its session is open, and relying on the rows happening to be
            # buffered is a bug waiting for a driver change.
            rows = (
                await session.execute(
                    select(Photo.mega_path, Photo.status).order_by(Photo.mega_path)
                )
            ).all()
        statuses: dict[str, PhotoStatus] = {}
        for mega_path, status in rows:
            basename = mega_path.rsplit("/", 1)[-1]
            if basename not in names:
                continue
            existing = statuses.get(basename)
            if existing is None or (
                existing == PhotoStatus.COMPLETED and status != PhotoStatus.COMPLETED
            ):
                statuses[basename] = status
        return statuses


def _summary_of(snapshot: DeviceSnapshot) -> dict[str, dict[str, int]]:
    return {
        "ARCHIVED": {"files": snapshot.archived_files, "bytes": snapshot.archived_bytes},
        "IN_FLIGHT": {"files": snapshot.in_flight_files, "bytes": snapshot.in_flight_bytes},
        "AMBIGUOUS": {"files": snapshot.ambiguous_files, "bytes": snapshot.ambiguous_bytes},
        "NOT_ARCHIVED": {
            "files": snapshot.not_archived_files,
            "bytes": snapshot.not_archived_bytes,
        },
        "TOTAL": {"files": snapshot.total_files, "bytes": snapshot.total_bytes},
    }
