"""One-off migration: upload a local folder to a new Telegram channel, verify by
re-download, report status. Run via docker compose (see README/spec). Nothing here
deletes source files.

Design: docs/superpowers/specs/2026-07-30-iphone-backup-telegram-design.md
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pyrogram import Client
from pyrogram.errors import FloodWait

from app.services.chunking import (
    ChunkWindow,
    build_chunk_caption,
    build_manifest,
    build_manifest_caption,
    chunk_name,
    compute_hashes,
    manifest_name,
    plan_chunks,
)
from app.services.telegram import TelegramService

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


async def upload_chunked(
    service,
    abs_path: Path,
    rel_path: str,
    size: int,
    sha256: str,
    chunk_size: int,
    chunk_hashes: list[str],
):
    base_name = Path(rel_path).name
    plan = plan_chunks(size, chunk_size)
    count = len(plan)
    chunk_records = []
    chunk_messages = []

    for spec in plan:
        name = chunk_name(base_name, spec["index"], count)
        window = ChunkWindow(abs_path, spec["offset"], spec["size"], name)
        try:
            caption = build_chunk_caption(
                rel_path,
                index=spec["index"],
                count=count,
                original_filename=base_name,
                total_size=size,
                sha256=sha256,
            )
            message = await service.upload_file_object(window, caption)
        finally:
            window.close()
        chunk_messages.append(message)
        chunk_records.append(
            {
                "index": spec["index"],
                "filename": name,
                "offset": spec["offset"],
                "size": spec["size"],
                "sha256": chunk_hashes[spec["index"] - 1],
            }
        )

    manifest = build_manifest(
        original_filename=base_name,
        total_size=size,
        sha256=sha256,
        chunk_size=chunk_size,
        chunks=chunk_records,
        mega_path=None,
        mtime_utc=None,
        capture_datetime=None,
        capture_datetime_source=None,
    )
    manifest_message = await service.upload_bytes(
        json.dumps(manifest, indent=2).encode(),
        file_name=manifest_name(base_name),
        caption=build_manifest_caption(rel_path, original_filename=base_name),
    )
    return manifest_message, count, chunk_messages


_HASH_BLOCK_SIZE = 4 * 1024 * 1024


async def _download_and_hash(client, message, tmp_dir: Path) -> str:
    tmp_path = tmp_dir / f"verify_{message.id}"
    try:
        await client.download_media(message, file_name=str(tmp_path))
        hasher = hashlib.sha256()
        with open(tmp_path, "rb") as handle:
            for block in iter(lambda: handle.read(_HASH_BLOCK_SIZE), b""):
                hasher.update(block)
        return hasher.hexdigest()
    finally:
        # try/finally, not a plain unlink() after the read: a mid-transfer exception
        # (FloodWait, network blip, disk full) must not leave a partial file behind —
        # this runs for hours over 106GB, so at-least-one interruption is expected.
        tmp_path.unlink(missing_ok=True)


async def verify_single(
    client, channel_id: int, message_id: int, expected_sha256: str, tmp_dir: Path
) -> bool:
    message = await client.get_messages(channel_id, message_id)
    digest = await _download_and_hash(client, message, tmp_dir)
    return digest == expected_sha256


async def verify_chunked(client, chunk_messages: list, expected_sha256: str, tmp_dir: Path) -> bool:
    hasher = hashlib.sha256()
    for message in chunk_messages:
        tmp_path = tmp_dir / f"verify_{message.id}"
        try:
            await client.download_media(message, file_name=str(tmp_path))
            with open(tmp_path, "rb") as handle:
                for block in iter(lambda: handle.read(_HASH_BLOCK_SIZE), b""):
                    hasher.update(block)
        finally:
            tmp_path.unlink(missing_ok=True)
    return hasher.hexdigest() == expected_sha256


async def process_file(
    client,
    service,
    conn: sqlite3.Connection,
    source_root: Path,
    rel_path: str,
    channel_id: int,
    chunk_threshold: int,
    chunk_size: int,
    tmp_verify_dir: Path,
) -> None:
    row = get_row(conn, rel_path)
    abs_path = source_root / rel_path
    status = row["status"]
    size = row["size"]
    sha256 = row["sha256"]
    tg_message_id = row["tg_message_id"]

    try:
        # Ensure the verify scratch dir exists before any verify_* call needs to write
        # into it. Cheap and idempotent, so doing it per-file (rather than once in
        # main()) also makes process_file callable standalone, as the tests do.
        tmp_verify_dir.mkdir(parents=True, exist_ok=True)

        if status == "PENDING":
            sha256, _ = compute_hashes(abs_path, chunk_size)
            set_status(conn, rel_path, "HASHED", sha256=sha256)
            status = "HASHED"

        if status == "HASHED":
            if size > chunk_threshold:
                # Recomputed rather than threaded from the PENDING branch above: a run
                # resumed after a crash enters here with status already HASHED (no
                # PENDING step this call), so chunk_hashes must be derived fresh either
                # way. The extra pass over the file only affects the handful of files
                # above chunk_threshold, and is negligible next to their upload+verify
                # time.
                _, chunk_hashes = compute_hashes(abs_path, chunk_size)
                manifest_message, count, chunk_messages = await upload_chunked(
                    service, abs_path, rel_path, size, sha256, chunk_size, chunk_hashes
                )
                ok = await verify_chunked(client, chunk_messages, sha256, tmp_verify_dir)
                set_status(
                    conn,
                    rel_path,
                    "VERIFIED" if ok else "FAILED",
                    is_chunked=1,
                    chunk_count=count,
                    manifest_tg_message_id=manifest_message.id,
                    error=None if ok else "hash mismatch on verify (chunked)",
                )
                return

            tg_message_id = await upload_single(service, abs_path, rel_path, size, sha256)
            set_status(conn, rel_path, "UPLOADED", tg_message_id=tg_message_id)
            status = "UPLOADED"

        if status == "UPLOADED":
            ok = await verify_single(client, channel_id, tg_message_id, sha256, tmp_verify_dir)
            set_status(
                conn,
                rel_path,
                "VERIFIED" if ok else "FAILED",
                error=None if ok else "hash mismatch on verify",
            )
    except FloodWait:
        # Longer than the client's sleep_threshold — propagate and end the run rather
        # than mark this (and every subsequent) file FAILED against a rate limit that
        # hasn't cleared yet. Rerun resumes cleanly from the DB state.
        raise
    except Exception as exc:  # one bad file must never stop the run
        logger.exception("failed processing %s", rel_path)
        set_status(conn, rel_path, "FAILED", error=str(exc))


def print_report(conn: sqlite3.Connection) -> None:
    counts = status_counts(conn)
    print(" ".join(f"{status}: {count}" for status, count in sorted(counts.items())))
    channel_id = get_meta(conn, "channel_id")
    if channel_id:
        print(f"Channel: {channel_id}")
    failures = failed_rows(conn)
    if failures:
        print("Failed files:")
        for rel_path, error in failures:
            print(f"  {rel_path} — {error}")


async def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Migrate a local folder to a new, dedicated Telegram channel."
    )
    parser.add_argument("--source", default="/backup-source")
    parser.add_argument("--state-db", default="/data/iphone_backup_state.db")
    parser.add_argument("--tmp-verify-dir", default="/data/tmp-verify")
    parser.add_argument(
        "--delay", type=float, default=float(os.getenv("TELEGRAM_UPLOAD_DELAY", "5"))
    )
    parser.add_argument(
        "--chunk-threshold",
        type=int,
        default=int(os.getenv("CHUNK_THRESHOLD", str(DEFAULT_CHUNK_THRESHOLD))),
    )
    parser.add_argument(
        "--chunk-size", type=int, default=int(os.getenv("CHUNK_SIZE", str(DEFAULT_CHUNK_SIZE)))
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Scan and report counts without connecting to Telegram",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    source_root = Path(args.source)
    tmp_verify_dir = Path(args.tmp_verify_dir)

    conn = open_state_db(Path(args.state_db))
    inserted = scan_folder(conn, source_root)
    logger.info("scan complete: %d new file(s) tracked", inserted)

    if args.scan_only:
        print_report(conn)
        return

    # Deferred until past the scan-only early-return: --scan-only must not touch the
    # filesystem beyond --state-db, even when --tmp-verify-dir is left at its
    # /data-rooted default. process_file() creates it again per-file regardless
    # (needed there since resumed/direct calls don't go through main() at all); the
    # call here just fails fast before opening the Telegram client.
    tmp_verify_dir.mkdir(parents=True, exist_ok=True)

    client = build_client()
    try:
        async with client:
            channel_id = await get_or_create_channel_id(client, conn)
            service = TelegramService(client, channel_id, upload_delay_seconds=args.delay)

            for rel_path in pending_rel_paths(conn):
                await process_file(
                    client,
                    service,
                    conn,
                    source_root,
                    rel_path,
                    channel_id,
                    args.chunk_threshold,
                    args.chunk_size,
                    tmp_verify_dir,
                )
    finally:
        # Always report — on a clean finish, a long FloodWait abort, or Ctrl-C.
        print_report(conn)


if __name__ == "__main__":
    asyncio.run(main())
