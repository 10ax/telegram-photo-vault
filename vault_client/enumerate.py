"""Walk the phone's media roots into inventory entries.

The extension sets are copied from app/services/media.py so the client stays
standard-library-only on the phone; tests pin them to the server's copy so they
cannot drift.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

IMAGE_EXTENSIONS = frozenset(
    {".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
)
VIDEO_EXTENSIONS = frozenset(
    {".3gp", ".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".mts", ".webm", ".wmv"}
)


@dataclass(frozen=True)
class Entry:
    relpath: str
    name: str
    size: int
    mtime: datetime  # timezone-aware, UTC


def is_media(name: str) -> bool:
    suffix = Path(name).suffix.lower()
    return suffix in IMAGE_EXTENSIONS or suffix in VIDEO_EXTENSIONS


def relpath_for(path: str | Path, sdcard: str | Path) -> str:
    return Path(os.path.relpath(path, sdcard)).as_posix()


def enumerate_entries(roots: Sequence[str | Path], *, sdcard: str | Path = Path("/sdcard")) -> list[Entry]:
    entries: list[Entry] = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name.startswith(".") or not is_media(name):
                    continue
                path = Path(dirpath) / name
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                entries.append(Entry(
                    relpath=relpath_for(path, sdcard), name=name, size=stat.st_size,
                    mtime=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                ))
    entries.sort(key=lambda e: e.relpath)
    return entries


def entry_to_manifest(entry: Entry, sha256: str | None = None) -> dict[str, object]:
    return {
        "relpath": entry.relpath, "name": entry.name, "size": entry.size,
        "mtime": entry.mtime.astimezone(timezone.utc).isoformat(), "sha256": sha256,
    }
