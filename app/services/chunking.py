from __future__ import annotations

import hashlib
import io
import os
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_VERSION = 1
MANIFEST_KIND = "telegram-photo-vault/chunked-file"
MANIFEST_SUFFIX = ".manifest.json"

DEFAULT_CHUNK_SIZE = 1_900_000_000
READ_BLOCK = 4 * 1024 * 1024


def plan_chunks(total_size: int, chunk_size: int) -> list[dict[str, int]]:
    if total_size <= 0:
        raise ValueError("total_size must be positive")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    chunks = []
    offset = 0
    index = 1
    while offset < total_size:
        size = min(chunk_size, total_size - offset)
        chunks.append({"index": index, "offset": offset, "size": size})
        offset += size
        index += 1
    return chunks


def chunk_name(base_name: str, index: int, count: int) -> str:
    pad = max(3, len(str(count)))
    return f"{base_name}.part{index:0{pad}d}-of-{count:0{pad}d}"


def manifest_name(base_name: str) -> str:
    return f"{base_name}{MANIFEST_SUFFIX}"


def compute_hashes(path: str | Path, chunk_size: int) -> tuple[str, list[str]]:
    """One streaming pass: whole-file SHA-256 plus a SHA-256 per chunk_size window."""
    whole = hashlib.sha256()
    chunk_hashes: list[str] = []
    current = hashlib.sha256()
    in_chunk = 0

    with open(path, "rb") as handle:
        while True:
            remaining = chunk_size - in_chunk
            block = handle.read(min(READ_BLOCK, remaining))
            if not block:
                break
            whole.update(block)
            current.update(block)
            in_chunk += len(block)
            if in_chunk == chunk_size:
                chunk_hashes.append(current.hexdigest())
                current = hashlib.sha256()
                in_chunk = 0

    if in_chunk:
        chunk_hashes.append(current.hexdigest())

    return whole.hexdigest(), chunk_hashes


class ChunkWindow(io.RawIOBase):
    """Read-only file-like view over a byte range of a file.

    Exposes .name, seek/tell/read within the window, which is exactly what
    Pyrogram's save_file needs to stream one chunk without a temp copy.
    """

    def __init__(self, path: str | Path, offset: int, length: int, name: str) -> None:
        super().__init__()
        self._file = open(path, "rb")
        self._offset = offset
        self._length = length
        self._pos = 0
        self._name = name
        self._file.seek(offset)

    @property
    def name(self) -> str:
        return self._name

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, position: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            target = position
        elif whence == os.SEEK_CUR:
            target = self._pos + position
        elif whence == os.SEEK_END:
            target = self._length + position
        else:
            raise ValueError(f"Unsupported whence: {whence}")

        self._pos = min(max(target, 0), self._length)
        self._file.seek(self._offset + self._pos)
        return self._pos

    def read(self, size: int = -1) -> bytes:
        remaining = self._length - self._pos
        if remaining <= 0:
            return b""
        if size is None or size < 0 or size > remaining:
            size = remaining
        data = self._file.read(size)
        self._pos += len(data)
        return data

    def close(self) -> None:
        try:
            self._file.close()
        finally:
            super().close()


def build_manifest(
    *,
    original_filename: str,
    total_size: int,
    sha256: str,
    chunk_size: int,
    chunks: list[dict[str, object]],
    mega_path: str | None,
    mtime_utc: datetime | None,
    capture_datetime: datetime | None,
    capture_datetime_source: str | None,
) -> dict[str, object]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "kind": MANIFEST_KIND,
        "original_filename": original_filename,
        "total_size": total_size,
        "sha256": sha256,
        "chunk_size": chunk_size,
        "chunk_count": len(chunks),
        "chunks": chunks,
        "source": {
            "mega_path": mega_path,
            "mtime_utc": mtime_utc.isoformat() if mtime_utc else None,
            "capture_datetime": capture_datetime.isoformat() if capture_datetime else None,
            "capture_datetime_source": capture_datetime_source,
        },
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "tool": "telegram-photo-vault",
    }


def build_chunk_caption(
    date_caption: str,
    *,
    index: int,
    count: int,
    original_filename: str,
    total_size: int,
    sha256: str,
) -> str:
    pad = max(3, len(str(count)))
    return (
        f"{date_caption}\n"
        f"#chunked #part{index:0{pad}d}_of_{count:0{pad}d}\n"
        f"file={original_filename} size={total_size} sha256={sha256[:16]}"
    )


def build_manifest_caption(date_caption: str, *, original_filename: str) -> str:
    return f"{date_caption}\n#manifest\nfile={original_filename}"
