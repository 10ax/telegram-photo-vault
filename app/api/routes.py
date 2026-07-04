from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import func, select

from app.models.database import AsyncSessionLocal, Photo, PhotoStatus

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
    return {
        "photos": status_counts,
        "worker": worker.status_snapshot() if worker is not None else None,
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
        return photo.failed_status
    # Legacy rows without failed_status: infer the resume point from what exists.
    if photo.compressed_path:
        return PhotoStatus.COMPRESSED
    if photo.tg_message_id:
        return PhotoStatus.TG_UPLOADED
    if photo.local_path:
        return PhotoStatus.DOWNLOADED
    return PhotoStatus.PENDING


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
