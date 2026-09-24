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
    DeviceVerdict,
    MatchTier,
    Photo,
    PhotoStatus,
)

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

    # 3. Metadata inference, which a stale catalog can undermine.
    if size > 0:
        for candidate in exact_name:
            if candidate.file_size == size:
                if _newer_than_catalog(mtime, catalog_newest):
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


class ReconcileService:
    """Fetches candidates for an inventory and asks `decide` about each entry."""

    async def catalog_freshness(self) -> dict[str, object]:
        """Newest archive-channel message the catalog has seen, and how many rows."""
        async with AsyncSessionLocal() as session:
            newest = await session.scalar(
                select(func.max(CatalogItem.message_date)).where(
                    CatalogItem.channel_role == ChannelRole.ARCHIVE
                )
            )
            rows = await session.scalar(
                select(func.count())
                .select_from(CatalogItem)
                .where(CatalogItem.channel_role == ChannelRole.ARCHIVE)
            )
        return {"newest_message_date": newest, "archive_rows": int(rows or 0)}

    async def evaluate(self, entries: Sequence[dict]) -> list[Decision]:
        freshness = await self.catalog_freshness()
        if freshness["archive_rows"] == 0:
            raise CatalogNeverScanned(
                "No archive channel has been scanned. Run POST /api/catalog/scan first."
            )

        catalog_newest = freshness["newest_message_date"]
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
                    pipeline_status=statuses.get(name),
                    candidates=candidates.get(name.lower(), ()),
                    catalog_newest=catalog_newest,
                )
            )
        return decisions

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
            rows = await session.execute(
                select(Photo.mega_path, Photo.status).order_by(Photo.mega_path)
            )
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
