from __future__ import annotations

import os
import secrets
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import func, select

from app.models.database import AsyncSessionLocal, Photo, PhotoStatus


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


@router.get("/status")
async def get_status() -> dict[str, int]:
    status_counts: dict[str, int] = {status.value: 0 for status in PhotoStatus}

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Photo.status, func.count(Photo.id)).group_by(Photo.status)
        )

        for status, count in result.all():
            key = status.value if isinstance(status, PhotoStatus) else str(status)
            status_counts[key] = int(count)

    return status_counts


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
