from __future__ import annotations

import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select

from app.models.database import AsyncSessionLocal, Photo, PhotoStatus, RecoveryItem, RecoveryStatus
from app.services.recovery import RecoveryBusyError
from app.services.reconcile import (
    CatalogNeverScanned,
    ReconcileService,
    SnapshotConflict,
    _parse_mtime,
)

ERROR_LOG_PREVIEW_CHARS = 4000


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-Api-Key")) -> None:
    expected_api_key = os.getenv("API_KEY")
    if not expected_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API key is not configured.",
        )

    if not x_api_key or not secrets.compare_digest(x_api_key, expected_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )


router = APIRouter(prefix="/api", tags=["api"], dependencies=[Depends(require_api_key)])


def _get_worker(request: Request):
    return getattr(request.app.state, "worker", None)


def _get_recovery(request: Request):
    return getattr(request.app.state, "recovery", None)


async def _recovery_counts() -> dict[str, int]:
    counts: dict[str, int] = {recovery_status.value: 0 for recovery_status in RecoveryStatus}
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(RecoveryItem.status, func.count(RecoveryItem.id)).group_by(RecoveryItem.status)
        )
        for item_status, count in result.all():
            key = item_status.value if isinstance(item_status, RecoveryStatus) else str(item_status)
            counts[key] = int(count)
    return counts


@router.get("/status")
async def get_status(request: Request) -> dict[str, object]:
    status_counts: dict[str, int] = {photo_status.value: 0 for photo_status in PhotoStatus}

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Photo.status, func.count(Photo.id)).group_by(Photo.status)
        )

        for photo_status, count in result.all():
            key = photo_status.value if isinstance(photo_status, PhotoStatus) else str(photo_status)
            status_counts[key] = int(count)

    worker = _get_worker(request)
    recovery = _get_recovery(request)

    recovery_block = None
    if recovery is not None:
        recovery_block = recovery.status_snapshot()
        recovery_block["items"] = await _recovery_counts()

    return {
        "photos": status_counts,
        "worker": worker.status_snapshot() if worker is not None else None,
        "recovery": recovery_block,
    }


@router.post("/run")
async def run_now(request: Request) -> dict[str, object]:
    worker = _get_worker(request)
    if worker is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Worker is not running.",
        )

    worker.trigger()
    return {"triggered": True, "worker": worker.status_snapshot()}


def _photo_to_dict(photo: Photo) -> dict[str, object]:
    error_log = photo.error_log
    if error_log and len(error_log) > ERROR_LOG_PREVIEW_CHARS:
        error_log = error_log[-ERROR_LOG_PREVIEW_CHARS:]

    return {
        "id": photo.id,
        "mega_path": photo.mega_path,
        "status": photo.status.value,
        "media_type": photo.media_type.value,
        "failed_status": photo.failed_status.value if photo.failed_status else None,
        "tg_message_id": photo.tg_message_id,
        "retry_count": photo.retry_count,
        "error_log": error_log,
        "created_at": photo.created_at.isoformat() if photo.created_at else None,
        "updated_at": photo.updated_at.isoformat() if photo.updated_at else None,
    }


@router.get("/photos")
async def list_photos(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    query = select(Photo)
    count_query = select(func.count(Photo.id))

    if status_filter is not None:
        try:
            wanted = PhotoStatus(status_filter)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown status: {status_filter}")
        query = query.where(Photo.status == wanted)
        count_query = count_query.where(Photo.status == wanted)

    async with AsyncSessionLocal() as session:
        total = (await session.execute(count_query)).scalar_one()
        result = await session.scalars(
            query.order_by(Photo.updated_at.desc(), Photo.id.desc()).limit(limit).offset(offset)
        )
        items = [_photo_to_dict(photo) for photo in result]

    return {"total": int(total), "limit": limit, "offset": offset, "items": items}


def _resume_status(photo: Photo) -> PhotoStatus:
    if photo.failed_status is not None:
        resume = photo.failed_status
    elif photo.compressed_path:
        # Legacy rows without failed_status: infer the resume point from what exists.
        resume = PhotoStatus.COMPRESSED
    elif photo.tg_message_id:
        resume = PhotoStatus.TG_UPLOADED
    elif photo.local_path:
        resume = PhotoStatus.DOWNLOADED
    else:
        resume = PhotoStatus.PENDING

    # Walk back when a step's prerequisite file no longer exists on disk.
    if resume == PhotoStatus.COMPRESSED and not (
        photo.compressed_path and Path(photo.compressed_path).is_file()
    ):
        resume = PhotoStatus.TG_UPLOADED
    if resume in (
        PhotoStatus.DOWNLOADED,
        PhotoStatus.CHUNK_UPLOADING,
        PhotoStatus.TG_UPLOADED,
    ) and not (photo.local_path and Path(photo.local_path).is_file()):
        resume = PhotoStatus.PENDING
    return resume


@router.post("/photos/{photo_id}/retry")
async def retry_photo(photo_id: int, request: Request) -> dict[str, object]:
    async with AsyncSessionLocal() as session:
        photo = await session.get(Photo, photo_id)
        if photo is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo not found.")
        if photo.status != PhotoStatus.FAILED:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Photo is {photo.status.value}, only FAILED photos can be retried.",
            )

        photo.status = _resume_status(photo)
        photo.failed_status = None
        photo.retry_count = 0
        photo.error_log = None
        await session.commit()
        # The server-side onupdate expires updated_at on commit; refresh explicitly
        # so serialization below doesn't trigger a sync lazy load.
        await session.refresh(photo)
        payload = _photo_to_dict(photo)

    worker = _get_worker(request)
    if worker is not None:
        worker.trigger()

    return payload


class RecoveryRunRequest(BaseModel):
    dry_run: bool = True
    # Optional per-request overrides; fall back to the service's configured batch.
    limit: int | None = None
    max_download_bytes: int | None = None


def _require_recovery(request: Request):
    recovery = _get_recovery(request)
    if recovery is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Recovery service is not available.",
        )
    return recovery


@router.post("/recovery/scan")
async def recovery_scan(request: Request) -> dict[str, object]:
    recovery = _require_recovery(request)
    try:
        recovery.start_scan()
    except RecoveryBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return recovery.status_snapshot()


@router.post("/recovery/run")
async def recovery_run(request: Request, payload: RecoveryRunRequest | None = None) -> dict[str, object]:
    recovery = _require_recovery(request)
    dry_run = payload.dry_run if payload is not None else True
    limit = payload.limit if payload is not None else None
    max_download_bytes = payload.max_download_bytes if payload is not None else None
    try:
        recovery.start_run(dry_run, limit=limit, max_download_bytes=max_download_bytes)
    except RecoveryBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return recovery.status_snapshot()


class RecoveryBackfillRequest(BaseModel):
    limit: int | None = None
    # Skip native videos larger than this (bytes). None = copy all sizes.
    max_video_bytes: int | None = None


@router.post("/recovery/backfill")
async def recovery_backfill(
    request: Request, payload: RecoveryBackfillRequest | None = None
) -> dict[str, object]:
    recovery = _require_recovery(request)
    limit = payload.limit if payload is not None else None
    max_video_bytes = payload.max_video_bytes if payload is not None else None
    try:
        recovery.start_backfill(limit=limit, max_video_bytes=max_video_bytes)
    except RecoveryBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return recovery.status_snapshot()


@router.get("/recovery/items")
async def recovery_items(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    query = select(RecoveryItem)
    count_query = select(func.count(RecoveryItem.id))

    if status_filter is not None:
        try:
            wanted = RecoveryStatus(status_filter)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown status: {status_filter}")
        query = query.where(RecoveryItem.status == wanted)
        count_query = count_query.where(RecoveryItem.status == wanted)

    async with AsyncSessionLocal() as session:
        total = (await session.execute(count_query)).scalar_one()
        result = await session.scalars(
            query.order_by(RecoveryItem.id.desc()).limit(limit).offset(offset)
        )
        items = [
            {
                "id": item.id,
                "tg_message_id": item.tg_message_id,
                "media_kind": item.media_kind,
                "file_name": item.file_name,
                "file_size": item.file_size,
                "message_date": item.message_date.isoformat() if item.message_date else None,
                "status": item.status.value,
                "sha256": item.sha256,
                "planned_caption": item.planned_caption,
                "new_tg_message_id": item.new_tg_message_id,
                "retry_count": item.retry_count,
                "error_log": (item.error_log or None)
                and item.error_log[-ERROR_LOG_PREVIEW_CHARS:],
            }
            for item in result
        ]

    return {"total": int(total), "limit": limit, "offset": offset, "items": items}


@router.get("/system")
async def get_system() -> dict[str, int | float | str]:
    data_path = Path(os.getenv("DATA_VOLUME_PATH", "/data"))
    usage_path = data_path if data_path.exists() else Path("/")
    usage = shutil.disk_usage(usage_path)
    used_percent = (usage.used / usage.total * 100.0) if usage.total else 0.0

    return {
        "path": str(usage_path),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_percent": round(used_percent, 2),
    }


class ReconcileEntry(BaseModel):
    relpath: str
    name: str
    size: int
    mtime: str | None = None
    sha256: str | None = None


class ReconcileRequest(BaseModel):
    entries: list[ReconcileEntry]
    snapshot_id: int | None = None
    taken_at: str | None = None
    # False while a client is sending a large library in several calls.
    final: bool = True


class DeletionRecord(BaseModel):
    relpath: str
    name: str
    size: int
    tier: str
    channel_id: int
    tg_message_id: int
    deleted_at: str | None = None


class DeletionsRequest(BaseModel):
    deleted: list[DeletionRecord]


class VerifyRequest(BaseModel):
    channel_id: int
    tg_message_id: int
    file_size: int
    head_sha256: str
    tail_sha256: str


def _require_reconcile(request: Request) -> ReconcileService:
    service = getattr(request.app.state, "reconcile", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reconciliation service is not available.",
        )
    return service


@router.get("/catalog/freshness")
async def catalog_freshness(request: Request) -> dict[str, object]:
    return await _require_reconcile(request).catalog_freshness()


@router.get("/vault/lookup")
async def vault_lookup(
    request: Request,
    name: str = Query(..., min_length=1),
    size: int = Query(...),
) -> dict[str, object]:
    service = _require_reconcile(request)
    entry = {
        "relpath": name,
        "name": name,
        "size": size,
        # This endpoint takes no local mtime, unlike a device's real inventory
        # entries. decide() fails closed on an unknown mtime (treats it as
        # newer than the catalog, to protect a real reconcile call from a
        # stale NAME_SIZE match) — with mtime always None that gate would fire
        # on every call and a plain name+size match could never come back
        # ARCHIVED. Reporting the epoch instead means "not newer than the
        # catalog" and lets a genuine match through; this lookup is
        # informational only and never authorises a deletion by itself.
        "mtime": datetime(1970, 1, 1, tzinfo=timezone.utc).isoformat(),
        "sha256": None,
    }
    try:
        [decision] = await service.evaluate([entry])
    except CatalogNeverScanned as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return {
        "verdict": decision.verdict.value,
        "tier": decision.tier.value if decision.tier else None,
        "reason": decision.reason,
        "channel_id": decision.channel_id,
        "tg_message_id": decision.tg_message_id,
    }


@router.post("/devices/{device_id}/reconcile")
async def device_reconcile(
    device_id: str, request: Request, payload: ReconcileRequest
) -> dict[str, object]:
    service = _require_reconcile(request)
    if len(payload.entries) > service.max_entries:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"{len(payload.entries)} entries exceeds the limit of "
                f"{service.max_entries}. Send the inventory in several calls, "
                f"passing the snapshot_id returned by the first."
            ),
        )
    try:
        return await service.reconcile(
            device_id,
            [entry.model_dump() for entry in payload.entries],
            snapshot_id=payload.snapshot_id,
            taken_at=_parse_mtime(payload.taken_at),
            final=payload.final,
        )
    except CatalogNeverScanned as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except SnapshotConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/devices/{device_id}/snapshot")
async def device_snapshot(device_id: str, request: Request) -> dict[str, object]:
    snapshot = await _require_reconcile(request).latest_snapshot(device_id)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No snapshot recorded for device {device_id!r}.",
        )
    return snapshot


@router.post("/devices/{device_id}/deletions")
async def device_deletions(
    device_id: str, request: Request, payload: DeletionsRequest
) -> dict[str, int]:
    service = _require_reconcile(request)
    recorded = await service.record_deletions(
        device_id, [record.model_dump() for record in payload.deleted]
    )
    return {"recorded": recorded}


def _require_catalog(request: Request):
    catalog = getattr(request.app.state, "catalog", None)
    if catalog is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Catalog service is not available.",
        )
    return catalog


@router.post("/catalog/scan")
async def catalog_scan(request: Request) -> dict[str, object]:
    catalog = _require_catalog(request)
    results = await catalog.scan_all()
    matched = await catalog.match_all(os.getenv("BACKUP_STATE_DB") or None)
    return {"scanned": results, "matched": matched}


@router.post("/catalog/resolve-manifests")
async def catalog_resolve_manifests(
    request: Request, limit: int = Query(default=50, ge=1, le=500)
) -> dict[str, int]:
    """Give chunked originals their identity, so files above 2 GB become findable."""
    return await _require_catalog(request).resolve_manifests(limit=limit)


@router.post("/vault/verify")
async def vault_verify(request: Request, payload: VerifyRequest) -> dict[str, object]:
    """Settle one AMBIGUOUS entry against the archived copy, for ~2 MiB of traffic."""
    service = _require_reconcile(request)
    telegram = getattr(request.app.state, "telegram", None)
    if telegram is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram service is not available.",
        )
    try:
        archived = await telegram.fingerprint_message(
            payload.channel_id,
            payload.tg_message_id,
            file_size=payload.file_size,
            window=service.fingerprint_bytes,
        )
    except AttributeError:
        # The client's real failure mode for a missing/deleted message: get_messages
        # returns something stream_media cannot read, and it blows up with a bare
        # AttributeError rather than a purpose-built error. Turn it into a 404.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Message {payload.tg_message_id} in channel {payload.channel_id} "
                "was not found or has been deleted."
            ),
        )
    matched = (
        archived["head_sha256"] == payload.head_sha256
        and archived["tail_sha256"] == payload.tail_sha256
    )
    return {"match": matched, **archived}
