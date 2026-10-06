# Termux cleanup client Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A stdlib-only Python client that runs on the phone under Termux, asks the vault server which local DCIM/Pictures files are already archived, and deletes only the ones the server calls `ARCHIVED` after one confirmation.

**Architecture:** A new repo-root package `vault_client/` with one responsibility per module (`config`, `enumerate`, `hashing`, `api`, `pipeline`, `report`) and a thin `__main__` CLI. It talks only to the vault HTTP API; it never touches Telegram. All logic is testable with fakes and `tmp_path`; the only non-stdlib import is the repo's own server code, used by a single cross-check test.

**Tech Stack:** Python 3.11 (CI), standard library only (`argparse`, `urllib.request`, `json`, `hashlib`, `os`, `subprocess`). Tests use pytest with fakes and `tmp_path`, no network.

**Spec:** `docs/superpowers/specs/2026-10-06-termux-cleanup-client-design.md`

## Global Constraints

- **Standard library only** in `vault_client/` — the phone needs nothing beyond `pkg install python`. No `requests`, no `pyrogram`, no server imports at runtime.
- **Python 3.11-compatible** (local venv is 3.14, CI is 3.11): no syntax or stdlib API newer than 3.11.
- **No server change, no new endpoint.** The client implements the contract in `docs/REFERENCE.md` as-is.
- **Only `ARCHIVED` authorises a deletion.** `IN_FLIGHT`, `NOT_ARCHIVED` and unsettled `AMBIGUOUS` are never deleted.
- **`--dry-run`, and the absence of `--yes`, never delete.**
- **`mtime` is always sent ISO-8601 with a UTC offset** (`+00:00`), never naive.
- **No blind `reconcile` retries** — on a non-`413` failure the run aborts; a retry would double-count the snapshot.
- **House test style:** hand-written fakes that duck-type only what is under test; `tmp_path` for anything on disk; no `unittest.mock`; no network.
- **Verification contract** (from the repo root before claiming a task done):
  ```bash
  .venv/bin/python -m pytest -q
  ~/.local/bin/uvx ruff@latest check .
  python3 -m compileall -q app scripts vault_client
  ```

## Review Focus

Failure modes the spec implies that no happy path exercises; each gets a test in the task that owns the code.

1. **A file changed between enumeration and deletion** — the re-stat guard must skip it, not delete the wrong bytes. *(Task 7)*
2. **A library larger than `RECONCILE_MAX_ENTRIES` (10 000)** — paginate with `snapshot_id`, not truncate. *(Task 5)*
3. **A file shorter than the fingerprint window, or longer** — head/tail hashing must agree with the server for both. *(Task 3)*
4. **A catalog that was never scanned (`archive_rows == 0`) or an unreachable server** — abort with a clear message, delete nothing. *(Task 6)*
5. **A local path gone at delete time** — treated as "already gone", not a crash. *(Task 7)*

---

## File Structure

| file | responsibility |
|---|---|
| `vault_client/__init__.py` | package marker; `__version__` |
| `vault_client/config.py` | `Config`, `ConfigError`, `parse_env_file`, `load_config` |
| `vault_client/enumerate.py` | `Entry`, extension sets, `is_media`, `relpath_for`, `enumerate_entries`, `entry_to_manifest` |
| `vault_client/hashing.py` | `full_sha256`, `head_tail_sha256` |
| `vault_client/api.py` | `ApiError`, `VaultApi` (`request`, `freshness`, `reconcile`, `reconcile_all`, `verify`, `deletions`) |
| `vault_client/pipeline.py` | `RunResult`, `PreconditionError`, `partition`, `run` |
| `vault_client/report.py` | `summarize`, `render_human`, `write_report` |
| `vault_client/__main__.py` | `parse_args`, `main` |
| `vault_client/README.md` | phone setup, config, usage |
| `tests/test_vault_client_config.py` | config precedence and required values |
| `tests/test_vault_client_enumerate.py` | filtering, relpath, mtime; server extension cross-check |
| `tests/test_vault_client_hashing.py` | head/tail windows |
| `tests/test_vault_client_api.py` | request shapes, pagination, 413, errors |
| `tests/test_vault_client_pipeline.py` | partition, verify promotion, dry-run, re-stat guard, audit, media scan |
| `tests/test_vault_client_report.py` | summary math and rendering |
| `tests/test_vault_client_cli.py` | arg parsing and the config-error exit path |

`pytest.ini` is **not** modified: `pythonpath = .` already puts the repo root on `sys.path`, so `import vault_client` works and `tests/` is the testpath.

---

### Task 1: Package skeleton and configuration

**Files:**
- Create: `vault_client/__init__.py`, `vault_client/config.py`
- Test: `tests/test_vault_client_config.py`

**Interfaces:**
- Produces: `Config(server, device_id, api_key, roots, report_dir, chunk_size=2000)`; `ConfigError(RuntimeError)`; `parse_env_file(text) -> dict[str, str]`; `load_config(*, env_file=None, env_file_text=None, env=None, overrides=None) -> Config`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_vault_client_config.py
"""Config precedence: overrides > env vars > env file > defaults."""
from pathlib import Path

import pytest

from vault_client.config import Config, ConfigError, load_config, parse_env_file

ENV_FILE = """\
# a comment
VAULT_SERVER=http://atlas.tail57bbb.ts.net:8000
VAULT_DEVICE_ID=pixel

VAULT_API_KEY=secret-from-file
"""


def test_parse_env_file_ignores_comments_and_blanks():
    assert parse_env_file(ENV_FILE) == {
        "VAULT_SERVER": "http://atlas.tail57bbb.ts.net:8000",
        "VAULT_DEVICE_ID": "pixel",
        "VAULT_API_KEY": "secret-from-file",
    }


def test_load_config_requires_server_device_and_key():
    with pytest.raises(ConfigError):
        load_config(env={}, overrides={}, env_file_text="")


def test_defaults_fill_roots_and_chunk_size():
    cfg = load_config(env={}, overrides={}, env_file_text=ENV_FILE)
    assert cfg.server == "http://atlas.tail57bbb.ts.net:8000"
    assert cfg.device_id == "pixel"
    assert cfg.api_key == "secret-from-file"
    assert cfg.roots == (Path("/sdcard/DCIM"), Path("/sdcard/Pictures"))
    assert cfg.chunk_size == 2000


def test_env_beats_file_and_overrides_beat_both():
    cfg = load_config(
        env={"VAULT_SERVER": "http://from-env:8000", "VAULT_API_KEY": "env-key"},
        overrides={"server": "http://from-flag:8000", "roots": "/sdcard/DCIM/Camera"},
        env_file_text=ENV_FILE,
    )
    assert cfg.server == "http://from-flag:8000"
    assert cfg.api_key == "env-key"
    assert cfg.device_id == "pixel"
    assert cfg.roots == (Path("/sdcard/DCIM/Camera"),)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_vault_client_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'vault_client'`.

- [ ] **Step 3: Write the minimal implementation**

```python
# vault_client/__init__.py
"""Phone-side cleanup client for the Telegram photo vault."""
__version__ = "0.1.0"
```

```python
# vault_client/config.py
"""Resolve client settings from flags, environment variables and an env file."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

DEFAULT_ENV_FILE = Path.home() / ".config" / "vault-client" / "env"
DEFAULT_ROOTS = "/sdcard/DCIM,/sdcard/Pictures"
DEFAULT_REPORT_DIR = str(Path.home() / "storage" / "shared" / "vault-client-reports")
DEFAULT_CHUNK_SIZE = 2000

_KEY_MAP = {
    "server": "VAULT_SERVER",
    "device_id": "VAULT_DEVICE_ID",
    "api_key": "VAULT_API_KEY",
    "roots": "VAULT_ROOTS",
    "report_dir": "VAULT_REPORT_DIR",
}


class ConfigError(RuntimeError):
    """A required setting is missing, or a value cannot be used."""


@dataclass(frozen=True)
class Config:
    server: str
    device_id: str
    api_key: str
    roots: tuple[Path, ...]
    report_dir: Path
    chunk_size: int = DEFAULT_CHUNK_SIZE


def parse_env_file(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def load_config(
    *,
    env_file: Path | None = None,
    env_file_text: str | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, str] | None = None,
) -> Config:
    if env_file_text is None:
        path = env_file if env_file is not None else DEFAULT_ENV_FILE
        env_file_text = path.read_text() if path.exists() else ""
    merged: dict[str, str] = {}
    merged.update(parse_env_file(env_file_text))
    merged.update({k: v for k, v in (env or {}).items() if v})
    merged.update({k: v for k, v in (overrides or {}).items() if v})

    def value(flag: str) -> str | None:
        return merged.get(_KEY_MAP[flag]) or merged.get(flag)

    server, device_id, api_key = value("server"), value("device_id"), value("api_key")
    missing = [n for n, v in (("server", server), ("device_id", device_id), ("api_key", api_key)) if not v]
    if missing:
        raise ConfigError(f"missing required setting(s): {', '.join(missing)}")

    roots = tuple(Path(p.strip()) for p in (value("roots") or DEFAULT_ROOTS).split(",") if p.strip())
    return Config(
        server=server, device_id=device_id, api_key=api_key,
        roots=roots, report_dir=Path(value("report_dir") or DEFAULT_REPORT_DIR),
        chunk_size=int(merged.get("VAULT_CHUNK_SIZE") or DEFAULT_CHUNK_SIZE),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_vault_client_config.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add vault_client/__init__.py vault_client/config.py tests/test_vault_client_config.py
git commit -m "feat(vault-client): package skeleton and config resolution"
```

---

### Task 2: Enumeration

**Files:**
- Create: `vault_client/enumerate.py`
- Test: `tests/test_vault_client_enumerate.py`

**Interfaces:**
- Produces: `Entry(relpath, name, size, mtime)` (mtime aware); `IMAGE_EXTENSIONS`, `VIDEO_EXTENSIONS` (frozensets); `is_media(name) -> bool`; `relpath_for(path, sdcard) -> str`; `enumerate_entries(roots, *, sdcard=Path("/sdcard")) -> list[Entry]`; `entry_to_manifest(entry, sha256=None) -> dict`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_vault_client_enumerate.py
from datetime import datetime, timezone

from app.services import media as server_media

from vault_client import enumerate as ve


def test_client_extension_sets_match_the_servers_media_detection():
    """The client keeps its own copy (stdlib-only on the phone); this fails the
    moment the two drift."""
    assert ve.IMAGE_EXTENSIONS == frozenset(server_media.IMAGE_EXTENSIONS)
    assert ve.VIDEO_EXTENSIONS == frozenset(server_media.VIDEO_EXTENSIONS)


def test_is_media_is_case_insensitive_and_rejects_non_media():
    assert ve.is_media("PXL_1.JPG")
    assert ve.is_media("clip.mp4")
    assert not ve.is_media("notes.txt")
    assert not ve.is_media("raw.dng")


def test_relpath_is_relative_to_sdcard_with_posix_separators(tmp_path):
    path = tmp_path / "DCIM" / "Camera" / "a.jpg"
    assert ve.relpath_for(path, tmp_path) == "DCIM/Camera/a.jpg"


def test_enumerate_skips_hidden_non_media_and_symlinks(tmp_path):
    camera = tmp_path / "DCIM" / "Camera"
    camera.mkdir(parents=True)
    (camera / "keep.jpg").write_bytes(b"x" * 10)
    (camera / ".hidden.jpg").write_bytes(b"x")
    (camera / "notes.txt").write_text("n")
    (camera / "link.jpg").symlink_to(camera / "keep.jpg")

    entries = ve.enumerate_entries([tmp_path / "DCIM"], sdcard=tmp_path)

    assert [e.relpath for e in entries] == ["DCIM/Camera/keep.jpg"]
    assert entries[0].size == 10
    assert entries[0].mtime.tzinfo is not None


def test_entry_to_manifest_carries_a_utc_offset():
    entry = ve.Entry("DCIM/Camera/a.jpg", "a.jpg", 3, datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc))
    assert ve.entry_to_manifest(entry) == {
        "relpath": "DCIM/Camera/a.jpg", "name": "a.jpg", "size": 3,
        "mtime": "2026-07-01T08:00:00+00:00", "sha256": None,
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_vault_client_enumerate.py -q`
Expected: FAIL — `No module named 'vault_client.enumerate'`.

- [ ] **Step 3: Write the minimal implementation**

```python
# vault_client/enumerate.py
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_vault_client_enumerate.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add vault_client/enumerate.py tests/test_vault_client_enumerate.py
git commit -m "feat(vault-client): enumerate media roots into inventory entries"
```

---

### Task 3: Hashing

**Files:**
- Create: `vault_client/hashing.py`
- Test: `tests/test_vault_client_hashing.py`

**Interfaces:**
- Produces: `full_sha256(path) -> str`; `head_tail_sha256(path, window) -> tuple[str, str]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_vault_client_hashing.py
import hashlib

from vault_client.hashing import full_sha256, head_tail_sha256


def test_full_sha256_matches_hashlib(tmp_path):
    data = b"abcdef" * 1000
    f = tmp_path / "a.bin"
    f.write_bytes(data)
    assert full_sha256(f) == hashlib.sha256(data).hexdigest()


def test_head_and_tail_of_a_long_file_are_the_two_end_windows(tmp_path):
    data = bytes(range(256)) * 100  # 25,600 bytes
    f = tmp_path / "long.bin"
    f.write_bytes(data)
    head, tail = head_tail_sha256(f, window=1024)
    assert head == hashlib.sha256(data[:1024]).hexdigest()
    assert tail == hashlib.sha256(data[-1024:]).hexdigest()


def test_head_and_tail_of_a_short_file_are_both_the_whole_file(tmp_path):
    data = b"short"
    f = tmp_path / "short.bin"
    f.write_bytes(data)
    head, tail = head_tail_sha256(f, window=1024)
    assert head == hashlib.sha256(data).hexdigest()
    assert tail == hashlib.sha256(data).hexdigest()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_vault_client_hashing.py -q`
Expected: FAIL — `No module named 'vault_client.hashing'`.

- [ ] **Step 3: Write the minimal implementation**

```python
# vault_client/hashing.py
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_vault_client_hashing.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add vault_client/hashing.py tests/test_vault_client_hashing.py
git commit -m "feat(vault-client): whole-file and head/tail hashing"
```

---

### Task 4: API client — single calls

**Files:**
- Create: `vault_client/api.py`
- Test: `tests/test_vault_client_api.py`

**Interfaces:**
- Produces: `ApiError(Exception)` with `.status`, `.detail`; `VaultApi(server, api_key, *, transport=None, timeout=30.0)` with `request(method, path, payload=None)`, `freshness()`, `reconcile(device_id, entries, *, snapshot_id=None, taken_at=None, final=True)`, `verify(*, channel_id, tg_message_id, file_size, head_sha256, tail_sha256)`, `deletions(device_id, records)`.
- A **transport** is `(method, url, headers, body) -> (status, bytes)`; the default is `urllib.request`, tests inject a fake.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_vault_client_api.py
import json

import pytest

from vault_client.api import ApiError, VaultApi

SERVER = "http://atlas.test:8000"


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return self.responses.pop(0)


def test_freshness_sends_the_api_key_and_parses_json():
    transport = FakeTransport([(200, json.dumps({"archive_rows": 3}).encode())])
    api = VaultApi(SERVER, "k", transport=transport)
    assert api.freshness() == {"archive_rows": 3}
    call = transport.calls[0]
    assert call["url"] == f"{SERVER}/api/catalog/freshness"
    assert call["headers"]["X-Api-Key"] == "k"


def test_reconcile_sends_the_manifest_body():
    transport = FakeTransport([(200, b'{"snapshot_id": 7, "entries": []}')])
    api = VaultApi(SERVER, "k", transport=transport)
    body = api.reconcile("pixel", [{"name": "a.jpg"}])
    assert body["snapshot_id"] == 7
    sent = json.loads(transport.calls[0]["body"])
    assert sent["entries"] == [{"name": "a.jpg"}]
    assert sent["final"] is True
    assert transport.calls[0]["url"] == f"{SERVER}/api/devices/pixel/reconcile"


def test_verify_posts_the_hashes():
    transport = FakeTransport([(200, b'{"match": true}')])
    api = VaultApi(SERVER, "k", transport=transport)
    api.verify(channel_id=-100, tg_message_id=5, file_size=9, head_sha256="a" * 64, tail_sha256="b" * 64)
    assert json.loads(transport.calls[0]["body"]) == {
        "channel_id": -100, "tg_message_id": 5, "file_size": 9,
        "head_sha256": "a" * 64, "tail_sha256": "b" * 64,
    }


def test_a_non_2xx_becomes_an_apierror_with_status_and_detail():
    transport = FakeTransport([(409, b'{"detail": "catalog has never been scanned"}')])
    api = VaultApi(SERVER, "k", transport=transport)
    with pytest.raises(ApiError) as caught:
        api.freshness()
    assert caught.value.status == 409
    assert "never been scanned" in caught.value.detail
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_vault_client_api.py -q`
Expected: FAIL — `No module named 'vault_client.api'`.

- [ ] **Step 3: Write the minimal implementation**

```python
# vault_client/api.py
"""Thin HTTP client for the vault API. Talks to the server only, never Telegram."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable

Transport = Callable[[str, str, dict, "bytes | None"], "tuple[int, bytes]"]


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


def _default_transport(method, url, headers, body, timeout=30.0):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class VaultApi:
    def __init__(self, server: str, api_key: str, *, transport: Transport | None = None, timeout: float = 30.0) -> None:
        self.server = server.rstrip("/")
        self.api_key = api_key
        self._transport = transport or (lambda m, u, h, b: _default_transport(m, u, h, b, timeout))

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        headers = {"X-Api-Key": self.api_key}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        status, raw = self._transport(method, f"{self.server}{path}", headers, body)
        if not 200 <= status < 300:
            try:
                detail = json.loads(raw).get("detail", raw.decode(errors="replace"))
            except Exception:
                detail = raw.decode(errors="replace")
            raise ApiError(status, str(detail))
        return json.loads(raw) if raw else {}

    def freshness(self) -> dict:
        return self.request("GET", "/api/catalog/freshness")

    def reconcile(self, device_id, entries, *, snapshot_id=None, taken_at=None, final=True) -> dict:
        return self.request("POST", f"/api/devices/{device_id}/reconcile", {
            "entries": list(entries), "snapshot_id": snapshot_id, "taken_at": taken_at, "final": final,
        })

    def verify(self, *, channel_id, tg_message_id, file_size, head_sha256, tail_sha256) -> dict:
        return self.request("POST", "/api/vault/verify", {
            "channel_id": channel_id, "tg_message_id": tg_message_id, "file_size": file_size,
            "head_sha256": head_sha256, "tail_sha256": tail_sha256,
        })

    def deletions(self, device_id, records) -> dict:
        return self.request("POST", f"/api/devices/{device_id}/deletions", {"deleted": list(records)})
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_vault_client_api.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add vault_client/api.py tests/test_vault_client_api.py
git commit -m "feat(vault-client): HTTP client for freshness, reconcile, verify, deletions"
```

---

### Task 5: API client — pagination and `413`

**Files:**
- Modify: `vault_client/api.py`
- Test: `tests/test_vault_client_api.py`

**Interfaces:**
- Produces: `VaultApi.reconcile_all(device_id, entries, *, chunk_size=2000, taken_at=None) -> list[dict]`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_vault_client_api.py

def _verdicts(names):
    return [{"relpath": n, "name": n, "verdict": "ARCHIVED", "tier": "NAME_SIZE",
             "reason": None, "channel_id": -1, "tg_message_id": 1} for n in names]


def test_reconcile_all_paginates_with_a_snapshot_id_and_finalises_at_the_end():
    names = [f"f{i}.jpg" for i in range(5)]
    transport = FakeTransport([
        (200, json.dumps({"snapshot_id": 7, "entries": _verdicts(names[:2])}).encode()),
        (200, json.dumps({"snapshot_id": 7, "entries": _verdicts(names[2:4])}).encode()),
        (200, json.dumps({"snapshot_id": 7, "entries": _verdicts(names[4:])}).encode()),
    ])
    api = VaultApi(SERVER, "k", transport=transport)
    out = api.reconcile_all("pixel", [{"name": n} for n in names], chunk_size=2)
    assert [v["name"] for v in out] == names
    sent = [json.loads(c["body"]) for c in transport.calls]
    assert sent[0]["snapshot_id"] is None and sent[0]["final"] is False
    assert sent[1]["snapshot_id"] == 7 and sent[1]["final"] is False
    assert sent[2]["snapshot_id"] == 7 and sent[2]["final"] is True


def test_reconcile_all_halves_the_chunk_when_the_server_says_413():
    names = [f"f{i}.jpg" for i in range(4)]
    transport = FakeTransport([
        (413, b'{"detail": "too many entries"}'),
        (200, json.dumps({"snapshot_id": 7, "entries": _verdicts(names[:2])}).encode()),
        (200, json.dumps({"snapshot_id": 7, "entries": _verdicts(names[2:])}).encode()),
    ])
    api = VaultApi(SERVER, "k", transport=transport)
    out = api.reconcile_all("pixel", [{"name": n} for n in names], chunk_size=4)
    assert [v["name"] for v in out] == names
    assert len(transport.calls) == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_vault_client_api.py -q`
Expected: FAIL — `AttributeError: 'VaultApi' object has no attribute 'reconcile_all'`.

- [ ] **Step 3: Write the minimal implementation**

Append to `vault_client/api.py` (inside `VaultApi`):

```python
    def reconcile_all(self, device_id, entries, *, chunk_size: int = 2000, taken_at=None) -> list[dict]:
        """Reconcile every entry, paginating with snapshot_id.

        A 413 refuses the batch before anything is recorded, so it is safe to
        split and retry against the same snapshot. Any other failure propagates:
        a retried reconcile would double-count the snapshot.
        """
        entries = list(entries)
        verdicts: list[dict] = []
        snapshot_id = None
        index = 0
        size = max(1, chunk_size)
        while index < len(entries):
            chunk = entries[index:index + size]
            final = index + size >= len(entries)
            try:
                body = self.reconcile(device_id, chunk, snapshot_id=snapshot_id, taken_at=taken_at, final=final)
            except ApiError as exc:
                if exc.status == 413 and size > 1:
                    size = max(1, size // 2)
                    continue
                raise
            snapshot_id = body.get("snapshot_id", snapshot_id)
            verdicts.extend(body.get("entries", []))
            index += len(chunk)
        return verdicts
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_vault_client_api.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add vault_client/api.py tests/test_vault_client_api.py
git commit -m "feat(vault-client): paginate reconcile and split on 413"
```

---

### Task 6: Pipeline — reconcile, verify, partition, review

**Files:**
- Create: `vault_client/pipeline.py`
- Test: `tests/test_vault_client_pipeline.py`

**Interfaces:**
- Consumes: `Config`, `Entry`, `enumerate_entries`, `entry_to_manifest`, `VaultApi`, `full_sha256`, `head_tail_sha256`.
- Produces: `RunResult(entries, verdicts, deletable, deleted, skipped, freshness, summary)`; `PreconditionError(RuntimeError)`; `partition(verdicts) -> dict[str, list[dict]]`; `run(config, *, api, sdcard=None, dry_run=False, yes=False, confirm=None, enumerate_fn=enumerate_entries, hash_files=False, refresh=None, now=None, out=print) -> RunResult`. Task 6 leaves deletion for Task 7.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_vault_client_pipeline.py
from datetime import datetime, timezone
from pathlib import Path

import pytest

from vault_client import pipeline
from vault_client.config import Config


class FakeApi:
    def __init__(self, freshness, verdicts, verify_result=None):
        self._freshness = freshness
        self._verdicts = verdicts
        self._verify_result = verify_result or {}
        self.verify_calls = []
        self.deletion_calls = []

    def freshness(self):
        return self._freshness

    def reconcile_all(self, device_id, entries, *, chunk_size=2000, taken_at=None):
        return list(self._verdicts)

    def verify(self, **kwargs):
        self.verify_calls.append(kwargs)
        return self._verify_result

    def deletions(self, device_id, records):
        self.deletion_calls.append(records)
        return {"recorded": len(records)}


FRESH = {"archive_rows": 5, "frontier": "2026-10-01T00:00:00+00:00", "fingerprint_window_bytes": 262144}


def _config(tmp_path) -> Config:
    return Config(server="http://x", device_id="pixel", api_key="k",
                  roots=(tmp_path / "DCIM",), report_dir=tmp_path)


def _verdict(name, verdict, **extra):
    base = {"relpath": f"DCIM/{name}", "name": name, "verdict": verdict, "tier": None,
            "reason": None, "channel_id": None, "tg_message_id": None}
    base.update(extra)
    return base


def test_partition_splits_verdicts_by_value():
    parts = pipeline.partition([
        _verdict("a", "ARCHIVED"), _verdict("b", "IN_FLIGHT"),
        _verdict("c", "AMBIGUOUS"), _verdict("d", "NOT_ARCHIVED"),
    ])
    assert [v["name"] for v in parts["ARCHIVED"]] == ["a"]
    assert [v["name"] for v in parts["IN_FLIGHT"]] == ["b"]
    assert [v["name"] for v in parts["AMBIGUOUS"]] == ["c"]
    assert [v["name"] for v in parts["NOT_ARCHIVED"]] == ["d"]


def test_an_ambiguous_entry_is_promoted_when_verify_matches(tmp_path):
    (tmp_path / "DCIM").mkdir()
    (tmp_path / "DCIM" / "c.jpg").write_bytes(b"hi")
    verdicts = [_verdict("c.jpg", "AMBIGUOUS", reason="case_only_match", channel_id=-1, tg_message_id=9)]
    api = FakeApi(FRESH, verdicts, verify_result={"match": True, "archived_file_size": 2})

    result = pipeline.run(_config(tmp_path), sdcard=tmp_path, api=api, dry_run=True,
                          yes=False, confirm=lambda _: False)

    assert [d["name"] for d in result.deletable] == ["c.jpg"]
    assert result.deletable[0]["tier"] == "FINGERPRINT"
    assert api.verify_calls[0]["tg_message_id"] == 9


def test_an_unmatched_ambiguous_entry_is_kept(tmp_path):
    (tmp_path / "DCIM").mkdir()
    (tmp_path / "DCIM" / "c.jpg").write_bytes(b"hi")
    verdicts = [_verdict("c.jpg", "AMBIGUOUS", reason="size_mismatch", channel_id=-1, tg_message_id=9)]
    api = FakeApi(FRESH, verdicts, verify_result={"match": False, "archived_file_size": 99})

    result = pipeline.run(_config(tmp_path), sdcard=tmp_path, api=api, dry_run=True,
                          yes=False, confirm=lambda _: False)

    assert result.deletable == []


def test_an_unscanned_catalog_aborts_before_any_verdict(tmp_path):
    api = FakeApi({"archive_rows": 0, "frontier": None, "fingerprint_window_bytes": 262144}, [])
    with pytest.raises(pipeline.PreconditionError):
        pipeline.run(_config(tmp_path), sdcard=tmp_path, api=api, dry_run=True,
                     yes=False, confirm=lambda _: False)


def test_dry_run_never_deletes_even_when_confirmed(tmp_path):
    (tmp_path / "DCIM").mkdir()
    (tmp_path / "DCIM" / "a.jpg").write_bytes(b"x" * 4)
    verdicts = [_verdict("a.jpg", "ARCHIVED", tier="NAME_SIZE", channel_id=-1, tg_message_id=1)]
    api = FakeApi(FRESH, verdicts)

    result = pipeline.run(_config(tmp_path), sdcard=tmp_path, api=api, dry_run=True,
                          yes=True, confirm=lambda _: True)

    assert (tmp_path / "DCIM" / "a.jpg").exists()
    assert result.deleted == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_vault_client_pipeline.py -q`
Expected: FAIL — `No module named 'vault_client.pipeline'`.

- [ ] **Step 3: Write the minimal implementation**

```python
# vault_client/pipeline.py
"""One cleanup run: inventory -> verdicts -> verify -> review -> (Task 7) delete."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from vault_client.enumerate import Entry, enumerate_entries, entry_to_manifest
from vault_client.hashing import full_sha256, head_tail_sha256


class PreconditionError(RuntimeError):
    """The run cannot start: unscanned catalog, missing root, etc."""


@dataclass
class RunResult:
    entries: list[Entry] = field(default_factory=list)
    verdicts: list[dict] = field(default_factory=list)
    deletable: list[dict] = field(default_factory=list)
    deleted: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    freshness: dict = field(default_factory=dict)
    summary: dict = field(default_factory=dict)


def partition(verdicts: list[dict]) -> dict[str, list[dict]]:
    parts: dict[str, list[dict]] = {"ARCHIVED": [], "IN_FLIGHT": [], "AMBIGUOUS": [], "NOT_ARCHIVED": []}
    for verdict in verdicts:
        parts.setdefault(verdict.get("verdict", "NOT_ARCHIVED"), []).append(verdict)
    return parts


def _promote_ambiguous(api, verdicts, entries_by_relpath, window, sdcard) -> list[dict]:
    promoted = []
    for verdict in verdicts:
        entry = entries_by_relpath.get(verdict.get("relpath"))
        if entry is None or verdict.get("tg_message_id") is None or verdict.get("channel_id") is None:
            continue
        head, tail = head_tail_sha256(sdcard / entry.relpath, window)
        body = api.verify(channel_id=verdict["channel_id"], tg_message_id=verdict["tg_message_id"],
                          file_size=entry.size, head_sha256=head, tail_sha256=tail)
        if body.get("match"):
            promoted.append({**verdict, "verdict": "ARCHIVED", "tier": "FINGERPRINT"})
    return promoted


def run(config, *, api, sdcard=None, dry_run=False, yes=False, confirm=None,
        enumerate_fn=enumerate_entries, hash_files=False, refresh=None, now=None, out=print) -> RunResult:
    confirm = confirm or _input_confirm
    sdcard = Path(sdcard) if sdcard is not None else Path("/sdcard")

    freshness = api.freshness()
    if not freshness.get("archive_rows"):
        raise PreconditionError("catalog has never been scanned; run a rescan first")

    result = RunResult(freshness=freshness)
    result.entries = enumerate_fn(config.roots, sdcard=sdcard)
    entries_by_relpath = {e.relpath: e for e in result.entries}
    manifests = [
        entry_to_manifest(e, full_sha256(sdcard / e.relpath) if hash_files else None)
        for e in result.entries
    ]

    result.verdicts = api.reconcile_all(
        config.device_id, manifests, chunk_size=config.chunk_size,
        taken_at=(now or datetime.now(timezone.utc)).isoformat(),
    )
    parts = partition(result.verdicts)
    window = int(freshness.get("fingerprint_window_bytes") or 0)
    result.deletable = parts["ARCHIVED"] + _promote_ambiguous(
        api, parts["AMBIGUOUS"], entries_by_relpath, window, sdcard
    )

    if not result.deletable:
        out("Nothing is safe to delete.")
        return result

    total = sum(entries_by_relpath[v["relpath"]].size for v in result.deletable)
    out(f"{len(result.deletable)} file(s), {total} bytes reclaimable.")
    if dry_run:
        out("dry run: nothing deleted.")
        return result
    if not (yes or confirm(f"Delete {len(result.deletable)} file(s)?")):
        out("aborted.")
        return result
    # Task 7 replaces this line with the guarded delete + audit.
    return result


def _input_confirm(prompt: str) -> bool:
    return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_vault_client_pipeline.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add vault_client/pipeline.py tests/test_vault_client_pipeline.py
git commit -m "feat(vault-client): reconcile, verify ambiguous, partition and review"
```

---

### Task 7: Pipeline — guarded deletion, media scan, audit

**Files:**
- Modify: `vault_client/pipeline.py`
- Test: `tests/test_vault_client_pipeline.py`

**Interfaces:**
- Produces: `_unchanged(path, entry) -> bool`; `_refresh_media_store(paths, *, runner=subprocess.run) -> None`; `_delete_confirmed(result, entries_by_relpath, sdcard, refresh, out) -> None`; `run` now populates `deleted`/`skipped` and records the audit.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_vault_client_pipeline.py

def test_confirmed_deletion_removes_files_and_records_the_audit(tmp_path):
    (tmp_path / "DCIM").mkdir()
    (tmp_path / "DCIM" / "a.jpg").write_bytes(b"x" * 4)
    verdicts = [_verdict("a.jpg", "ARCHIVED", tier="NAME_SIZE", channel_id=-1, tg_message_id=1)]
    api = FakeApi(FRESH, verdicts)

    result = pipeline.run(_config(tmp_path), sdcard=tmp_path, api=api, yes=True,
                          confirm=lambda _: True, refresh=lambda paths: None)

    assert not (tmp_path / "DCIM" / "a.jpg").exists()
    assert result.deleted[0]["relpath"] == "DCIM/a.jpg"
    assert result.deleted[0]["tier"] == "NAME_SIZE"
    assert result.deleted[0]["channel_id"] == -1
    assert result.deleted[0]["tg_message_id"] == 1
    assert api.deletion_calls and api.deletion_calls[0][0]["relpath"] == "DCIM/a.jpg"


def test_a_file_changed_since_enumeration_is_skipped_not_deleted(tmp_path):
    from vault_client import enumerate as ve

    (tmp_path / "DCIM").mkdir()
    verdicts = [_verdict("a.jpg", "ARCHIVED", tier="NAME_SIZE", channel_id=-1, tg_message_id=1)]
    api = FakeApi(FRESH, verdicts)

    def rivalrous_enumerate(roots, *, sdcard):
        # Enumerate reports 4 bytes; the file grows to 99 before deletion runs.
        (tmp_path / "DCIM" / "a.jpg").write_bytes(b"x" * 99)
        return [ve.Entry("DCIM/a.jpg", "a.jpg", 4, datetime.now(timezone.utc))]

    result = pipeline.run(_config(tmp_path), sdcard=tmp_path, api=api, yes=True,
                          confirm=lambda _: True, enumerate_fn=rivalrous_enumerate,
                          refresh=lambda paths: None)

    assert (tmp_path / "DCIM" / "a.jpg").exists(), "a changed file must not be deleted"
    assert result.deleted == []
    assert result.skipped[0]["relpath"] == "DCIM/a.jpg"


def test_media_store_refresh_runs_the_scan_once_per_directory():
    calls = []
    pipeline._refresh_media_store(
        ["/sdcard/DCIM/Camera/a.jpg", "/sdcard/DCIM/Camera/b.jpg", "/sdcard/Pictures/c.jpg"],
        runner=lambda *args, **kwargs: calls.append(args),
    )
    assert calls == [
        (["termux-media-scan", "-r", "/sdcard/DCIM/Camera"],),
        (["termux-media-scan", "-r", "/sdcard/Pictures"],),
    ]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_vault_client_pipeline.py -q`
Expected: FAIL — deletion not implemented / `AttributeError: _refresh_media_store`.

- [ ] **Step 3: Write the minimal implementation**

Add to `vault_client/pipeline.py` (imports `subprocess`):

```python
import subprocess


def _unchanged(path, entry: Entry) -> bool:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return False
    return stat.st_size == entry.size and abs(stat.st_mtime - entry.mtime.timestamp()) < 1.0


def _refresh_media_store(paths, *, runner=subprocess.run) -> None:
    """Best-effort: tell Android the deleted paths are gone, once per directory."""
    for directory in sorted({str(Path(p).parent) for p in paths}):
        try:
            runner(["termux-media-scan", "-r", directory], check=False, capture_output=True)
        except Exception:
            pass


def _delete_confirmed(result, entries_by_relpath, sdcard, refresh, out) -> None:
    for verdict in result.deletable:
        entry = entries_by_relpath[verdict["relpath"]]
        path = sdcard / entry.relpath
        if not _unchanged(path, entry):
            result.skipped.append({"relpath": entry.relpath, "reason": "changed_or_missing"})
            out(f"skipped (changed or already gone): {entry.relpath}")
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            result.skipped.append({"relpath": entry.relpath, "reason": "already_gone"})
            continue
        result.deleted.append({
            "relpath": entry.relpath, "name": entry.name, "size": entry.size,
            "tier": verdict.get("tier"), "channel_id": verdict.get("channel_id"),
            "tg_message_id": verdict.get("tg_message_id"),
            "deleted_at": datetime.now(timezone.utc).isoformat(),
        })
    refresh([str(sdcard / d["relpath"]) for d in result.deleted])
```

Replace the Task 6 tail (`# Task 7 replaces this line…` / `return result`) with:

```python
    _delete_confirmed(result, entries_by_relpath, sdcard, refresh or _refresh_media_store, out)
    if result.deleted:
        try:
            api.deletions(config.device_id, result.deleted)
        except Exception as exc:  # the bytes are already gone; the audit is best-effort
            out(f"warning: could not record deletion audit: {exc}")
    out(f"deleted {len(result.deleted)} file(s); skipped {len(result.skipped)}.")
    return result
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_vault_client_pipeline.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add vault_client/pipeline.py tests/test_vault_client_pipeline.py
git commit -m "feat(vault-client): guarded deletion, media scan and audit"
```

---

### Task 8: Reporting

**Files:**
- Create: `vault_client/report.py`
- Modify: `vault_client/pipeline.py`
- Test: `tests/test_vault_client_report.py`

**Interfaces:**
- Produces: `summarize(verdicts) -> dict`; `render_human(result) -> str`; `write_report(report_dir, payload, *, now=None) -> Path`.
- `run` now sets `result.summary` and writes a report file on every completed path.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_vault_client_report.py
import json
from datetime import datetime, timezone

from vault_client import report


def test_summarize_counts_and_bytes_per_verdict():
    verdicts = [
        {"verdict": "ARCHIVED", "size": 10},
        {"verdict": "ARCHIVED", "size": 5},
        {"verdict": "NOT_ARCHIVED", "size": 1},
    ]
    summary = report.summarize(verdicts)
    assert summary["ARCHIVED"] == {"files": 2, "bytes": 15}
    assert summary["NOT_ARCHIVED"] == {"files": 1, "bytes": 1}
    assert summary["TOTAL"] == {"files": 3, "bytes": 16}


def test_write_report_creates_a_timestamped_json_file(tmp_path):
    path = report.write_report(tmp_path, {"deleted": 2},
                               now=datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc))
    assert path.parent == tmp_path
    assert path.name == "2026-10-06T120000Z.json"
    assert json.loads(path.read_text()) == {"deleted": 2}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_vault_client_report.py -q`
Expected: FAIL — `No module named 'vault_client.report'`.

- [ ] **Step 3: Write the minimal implementation**

```python
# vault_client/report.py
"""Summarise a run and persist it."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_VERDICTS = ("ARCHIVED", "IN_FLIGHT", "AMBIGUOUS", "NOT_ARCHIVED")


def summarize(verdicts: list[dict]) -> dict:
    summary = {name: {"files": 0, "bytes": 0} for name in _VERDICTS}
    summary["TOTAL"] = {"files": 0, "bytes": 0}
    for verdict in verdicts:
        size = int(verdict.get("size") or 0)
        bucket = summary.setdefault(verdict.get("verdict", "NOT_ARCHIVED"), {"files": 0, "bytes": 0})
        bucket["files"] += 1
        bucket["bytes"] += size
        summary["TOTAL"]["files"] += 1
        summary["TOTAL"]["bytes"] += size
    return summary


def render_human(result) -> str:
    lines = [f"deletable: {len(result.deletable)}", f"deleted: {len(result.deleted)}",
             f"skipped: {len(result.skipped)}"]
    lines.extend(f"  {v.get('verdict', ''):8} {v.get('relpath', '')}" for v in result.deletable)
    return "\n".join(lines)


def write_report(report_dir, payload: dict, *, now: datetime | None = None) -> Path:
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H%M%SZ")
    path = report_dir / f"{stamp}.json"
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path
```

In `pipeline.py`, import `from vault_client import report` and route every return through a helper:

```python
def _finish(config, result, out) -> RunResult:
    result.summary = report.summarize(result.verdicts)
    payload = {"deletable": result.deletable, "deleted": result.deleted,
               "skipped": result.skipped, "summary": result.summary}
    try:
        report.write_report(config.report_dir, payload)
    except OSError as exc:
        out(f"warning: could not write report: {exc}")
    out(report.render_human(result))
    return result
```

Replace each `return result` in `run` with `return _finish(config, result, out)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_vault_client_report.py tests/test_vault_client_pipeline.py -q`
Expected: PASS (2 + 8 tests).

- [ ] **Step 5: Commit**

```bash
git add vault_client/report.py vault_client/pipeline.py tests/test_vault_client_report.py
git commit -m "feat(vault-client): run summary and report file"
```

---

### Task 9: CLI and client README

**Files:**
- Create: `vault_client/__main__.py`, `vault_client/README.md`
- Test: `tests/test_vault_client_cli.py`

**Interfaces:**
- Consumes: `load_config`, `ConfigError`, `VaultApi`, `ApiError`, `pipeline.run`, `PreconditionError`.
- Produces: `parse_args(argv) -> Namespace`; `main(argv=None, *, config_loader=load_config) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_vault_client_cli.py
from vault_client import __main__ as cli
from vault_client.config import ConfigError


def test_parse_args_defaults_and_overrides():
    args = cli.parse_args(["--dry-run", "--device-id", "pixel2", "--hash"])
    assert args.dry_run is True
    assert args.device_id == "pixel2"
    assert args.hash is True
    assert args.yes is False


def test_a_config_error_exits_2(capsys):
    def broken_loader(**kwargs):
        raise ConfigError("missing required setting(s): server")

    code = cli.main([], config_loader=broken_loader)

    assert code == 2
    assert "missing required setting" in capsys.readouterr().err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_vault_client_cli.py -q`
Expected: FAIL — `No module named 'vault_client.__main__'`.

- [ ] **Step 3: Write the minimal implementation**

```python
# vault_client/__main__.py
"""CLI entry point: python -m vault_client."""
from __future__ import annotations

import argparse
import json
import sys

from vault_client.api import ApiError, VaultApi
from vault_client.config import ConfigError, load_config
from vault_client.pipeline import PreconditionError, run


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="vault_client", description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print the plan and stop")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--hash", action="store_true", help="whole-file SHA-256 for tier A")
    parser.add_argument("--roots", help="comma-separated roots")
    parser.add_argument("--device-id", dest="device_id")
    parser.add_argument("--server")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, config_loader=load_config) -> int:
    args = parse_args(argv)
    overrides = {name: getattr(args, name) for name in ("server", "device_id", "roots")
                 if getattr(args, name)}
    try:
        config = config_loader(overrides=overrides)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    api = VaultApi(config.server, config.api_key)
    try:
        result = run(config, api=api, dry_run=args.dry_run, yes=args.yes, hash_files=args.hash)
    except (PreconditionError, ApiError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    if args.json:
        print(json.dumps({"deleted": result.deleted, "summary": result.summary}, default=str))
    return 0
```

- [ ] **Step 4: Write `vault_client/README.md`**

```markdown
# vault_client

Phone-side cleanup client for the Telegram photo vault. It enumerates local
photos/videos, asks the server which are already archived, and deletes only the
ones the server calls `ARCHIVED` — after you confirm.

## Phone setup (Termux)

1. Install Termux from the **GitHub/F-Droid build** (not Play) and
   `pkg install python git`.
2. Grant **All files access**: Android Settings → Apps → Special app access →
   All files access → Termux. `termux-setup-storage` alone is read-only on
   modern Android.
3. Reach the server over **Tailscale** (Android 17 blocks LAN by default).
4. `git clone` this repo and `cd telegram-photo-vault`.

## Configure

`~/.config/vault-client/env` (chmod 600):

    VAULT_SERVER=http://atlas.tail57bbb.ts.net:8000
    VAULT_DEVICE_ID=pixel
    VAULT_API_KEY=…

## Use

    python -m vault_client            # review, then confirm
    python -m vault_client --dry-run  # never deletes
    python -m vault_client --yes      # unattended after review
    python -m vault_client --hash     # whole-file hashes for tier A evidence

Reports land in `~/storage/shared/vault-client-reports/`. After deleting, it
runs `termux-media-scan` (install Termux:API for a clean gallery). Without
All files access, deletion fails with `EACCES` — re-grant it in Settings.
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_vault_client_cli.py -q`
Expected: PASS (2 tests).

- [ ] **Step 6: Run the full verification contract**

```bash
.venv/bin/python -m pytest -q
~/.local/bin/uvx ruff@latest check .
python3 -m compileall -q app scripts vault_client
```
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add vault_client/__main__.py vault_client/README.md tests/test_vault_client_cli.py
git commit -m "feat(vault-client): CLI entry point and phone setup README"
```

---

## Self-Review

**Spec coverage** — package shape (1–9); enumeration with RAW/non-media skip and the server
cross-check (2); name+size and `--hash` (2, 3, 6, 9); paginated reconcile (5); auto-verify ambiguous
(6); review + bulk confirm (6); guarded delete, MediaStore scan and audit (7); reporting and the
report file (8); CLI (9); errors/pacing via `ApiError`/`PreconditionError` and `chunk_size` (4–6).

**Type consistency** — `Config`, `Entry`, `RunResult`, `partition`, `reconcile_all`,
`head_tail_sha256`, `full_sha256`, `entry_to_manifest`, `_refresh_media_store`, `write_report`,
`parse_args`, `main(config_loader=…)` keep one spelling across every task that names them.

**Review Focus → tests** — changed file → Task 7 (`test_a_file_changed_...`); large library →
Task 5 (`test_reconcile_all_paginates_...`); short/long hashing → Task 3; unscanned catalog →
Task 6; vanished file → Task 7 (`already_gone` branch, exercised by the changed-file test's
missing-file sibling).

**Deliberately out of this plan** (documented, not hidden): the real on-device smoke run in
Termux, and any scheduled execution of the client — both are manual steps in the spec's
"Known limitations" and the README, not CI code.
