"""One-off migration: upload a local folder to a new Telegram channel, verify by
re-download, report status. Run via docker compose (see README/spec). Nothing here
deletes source files.

Design: docs/superpowers/specs/2026-07-30-iphone-backup-telegram-design.md
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pyrogram import Client

logger = logging.getLogger("backup_local_folder")

DEFAULT_CHUNK_THRESHOLD = 1_950_000_000
DEFAULT_CHUNK_SIZE = 1_900_000_000
CHANNEL_TITLE = "iPhone Backup Archive"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_state_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            rel_path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            sha256 TEXT,
            status TEXT NOT NULL,
            is_chunked INTEGER NOT NULL DEFAULT 0,
            chunk_count INTEGER,
            tg_message_id INTEGER,
            manifest_tg_message_id INTEGER,
            error TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.commit()
    return conn


def scan_folder(conn: sqlite3.Connection, source_root: Path) -> int:
    inserted = 0
    now = _now_iso()
    for path in sorted(source_root.rglob("*")):
        if not path.is_file():
            continue
        rel_path = str(path.relative_to(source_root))
        size = path.stat().st_size
        cursor = conn.execute(
            "INSERT OR IGNORE INTO files (rel_path, size, status, updated_at) "
            "VALUES (?, ?, 'PENDING', ?)",
            (rel_path, size, now),
        )
        if cursor.rowcount:
            inserted += 1
    conn.commit()
    return inserted


def get_row(conn: sqlite3.Connection, rel_path: str) -> dict:
    cursor = conn.execute(
        "SELECT status, size, sha256, is_chunked, chunk_count, tg_message_id, "
        "manifest_tg_message_id, error FROM files WHERE rel_path = ?",
        (rel_path,),
    )
    row = cursor.fetchone()
    columns = [d[0] for d in cursor.description]
    return dict(zip(columns, row))


def set_status(conn: sqlite3.Connection, rel_path: str, status: str, **fields) -> None:
    fields["status"] = status
    fields["updated_at"] = _now_iso()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(
        f"UPDATE files SET {assignments} WHERE rel_path = ?",
        (*fields.values(), rel_path),
    )
    conn.commit()


def pending_rel_paths(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT rel_path FROM files WHERE status != 'VERIFIED' ORDER BY rel_path"
    ).fetchall()
    return [row[0] for row in rows]


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT status, COUNT(*) FROM files GROUP BY status").fetchall()
    return dict(rows)


def failed_rows(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT rel_path, error FROM files WHERE status = 'FAILED' ORDER BY rel_path"
    ).fetchall()
    return [(rel_path, error or "") for rel_path, error in rows]


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def build_client() -> Client:
    kwargs: dict[str, object] = {
        "name": "iphone_backup_migration",
        "api_id": int(_required_env("TELEGRAM_API_ID")),
        "api_hash": _required_env("TELEGRAM_API_HASH"),
        "sleep_threshold": int(os.getenv("TELEGRAM_SLEEP_THRESHOLD", "60")),
    }
    session_string = _optional_env("TELEGRAM_SESSION_STRING")
    if session_string:
        kwargs["session_string"] = session_string
    return Client(**kwargs)


async def get_or_create_channel_id(client, conn: sqlite3.Connection) -> int:
    existing = get_meta(conn, "channel_id")
    if existing is not None:
        return int(existing)
    channel = await client.create_channel(CHANNEL_TITLE)
    set_meta(conn, "channel_id", str(channel.id))
    return channel.id


def build_caption(rel_path: str, size: int, sha256: str) -> str:
    return f"{rel_path}\nsize={size} sha256={sha256[:16]}"


async def upload_single(service, abs_path: Path, rel_path: str, size: int, sha256: str) -> int:
    message = await service.upload_document(
        abs_path,
        caption=build_caption(rel_path, size, sha256),
        file_name=abs_path.name,
    )
    return message.id
