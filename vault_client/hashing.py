"""Whole-file and end-window hashes used as archive evidence."""
from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1024 * 1024


def full_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def head_tail_sha256(path: str | Path, window: int) -> tuple[str, str]:
    """SHA-256 of the first and last `window` bytes.

    For a file at most `window` long both are the whole file, matching what the
    server computes for the archived copy.
    """
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as handle:
        head_block = handle.read(max(1, window))
        head = hashlib.sha256(head_block).hexdigest()
        if size <= window:
            return head, head
        handle.seek(size - window)
        tail = hashlib.sha256(handle.read(window)).hexdigest()
    return head, tail
