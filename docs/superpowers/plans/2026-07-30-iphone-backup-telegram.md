# iPhone Backup → Telegram Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone, resumable script that uploads `/home/tenax/Pictures/iPhone backup/` (106 GB, 6916 files) to a newly-created Telegram channel, verifies every upload by re-downloading and re-hashing it, and reports status — so the user can manually delete the local folder once the report shows zero failures.

**Architecture:** One new file, `scripts/backup_local_folder.py`, built incrementally task-by-task, reusing `app/services/chunking.py` and `app/services/telegram.py` from the existing app. State lives in a small SQLite table (own file, not the app's DB) so the script is safely re-runnable after any interruption. Run via `docker compose run --rm --entrypoint python` (bypassing the MEGA-login entrypoint) against the production image; unit tests run directly against the repo's `.venv` with fakes, no real Telegram calls.

**Tech Stack:** Python 3.11, kurigram (pyrogram-compatible), stdlib `sqlite3`, pytest (`asyncio_mode = auto`).

## Global Constraints

- No automatic deletion of source files — the script only uploads, verifies, and reports (spec: Non-goals).
- No changes to `docker-compose.yml` or the `Dockerfile` — the script and its dependencies are bind-mounted at run time (spec: Architecture).
- Chunk threshold/size default to the existing project's conventions: `CHUNK_THRESHOLD=1950000000`, `CHUNK_SIZE=1900000000` (env-overridable, same names as the production app).
- Verification is always a full re-download + SHA-256 compare — no size-only shortcut (spec: Verification depth, approved).
- Reuse `app/services/chunking.py` and `app/services/telegram.py` as-is; do not modify them.

Full design context: `docs/superpowers/specs/2026-07-30-iphone-backup-telegram-design.md`.

---

### Task 1: State database (scan, status transitions, reporting queries)

**Files:**
- Create: `scripts/backup_local_folder.py`
- Create: `tests/test_backup_local_folder.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces (for later tasks):
  - `open_state_db(path: Path) -> sqlite3.Connection`
  - `scan_folder(conn, source_root: Path) -> int` — inserts new files as `PENDING`, returns count inserted
  - `get_row(conn, rel_path: str) -> dict` — keys: `status, size, sha256, is_chunked, chunk_count, tg_message_id, manifest_tg_message_id, error`
  - `set_status(conn, rel_path: str, status: str, **fields) -> None`
  - `pending_rel_paths(conn) -> list[str]` — every row with `status != 'VERIFIED'`, sorted
  - `get_meta(conn, key: str) -> str | None`
  - `set_meta(conn, key: str, value: str) -> None`
  - `status_counts(conn) -> dict[str, int]`
  - `failed_rows(conn) -> list[tuple[str, str]]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_backup_local_folder.py`:

```python
from pathlib import Path

from scripts.backup_local_folder import (
    failed_rows,
    get_meta,
    get_row,
    open_state_db,
    pending_rel_paths,
    scan_folder,
    set_meta,
    set_status,
    status_counts,
)


def _make_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "100APPLE").mkdir(parents=True)
    (source / "100APPLE" / "IMG_0001.HEIC").write_bytes(b"a" * 10)
    (source / "100APPLE" / "IMG_0002.MOV").write_bytes(b"b" * 20)
    (source / "101APPLE").mkdir()
    (source / "101APPLE" / "IMG_1000.JPG").write_bytes(b"c" * 30)
    return source


def test_scan_folder_inserts_pending_rows(tmp_path):
    source = _make_source(tmp_path)
    conn = open_state_db(tmp_path / "state.db")

    inserted = scan_folder(conn, source)

    assert inserted == 3
    assert pending_rel_paths(conn) == [
        "100APPLE/IMG_0001.HEIC",
        "100APPLE/IMG_0002.MOV",
        "101APPLE/IMG_1000.JPG",
    ]
    row = get_row(conn, "100APPLE/IMG_0002.MOV")
    assert row["status"] == "PENDING"
    assert row["size"] == 20
    assert row["sha256"] is None


def test_scan_folder_is_idempotent(tmp_path):
    source = _make_source(tmp_path)
    conn = open_state_db(tmp_path / "state.db")
    scan_folder(conn, source)

    second_pass = scan_folder(conn, source)

    assert second_pass == 0
    assert len(pending_rel_paths(conn)) == 3


def test_set_status_updates_fields_and_excludes_verified_from_pending(tmp_path):
    source = _make_source(tmp_path)
    conn = open_state_db(tmp_path / "state.db")
    scan_folder(conn, source)

    set_status(conn, "100APPLE/IMG_0001.HEIC", "VERIFIED", sha256="deadbeef", tg_message_id=42)

    row = get_row(conn, "100APPLE/IMG_0001.HEIC")
    assert row["status"] == "VERIFIED"
    assert row["sha256"] == "deadbeef"
    assert row["tg_message_id"] == 42
    remaining = pending_rel_paths(conn)
    assert "100APPLE/IMG_0001.HEIC" not in remaining
    assert len(remaining) == 2


def test_status_counts_and_failed_rows(tmp_path):
    source = _make_source(tmp_path)
    conn = open_state_db(tmp_path / "state.db")
    scan_folder(conn, source)

    set_status(conn, "100APPLE/IMG_0001.HEIC", "VERIFIED")
    set_status(conn, "100APPLE/IMG_0002.MOV", "FAILED", error="hash mismatch on verify")

    counts = status_counts(conn)
    assert counts["VERIFIED"] == 1
    assert counts["FAILED"] == 1
    assert counts["PENDING"] == 1
    assert failed_rows(conn) == [("100APPLE/IMG_0002.MOV", "hash mismatch on verify")]


def test_meta_roundtrip_and_overwrite(tmp_path):
    conn = open_state_db(tmp_path / "state.db")

    assert get_meta(conn, "channel_id") is None
    set_meta(conn, "channel_id", "-1001234567890")
    assert get_meta(conn, "channel_id") == "-1001234567890"
    set_meta(conn, "channel_id", "-1009999999999")
    assert get_meta(conn, "channel_id") == "-1009999999999"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/tenax/Personal/code/telegram-photo-vault && .venv/bin/python -m pytest tests/test_backup_local_folder.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.backup_local_folder'` (the file doesn't exist yet).

- [ ] **Step 3: Write the minimal implementation**

Create `scripts/backup_local_folder.py`:

```python
"""One-off migration: upload a local folder to a new Telegram channel, verify by
re-download, report status. Run via docker compose (see README/spec). Nothing here
deletes source files.

Design: docs/superpowers/specs/2026-07-30-iphone-backup-telegram-design.md
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/tenax/Personal/code/telegram-photo-vault && .venv/bin/python -m pytest tests/test_backup_local_folder.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/tenax/Personal/code/telegram-photo-vault
git add scripts/backup_local_folder.py tests/test_backup_local_folder.py
git commit -m "feat: add state DB for local-folder Telegram backup script"
```

---

### Task 2: Telegram client bootstrap + channel creation

**Files:**
- Modify: `scripts/backup_local_folder.py`
- Modify: `tests/test_backup_local_folder.py`

**Interfaces:**
- Consumes: `get_meta`, `set_meta` (Task 1).
- Produces:
  - `build_client() -> pyrogram.Client`
  - `async def get_or_create_channel_id(client, conn) -> int`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backup_local_folder.py`:

```python
from types import SimpleNamespace

from scripts.backup_local_folder import get_or_create_channel_id


class FakeChannelClient:
    def __init__(self):
        self.create_channel_calls = []

    async def create_channel(self, title):
        self.create_channel_calls.append(title)
        return SimpleNamespace(id=-1009999999999)


async def test_get_or_create_channel_id_creates_once(tmp_path):
    conn = open_state_db(tmp_path / "state.db")
    client = FakeChannelClient()

    channel_id = await get_or_create_channel_id(client, conn)

    assert channel_id == -1009999999999
    assert client.create_channel_calls == ["iPhone Backup Archive"]
    assert get_meta(conn, "channel_id") == "-1009999999999"


async def test_get_or_create_channel_id_reuses_stored_id(tmp_path):
    conn = open_state_db(tmp_path / "state.db")
    set_meta(conn, "channel_id", "-1001111111111")
    client = FakeChannelClient()

    channel_id = await get_or_create_channel_id(client, conn)

    assert channel_id == -1001111111111
    assert client.create_channel_calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/tenax/Personal/code/telegram-photo-vault && .venv/bin/python -m pytest tests/test_backup_local_folder.py -v -k channel_id`
Expected: FAIL with `ImportError: cannot import name 'get_or_create_channel_id'`.

- [ ] **Step 3: Write the minimal implementation**

Add imports at the top of `scripts/backup_local_folder.py` (after the existing `from pathlib import Path`):

```python
import os

from pyrogram import Client
```

Append to `scripts/backup_local_folder.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/tenax/Personal/code/telegram-photo-vault && .venv/bin/python -m pytest tests/test_backup_local_folder.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/tenax/Personal/code/telegram-photo-vault
git add scripts/backup_local_folder.py tests/test_backup_local_folder.py
git commit -m "feat: add Telegram client bootstrap and channel creation"
```

---

### Task 3: Single-file upload

**Files:**
- Modify: `scripts/backup_local_folder.py`
- Modify: `tests/test_backup_local_folder.py`

**Interfaces:**
- Consumes: none new from earlier tasks (pure function, takes a service object).
- Produces:
  - `build_caption(rel_path: str, size: int, sha256: str) -> str`
  - `async def upload_single(service, abs_path: Path, rel_path: str, size: int, sha256: str) -> int` — returns the Telegram message id

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backup_local_folder.py`:

```python
from scripts.backup_local_folder import build_caption, upload_single


class FakeUploadService:
    def __init__(self):
        self.calls = []

    async def upload_document(self, file_path, *, caption=None, file_name=None):
        self.calls.append({"file_path": file_path, "caption": caption, "file_name": file_name})
        return SimpleNamespace(id=555)


def test_build_caption_contains_path_size_and_hash_prefix():
    caption = build_caption("106APPLE/IMG_6849.MOV", 2476877121, "a" * 64)

    assert caption == "106APPLE/IMG_6849.MOV\nsize=2476877121 sha256=" + "a" * 16


async def test_upload_single_calls_service_and_returns_message_id(tmp_path):
    abs_path = tmp_path / "IMG_0001.HEIC"
    abs_path.write_bytes(b"x" * 10)
    service = FakeUploadService()

    message_id = await upload_single(service, abs_path, "100APPLE/IMG_0001.HEIC", 10, "b" * 64)

    assert message_id == 555
    assert len(service.calls) == 1
    call = service.calls[0]
    assert call["file_path"] == abs_path
    assert call["file_name"] == "IMG_0001.HEIC"
    assert call["caption"] == "100APPLE/IMG_0001.HEIC\nsize=10 sha256=" + "b" * 16
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/tenax/Personal/code/telegram-photo-vault && .venv/bin/python -m pytest tests/test_backup_local_folder.py -v -k upload_single or build_caption`
Expected: FAIL with `ImportError: cannot import name 'build_caption'`.

- [ ] **Step 3: Write the minimal implementation**

Append to `scripts/backup_local_folder.py`:

```python
def build_caption(rel_path: str, size: int, sha256: str) -> str:
    return f"{rel_path}\nsize={size} sha256={sha256[:16]}"


async def upload_single(service, abs_path: Path, rel_path: str, size: int, sha256: str) -> int:
    message = await service.upload_document(
        abs_path,
        caption=build_caption(rel_path, size, sha256),
        file_name=abs_path.name,
    )
    return message.id
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/tenax/Personal/code/telegram-photo-vault && .venv/bin/python -m pytest tests/test_backup_local_folder.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/tenax/Personal/code/telegram-photo-vault
git add scripts/backup_local_folder.py tests/test_backup_local_folder.py
git commit -m "feat: add single-file upload path"
```

---

### Task 4: Chunked upload (files > threshold)

**Files:**
- Modify: `scripts/backup_local_folder.py`
- Modify: `tests/test_backup_local_folder.py`

**Interfaces:**
- Consumes: nothing new from earlier tasks.
- Produces:
  - `async def upload_chunked(service, abs_path: Path, rel_path: str, size: int, sha256: str, chunk_size: int, chunk_hashes: list[str]) -> tuple[Message, int, list[Message]]` — `(manifest_message, chunk_count, chunk_messages)`. `chunk_hashes` is the per-chunk SHA-256 list from `compute_hashes(abs_path, chunk_size)` (same call the caller already makes at the HASHED step) — it is threaded through so the manifest's per-chunk `sha256` field is real, not a placeholder. `scripts/vault_merge.py` verifies every part's hash against this field before merging; an empty/wrong value silently breaks that recovery path, so this is load-bearing, not cosmetic.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_backup_local_folder.py`:

```python
import hashlib
import json

from scripts.backup_local_folder import upload_chunked


class FakeChunkService:
    def __init__(self):
        self.next_id = 700
        self.chunk_payloads = {}
        self.manifest_payload = None
        self.manifest_caption = None
        self.chunk_captions = {}

    async def upload_file_object(self, file_object, caption):
        self.chunk_payloads[file_object.name] = file_object.read()
        self.chunk_captions[file_object.name] = caption
        self.next_id += 1
        return SimpleNamespace(id=self.next_id)

    async def upload_bytes(self, data, *, file_name, caption):
        self.manifest_payload = json.loads(data)
        self.manifest_caption = caption
        self.next_id += 1
        return SimpleNamespace(id=self.next_id)


async def test_upload_chunked_splits_uploads_and_builds_manifest(tmp_path):
    data = bytes(range(256)) * 40  # 10_240 bytes
    abs_path = tmp_path / "IMG_7023.MOV"
    abs_path.write_bytes(data)
    sha256 = hashlib.sha256(data).hexdigest()
    chunk_size = 4_000
    _, chunk_hashes = compute_hashes(abs_path, chunk_size)
    service = FakeChunkService()

    manifest_message, count, chunk_messages = await upload_chunked(
        service, abs_path, "107APPLE/IMG_7023.MOV", len(data), sha256, chunk_size, chunk_hashes
    )

    assert count == 3
    assert len(chunk_messages) == 3
    assert manifest_message.id == service.next_id
    assert set(service.chunk_payloads) == {
        "IMG_7023.MOV.part001-of-003",
        "IMG_7023.MOV.part002-of-003",
        "IMG_7023.MOV.part003-of-003",
    }
    joined = b"".join(
        service.chunk_payloads[f"IMG_7023.MOV.part{i:03d}-of-003"] for i in (1, 2, 3)
    )
    assert joined == data
    assert service.manifest_payload["sha256"] == sha256
    assert service.manifest_payload["chunk_count"] == 3
    # Per-chunk hashes in the manifest must be real (this is what scripts/vault_merge.py
    # verifies against before it will merge parts back into the original file).
    for spec in service.manifest_payload["chunks"]:
        expected = chunk_hashes[spec["index"] - 1]
        assert spec["sha256"] == expected
        assert spec["sha256"] != ""
    assert "107APPLE/IMG_7023.MOV" in service.chunk_captions["IMG_7023.MOV.part001-of-003"]
    assert "107APPLE/IMG_7023.MOV" in service.manifest_caption
```

Add `from app.services.chunking import compute_hashes` to the test file's imports if not already present (Task 1's imports don't include it; add it alongside the `import hashlib`/`import json` lines already being added in this task's Step 1).

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/tenax/Personal/code/telegram-photo-vault && .venv/bin/python -m pytest tests/test_backup_local_folder.py -v -k upload_chunked`
Expected: FAIL with `ImportError: cannot import name 'upload_chunked'`.

- [ ] **Step 3: Write the minimal implementation**

Add to the imports at the top of `scripts/backup_local_folder.py`:

```python
import json

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
```

Append to `scripts/backup_local_folder.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/tenax/Personal/code/telegram-photo-vault && .venv/bin/python -m pytest tests/test_backup_local_folder.py -v`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/tenax/Personal/code/telegram-photo-vault
git add scripts/backup_local_folder.py tests/test_backup_local_folder.py
git commit -m "feat: add chunked upload path for files over the size threshold"
```

---

### Task 5: Verification by re-download + hash compare

**Files:**
- Modify: `scripts/backup_local_folder.py`
- Modify: `tests/test_backup_local_folder.py`

**Interfaces:**
- Consumes: nothing new from earlier tasks.
- Produces:
  - `async def verify_single(client, channel_id: int, message_id: int, expected_sha256: str, tmp_dir: Path) -> bool`
  - `async def verify_chunked(client, chunk_messages: list, expected_sha256: str, tmp_dir: Path) -> bool`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backup_local_folder.py`:

```python
from scripts.backup_local_folder import verify_chunked, verify_single


class FakeDownloadClient:
    def __init__(self, payloads_by_message_id):
        self.payloads = payloads_by_message_id
        self.get_messages_calls = []

    async def get_messages(self, chat_id, message_id):
        self.get_messages_calls.append((chat_id, message_id))
        return SimpleNamespace(id=message_id)

    async def download_media(self, message, file_name):
        Path(file_name).write_bytes(self.payloads[message.id])


async def test_verify_single_matches_expected_hash(tmp_path):
    data = b"hello world"
    client = FakeDownloadClient({42: data})

    ok = await verify_single(client, -100123, 42, hashlib.sha256(data).hexdigest(), tmp_path)

    assert ok is True
    assert client.get_messages_calls == [(-100123, 42)]
    assert list(tmp_path.iterdir()) == []  # temp file cleaned up


async def test_verify_single_detects_mismatch(tmp_path):
    client = FakeDownloadClient({42: b"corrupted"})

    ok = await verify_single(client, -100123, 42, hashlib.sha256(b"original").hexdigest(), tmp_path)

    assert ok is False


async def test_verify_chunked_hashes_parts_in_order(tmp_path):
    part1, part2 = b"first-part-bytes", b"second-part-bytes"
    client = FakeDownloadClient({1: part1, 2: part2})
    chunk_messages = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
    expected = hashlib.sha256(part1 + part2).hexdigest()

    ok = await verify_chunked(client, chunk_messages, expected, tmp_path)

    assert ok is True
    assert list(tmp_path.iterdir()) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/tenax/Personal/code/telegram-photo-vault && .venv/bin/python -m pytest tests/test_backup_local_folder.py -v -k verify_`
Expected: FAIL with `ImportError: cannot import name 'verify_single'`.

- [ ] **Step 3: Write the minimal implementation**

Add to the imports at the top of `scripts/backup_local_folder.py`:

```python
import hashlib
```

Append to `scripts/backup_local_folder.py`:

```python
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
```

`verify_chunked` keeps its own inline loop rather than reusing `_download_and_hash` — that helper returns a standalone per-chunk digest, but `verify_chunked` needs one hasher fed incrementally across ALL chunks in sequence, so that its final digest equals the *whole original file's* SHA-256 (the same `expected_sha256` used by `verify_single` and stored in the DB) — hashing a chunk's bytes into a per-chunk digest and then hashing the sequence of digests is a different, incompatible value from hashing the concatenated raw bytes directly. The task reviewer flagged the resulting small duplication between the two functions as Minor; it is accepted as-is rather than risking exactly this kind of subtle correctness break to remove it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/tenax/Personal/code/telegram-photo-vault && .venv/bin/python -m pytest tests/test_backup_local_folder.py -v`
Expected: PASS (13 tests).

- [ ] **Step 5: Commit**

```bash
cd /home/tenax/Personal/code/telegram-photo-vault
git add scripts/backup_local_folder.py tests/test_backup_local_folder.py
git commit -m "feat: add re-download hash verification for single and chunked uploads"
```

---

### Task 6: Per-file dispatch, CLI, and report — wire it all together

**Files:**
- Modify: `scripts/backup_local_folder.py`
- Modify: `tests/test_backup_local_folder.py`

**Interfaces:**
- Consumes: every function from Tasks 1–5, plus `TelegramService` from `app/services/telegram.py`.
- Produces:
  - `async def process_file(client, service, conn, source_root, rel_path, channel_id, chunk_threshold, chunk_size, tmp_verify_dir) -> None`
  - `def print_report(conn) -> None`
  - `async def main(argv=None) -> None`
  - CLI entry point (`if __name__ == "__main__":`)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_backup_local_folder.py`:

```python
from scripts.backup_local_folder import main, process_file


class FakeFullClient:
    """Combined fake covering channel creation, upload, and verify-download."""

    def __init__(self):
        self.channel_id = -1005555555555
        self.messages = {}
        self.next_id = 900
        self.corrupt_message_ids = set()

    async def create_channel(self, title):
        return SimpleNamespace(id=self.channel_id)

    async def get_messages(self, chat_id, message_id):
        return SimpleNamespace(id=message_id)

    async def download_media(self, message, file_name):
        payload = self.messages[message.id]
        if message.id in self.corrupt_message_ids:
            payload = payload[:-1] + b"\x00"
        Path(file_name).write_bytes(payload)


class FakeFullService:
    def __init__(self, client):
        self.client = client

    async def upload_document(self, file_path, *, caption=None, file_name=None):
        data = Path(file_path).read_bytes()
        self.client.next_id += 1
        self.client.messages[self.client.next_id] = data
        return SimpleNamespace(id=self.client.next_id)

    async def upload_file_object(self, file_object, caption):
        data = file_object.read()
        self.client.next_id += 1
        self.client.messages[self.client.next_id] = data
        return SimpleNamespace(id=self.client.next_id)

    async def upload_bytes(self, data, *, file_name, caption):
        self.client.next_id += 1
        self.client.messages[self.client.next_id] = data
        return SimpleNamespace(id=self.client.next_id)


async def test_process_file_uploads_and_verifies_small_file(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0001.HEIC").write_bytes(b"x" * 100)
    conn = open_state_db(tmp_path / "state.db")
    scan_folder(conn, source)
    client = FakeFullClient()
    service = FakeFullService(client)

    await process_file(
        client, service, conn, source, "IMG_0001.HEIC", client.channel_id,
        chunk_threshold=1_000_000, chunk_size=500_000, tmp_verify_dir=tmp_path / "verify",
    )

    row = get_row(conn, "IMG_0001.HEIC")
    assert row["status"] == "VERIFIED"
    assert row["tg_message_id"] is not None


async def test_process_file_marks_failed_on_verify_mismatch(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_0002.HEIC").write_bytes(b"y" * 100)
    conn = open_state_db(tmp_path / "state.db")
    scan_folder(conn, source)
    client = FakeFullClient()
    service = FakeFullService(client)

    async def upload_then_corrupt(file_path, *, caption=None, file_name=None):
        data = Path(file_path).read_bytes()
        client.next_id += 1
        client.messages[client.next_id] = data
        client.corrupt_message_ids.add(client.next_id)
        return SimpleNamespace(id=client.next_id)

    service.upload_document = upload_then_corrupt

    await process_file(
        client, service, conn, source, "IMG_0002.HEIC", client.channel_id,
        chunk_threshold=1_000_000, chunk_size=500_000, tmp_verify_dir=tmp_path / "verify",
    )

    row = get_row(conn, "IMG_0002.HEIC")
    assert row["status"] == "FAILED"
    assert row["error"] == "hash mismatch on verify"


async def test_process_file_chunked_path_verifies_end_to_end(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "IMG_7023.MOV").write_bytes(os.urandom(10_000))
    conn = open_state_db(tmp_path / "state.db")
    scan_folder(conn, source)
    client = FakeFullClient()
    service = FakeFullService(client)

    await process_file(
        client, service, conn, source, "IMG_7023.MOV", client.channel_id,
        chunk_threshold=4_000, chunk_size=4_000, tmp_verify_dir=tmp_path / "verify",
    )

    row = get_row(conn, "IMG_7023.MOV")
    assert row["status"] == "VERIFIED"
    assert row["is_chunked"] == 1
    assert row["chunk_count"] == 3
    assert row["manifest_tg_message_id"] is not None


async def test_main_scan_only_reports_without_touching_telegram(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.jpg").write_bytes(b"1")
    (source / "b.jpg").write_bytes(b"2")

    def _fail_build_client():
        raise AssertionError("build_client must not be called in --scan-only mode")

    monkeypatch.setattr("scripts.backup_local_folder.build_client", _fail_build_client)

    await main(
        [
            "--source", str(source),
            "--state-db", str(tmp_path / "state.db"),
            "--scan-only",
        ]
    )

    out = capsys.readouterr().out
    assert "PENDING: 2" in out
```

Add `import os` near the top of the test file if not already present (it is, once Task 6's implementation imports it — but the test file itself also needs it for `os.urandom`; add `import os` to the existing top-of-file imports in `tests/test_backup_local_folder.py`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/tenax/Personal/code/telegram-photo-vault && .venv/bin/python -m pytest tests/test_backup_local_folder.py -v -k "process_file or main_scan_only"`
Expected: FAIL with `ImportError: cannot import name 'process_file'`.

- [ ] **Step 3: Write the minimal implementation**

Add to the imports at the top of `scripts/backup_local_folder.py`:

```python
import argparse
import asyncio

from pyrogram.errors import FloodWait

from app.services.telegram import TelegramService
```

Append to `scripts/backup_local_folder.py`:

```python
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
    tmp_verify_dir.mkdir(parents=True, exist_ok=True)

    conn = open_state_db(Path(args.state_db))
    inserted = scan_folder(conn, source_root)
    logger.info("scan complete: %d new file(s) tracked", inserted)

    if args.scan_only:
        print_report(conn)
        return

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/tenax/Personal/code/telegram-photo-vault && .venv/bin/python -m pytest tests/test_backup_local_folder.py -v`
Expected: PASS (17 tests).

- [ ] **Step 5: Run the full test suite to confirm nothing else broke**

Run: `cd /home/tenax/Personal/code/telegram-photo-vault && .venv/bin/python -m pytest -q`
Expected: PASS, same pre-existing pass count plus the 17 new tests, 0 failures.

- [ ] **Step 6: Commit**

```bash
cd /home/tenax/Personal/code/telegram-photo-vault
git add scripts/backup_local_folder.py tests/test_backup_local_folder.py
git commit -m "feat: wire per-file dispatch, CLI, and status report for local-folder backup script"
```

---

### Task 7: Manual smoke test against real Telegram, then the real migration run

This task has no automated assertions — it exercises the script against the real
Telegram account and the real source folder. Do not skip the dry-run smoke test
(Step 1–2) before pointing it at the real 106 GB folder.

**Files:** none (execution only).

- [ ] **Step 1: Build a throwaway 2-file test folder and dry-run the scan**

```bash
mkdir -p /tmp/backup-smoke-test/100APPLE
head -c 1000 /dev/urandom > /tmp/backup-smoke-test/100APPLE/test1.jpg
head -c 5000000 /dev/urandom > /tmp/backup-smoke-test/100APPLE/test2.mov
cd /home/tenax/Personal/code/telegram-photo-vault
docker compose run --rm --entrypoint python \
  -v "/tmp/backup-smoke-test:/backup-source:ro" \
  -v "$(pwd)/scripts:/app/scripts:ro" \
  telegram-photo-vault scripts/backup_local_folder.py --scan-only
```

Expected output: `PENDING: 2` and no channel line (nothing created yet in scan-only mode).

- [ ] **Step 2: Run the smoke test for real (creates the channel, uploads 2 tiny files)**

```bash
cd /home/tenax/Personal/code/telegram-photo-vault
docker compose run --rm --entrypoint python \
  -v "/tmp/backup-smoke-test:/backup-source:ro" \
  -v "$(pwd)/scripts:/app/scripts:ro" \
  telegram-photo-vault scripts/backup_local_folder.py
```

Expected output: `VERIFIED: 2` and a `Channel: -100...` line. Open Telegram and confirm
the "iPhone Backup Archive" channel exists with 2 documents captioned
`100APPLE/test1.jpg\n...` and `100APPLE/test2.mov\n...`.

- [ ] **Step 3: Note the created channel id, then clean up the smoke test's local state**

The channel id printed in Step 2 is now the *real* backup channel — record it (e.g. in
your own notes; nothing in this repo needs it, since the script re-reads it from
`/data/iphone_backup_state.db` on every run). Remove the throwaway smoke-test state so
it doesn't linger in the same DB as the real migration:

```bash
rm -f /home/tenax/Personal/code/telegram-photo-vault/data/iphone_backup_state.db
rm -rf /tmp/backup-smoke-test
```

- [ ] **Step 4: Scan-only against the real folder to confirm the file count**

```bash
cd /home/tenax/Personal/code/telegram-photo-vault
docker compose run --rm --entrypoint python \
  -v "/home/tenax/Pictures/iPhone backup:/backup-source:ro" \
  -v "$(pwd)/scripts:/app/scripts:ro" \
  telegram-photo-vault scripts/backup_local_folder.py --scan-only
```

Expected output: `PENDING: 6916` (matches the count established during investigation).

- [ ] **Step 5: Run the real migration in the background**

This will take many hours (106 GB uploaded, then re-downloaded for verification, at a
conservative pace). Run it detached so it survives a closed terminal:

```bash
cd /home/tenax/Personal/code/telegram-photo-vault
nohup docker compose run --rm --entrypoint python \
  -v "/home/tenax/Pictures/iPhone backup:/backup-source:ro" \
  -v "$(pwd)/scripts:/app/scripts:ro" \
  telegram-photo-vault scripts/backup_local_folder.py \
  > /home/tenax/Personal/code/telegram-photo-vault/local-data/backup-run.log 2>&1 &
disown
```

- [ ] **Step 6: Check progress at any time without disturbing the run**

The state DB is a plain SQLite file on the host at `./data/iphone_backup_state.db`
(read-only queries are safe to run concurrently):

```bash
sqlite3 /home/tenax/Personal/code/telegram-photo-vault/data/iphone_backup_state.db \
  "SELECT status, COUNT(*) FROM files GROUP BY status;"
tail -f /home/tenax/Personal/code/telegram-photo-vault/local-data/backup-run.log
```

- [ ] **Step 7: When the run finishes, confirm zero failures before deleting anything**

```bash
sqlite3 /home/tenax/Personal/code/telegram-photo-vault/data/iphone_backup_state.db \
  "SELECT rel_path, error FROM files WHERE status = 'FAILED';"
```

If that query returns no rows, every file is `VERIFIED` and it is safe to manually
delete `/home/tenax/Pictures/iPhone backup/` to reclaim the 106 GB. If it returns rows,
re-run the same Step 5 command — the script only retries files not already `VERIFIED`
and will pick those failures back up from `HASHED`.
