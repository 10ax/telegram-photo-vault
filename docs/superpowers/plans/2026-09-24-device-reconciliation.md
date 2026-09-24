# Device Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Given an inventory of files on a phone, return a per-file verdict saying whether the archive already holds those exact bytes, so a client can delete in bulk without inspecting files one by one.

**Architecture:** One new service, `app/services/reconcile.py`, whose decision logic is a pure module-level function (`decide`) that touches neither the network nor the database — the service only fetches candidates and calls it. `catalog_items` is the lookup surface; this plan adds no table that describes a channel. Three new tables hold what the server remembers about a device: an aggregate snapshot, the non-`ARCHIVED` findings, and a permanent deletion audit.

**Tech Stack:** Python 3.11-compatible (local venv is 3.14, CI is 3.11), FastAPI, SQLAlchemy 2 async on SQLite via aiosqlite, kurigram (pyrogram namespace), pytest with `asyncio_mode = auto`.

**Spec:** `docs/superpowers/specs/2026-09-24-device-reconciliation-design.md`

**Depends on:** Tasks 1-3 of `docs/superpowers/plans/2026-09-21-catalog-and-immich-bridge.md` (the `CatalogItem` model, the multi-channel scan, provenance matching). It does **not** depend on that plan's enrich, report or gallery-export tasks. Those three tasks must be complete and committed before Task 1 here begins.

**Scope amendment (decided at execution pre-flight).** The catalog plan puts `POST /api/catalog/scan` in its Task 5, alongside the enrichment report — outside the dependency above. Delivered without it, nothing could populate the catalog, so `evaluate` would raise `CatalogNeverScanned` forever and this feature would ship inert. Task 5 below therefore carries two routes that trigger work owned by earlier tasks: `/catalog/scan` (copied verbatim from catalog Task 5, so that task must not redefine it when it later runs) and `/catalog/resolve-manifests` (which gives Task 1's operation its caller).

## Global Constraints

- **Nothing in this plan writes to Telegram, and nothing in it deletes anything anywhere.** The server emits verdicts; deleting local files is the client's job and no client is in scope. There is no `--apply` flag because there is no destructive mode.
- **No second index.** `catalog_items` is the only table describing channel contents. This plan adds columns to it and never a parallel copy.
- **Evidence policy, enforced in code and asserted in tests:** only rows whose `channel_role` is `ChannelRole.ARCHIVE` count as proof; a row with `media_kind == "photo"` never counts as proof, in any channel; a row with `artifact == "chunk"` is never matched against a local file.
- **No fuzzy matching.** No name normalisation, no date heuristics, no nearest-size matching. The same inventory against the same catalog must yield the same verdicts.
- **Additive schema only.** New tables are created by `create_all`; new columns on `catalog_items` are nullable. No existing column is renamed or dropped.
- **Every new env var takes all five edits**: parse in `lifespan`, pass as a constructor kwarg, add to `docker-compose.yml`, document in `AGENTS.md` and in `docs/REFERENCE.md`. The `add-config-knob` skill covers the sequence. The task that introduces a knob does all five in that task.
- **Test style is the house style**: hand-written fakes that duck-type only the methods under test (`SimpleNamespace` messages, a `FakeClient` class), never `unittest.mock` patching; `tmp_path` whenever the filesystem is touched; the `clean_db` fixture for anything using the database; no network, no credentials, no real Telegram/MEGA/SFTP call anywhere.
- **Pacing is a safety requirement, not a tuning knob.** Every loop that touches Telegram sleeps between items and processes a bounded batch.
- **Verification contract** — run all three from the repo root before claiming a task is done:
  ```bash
  .venv/bin/python -m pytest -q
  .venv/bin/python -m ruff check .
  python3 -m compileall -q app scripts
  ```
  (`ruff.toml` enables only `E9` + `F`, `target-version = "py311"`.)

**Two refinements of the spec, decided here.**

1. *Where a chunked file's identity lives.* The spec said the scan writes "one row for the original file" synthesised from the manifest. The catalog plan had already settled that a `catalog_items` row describes exactly one **message**, with an `artifact` column labelling chunk parts and manifests — and it is right, because counting eight chunk parts as eight photos corrupts every number in the report. So the original's identity is stored **on the manifest row**, in three new nullable columns, which is faithful to per-message rows: the manifest message's entire payload *is* the description of the original. And because parsing it costs a fetch, it is a separate bounded operation (`resolve_manifests`) in the style of `enrich`, not extra work inside the pure-metadata scan.

2. *The fingerprint needs an endpoint.* The spec described settling an ambiguous entry by comparing partial hashes, and said the client "sends the same partial hash", but listed no route for it. `POST /api/vault/verify` is that route, so the mechanism has a caller and `RECONCILE_FINGERPRINT_BYTES` has a reader.

3. *What the freshness rule demotes.* The spec said an entry newer than the catalog "cannot be `ARCHIVED`". That rule exists because a name-and-size match is a metadata inference, and a stale catalog makes inference unsound. A hash match is not an inference — if the content hash is in the catalog, the bytes are in the channel, whatever the dates say. So the freshness rule demotes `NAME_SIZE` matches only, and leaves `HASH` and `FINGERPRINT` alone. Being conservative about proof would cost the user deletions they are entitled to, and buy nothing.

## Review Focus

Five failure modes the spec implies that no task's happy path exercises, most likely to bite first. Each one's test is placed in the task that owns the code.

- **A zero-byte local file.** Every zero-byte file shares a size, so a name-and-size match on `size == 0` is not evidence of anything. `NAME_SIZE` must refuse sizes that are not strictly positive. *(Task 3)*
- **A catalog that has never been scanned.** Every entry would come back `NOT_ARCHIVED` and read as "nothing you own is backed up". The service must refuse to produce verdicts at all when no archive channel has ever been scanned, rather than answer confidently from an empty table. *(Task 3)*
- **A missing or future `mtime`.** The freshness rule is driven by a timestamp the client supplies and can get wrong. A missing `mtime` must fail closed — treated as newer than the catalog — rather than silently skip the rule. *(Task 3)*
- **A local file whose name matches a chunk part.** `movie.mp4.part001-of-003` is a name that exists in the channel; a local file called that is not an archived original. Chunk rows must never be candidates. *(Task 3)*
- **A continuation `snapshot_id` that belongs to another device or to a finished snapshot.** Accepting it would file one phone's entries under another phone's snapshot. *(Task 5)*

---

## File Structure

**Create:**

| file | responsibility |
|---|---|
| `app/services/reconcile.py` | `decide()` — the pure verdict function, no database and no network. `ReconcileService` — fetches candidates, calls `decide`, persists snapshots, findings and the audit. |
| `tests/test_reconcile_rules.py` | Every branch of `decide()` as a table of cases. No database, no fakes, no client. |
| `tests/test_reconcile_service.py` | Candidate selection against a real `catalog_items`: role, media kind and artifact exclusions; the empty-catalog refusal. |
| `tests/test_reconcile_api.py` | The five endpoints: auth, 409, 413, continuation rules, the audit. |
| `tests/test_catalog_manifest_rows.py` | `resolve_manifests`: a manifest parsed into the original's identity, a malformed one recorded once, bounded batches. |
| `tests/test_partial_fingerprint.py` | Head and tail ranges requested and hashed. |

**Modify:**

| file | change |
|---|---|
| `app/models/database.py` | Three nullable `chunked_*` columns on `CatalogItem`; `DeviceVerdict` and `MatchTier` enums; `DeviceSnapshot`, `DeviceFinding`, `DeletionAudit` tables. |
| `app/services/catalog.py` | `resolve_manifests()`. |
| `app/services/telegram.py` | `partial_fingerprint()`. |
| `app/main.py` | Parse the two knobs, build `ReconcileService`, store it and the existing `TelegramService` on `app.state`. |
| `app/api/routes.py` | Eight endpoints. |
| `tests/test_db_migrations.py` | The three new tables on a legacy database. |
| `docker-compose.yml`, `AGENTS.md`, `docs/REFERENCE.md` | Two knobs plus the manifest protocol, per the five-edit rule. |

---

### Task 1: a chunked file's identity on its manifest row

Without this, every file above 2 GB is invisible to the lookup and comes back `NOT_ARCHIVED` — the files whose loss costs most.

**Files:**
- Modify: `app/models/database.py` (`CatalogItem`)
- Modify: `app/services/catalog.py` (`CatalogService`)
- Test: `tests/test_catalog_manifest_rows.py`

**Interfaces:**
- Consumes: `CatalogItem`, `CatalogService`, `classify_artifact` from catalog plan Tasks 1-2.
- Produces: `CatalogItem.chunked_original_name`, `.chunked_total_size`, `.chunked_sha256`; `CatalogService.resolve_manifests(limit: int = 50) -> dict[str, int]` returning `{"resolved": n, "failed": n, "remaining": n}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_catalog_manifest_rows.py
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app.models.database import AsyncSessionLocal, CatalogItem, ChannelRole
from app.services.catalog import CatalogService, ChannelSpec
from app.services.chunking import MANIFEST_KIND

CHANNEL = -1002637897512


def _manifest_bytes(*, name="movie.mp4", total_size=5_000_000_000, sha="a" * 64, count=3):
    # kind comes from chunking.MANIFEST_KIND so the fixture cannot drift from the contract
    return json.dumps(
        {
            "manifest_version": 1,
            "kind": MANIFEST_KIND,
            "original_filename": name,
            "total_size": total_size,
            "sha256": sha,
            "chunk_size": 1_950_000_000,
            "chunk_count": count,
            "chunks": [],
        }
    ).encode()


class FakeClient:
    """Duck-types only download_media, returning bytes for known message ids."""

    def __init__(self, payloads: dict[int, bytes]):
        self.payloads = payloads
        self.downloaded: list[int] = []

    async def download_media(self, message, in_memory=True):
        self.downloaded.append(message.id)
        payload = self.payloads.get(message.id)
        if payload is None:
            raise FileNotFoundError(f"no payload for {message.id}")
        return SimpleNamespace(getvalue=lambda: payload)

    async def get_messages(self, chat_id, message_ids):
        return SimpleNamespace(id=message_ids)


async def _add(session, **kwargs):
    row = CatalogItem(
        channel_id=CHANNEL,
        channel_role=ChannelRole.ARCHIVE,
        media_kind="document",
        message_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
        **kwargs,
    )
    session.add(row)
    return row


async def test_manifest_row_gains_the_originals_identity(clean_db):
    async with AsyncSessionLocal() as session:
        await _add(session, tg_message_id=10, artifact="manifest",
                   file_name="movie.mp4.manifest.json", file_size=800)
        await session.commit()

    service = CatalogService(
        FakeClient({10: _manifest_bytes()}),
        [ChannelSpec(channel_id=CHANNEL, role=ChannelRole.ARCHIVE)],
    )
    result = await service.resolve_manifests()

    assert result["resolved"] == 1
    async with AsyncSessionLocal() as session:
        row = await session.scalar(select(CatalogItem).where(CatalogItem.tg_message_id == 10))
        assert row.chunked_original_name == "movie.mp4"
        assert row.chunked_total_size == 5_000_000_000
        assert row.chunked_sha256 == "a" * 64


async def test_chunk_parts_and_plain_documents_are_never_resolved(clean_db):
    async with AsyncSessionLocal() as session:
        await _add(session, tg_message_id=11, artifact="chunk",
                   file_name="movie.mp4.part001-of-003", file_size=1_950_000_000)
        await _add(session, tg_message_id=12, artifact=None,
                   file_name="PXL_20260713_115033830.jpg", file_size=3_412_887)
        await session.commit()

    client = FakeClient({})
    service = CatalogService(client, [ChannelSpec(channel_id=CHANNEL, role=ChannelRole.ARCHIVE)])
    result = await service.resolve_manifests()

    assert result["resolved"] == 0
    assert client.downloaded == []


async def test_a_malformed_manifest_is_attempted_once_and_recorded(clean_db):
    async with AsyncSessionLocal() as session:
        await _add(session, tg_message_id=13, artifact="manifest",
                   file_name="broken.manifest.json", file_size=12)
        await session.commit()

    client = FakeClient({13: b"not json at all"})
    service = CatalogService(client, [ChannelSpec(channel_id=CHANNEL, role=ChannelRole.ARCHIVE)])

    first = await service.resolve_manifests()
    assert first["failed"] == 1

    second = await service.resolve_manifests()
    assert second["resolved"] == 0 and second["failed"] == 0
    assert client.downloaded == [13], "a permanently broken manifest must not be retried forever"

    async with AsyncSessionLocal() as session:
        row = await session.scalar(select(CatalogItem).where(CatalogItem.tg_message_id == 13))
        assert row.enrich_error is not None
        assert row.chunked_original_name is None


async def test_resolution_is_bounded_and_resumable(clean_db):
    payloads = {}
    async with AsyncSessionLocal() as session:
        for mid in range(20, 25):
            await _add(session, tg_message_id=mid, artifact="manifest",
                       file_name=f"f{mid}.mp4.manifest.json", file_size=700)
            payloads[mid] = _manifest_bytes(name=f"f{mid}.mp4", sha=str(mid) * 32)
        await session.commit()

    service = CatalogService(FakeClient(payloads),
                             [ChannelSpec(channel_id=CHANNEL, role=ChannelRole.ARCHIVE)])

    first = await service.resolve_manifests(limit=2)
    assert first == {"resolved": 2, "failed": 0, "remaining": 3}

    second = await service.resolve_manifests(limit=10)
    assert second == {"resolved": 3, "failed": 0, "remaining": 0}


async def test_a_mirror_channel_manifest_is_left_alone(clean_db):
    async with AsyncSessionLocal() as session:
        row = CatalogItem(
            channel_id=-1004367643112,
            tg_message_id=30,
            channel_role=ChannelRole.MIRROR,
            media_kind="document",
            artifact="manifest",
            file_name="movie.mp4.manifest.json",
            file_size=800,
        )
        session.add(row)
        await session.commit()

    client = FakeClient({30: _manifest_bytes()})
    service = CatalogService(client, [ChannelSpec(channel_id=-1004367643112, role=ChannelRole.MIRROR)])
    result = await service.resolve_manifests()

    assert result["resolved"] == 0
    assert client.downloaded == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_catalog_manifest_rows.py -q`
Expected: FAIL — `AttributeError: 'CatalogService' object has no attribute 'resolve_manifests'`.

- [ ] **Step 3: Add the three columns**

In `app/models/database.py`, inside `CatalogItem`, immediately after the `sha256` column:

```python
    # A chunked upload appears as N "chunk" messages plus one "manifest" message.
    # The manifest's payload describes the logical original, so its identity is
    # stored here, on the row for the message that carries that description.
    # NULL on every row that is not a resolved manifest.
    chunked_original_name: Mapped[str | None] = mapped_column(
        String(512), nullable=True, index=True
    )
    chunked_total_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    chunked_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
```

Then, in the same file, add the `catalog_items` entry to `_COLUMN_MIGRATIONS`. `create_all`
only creates missing *tables*, so anyone who ran the catalog plan before this one already
has a `catalog_items` without these three columns, and would hit `no such column` on the
first lookup:

```python
    "catalog_items": {
        "chunked_original_name": "VARCHAR(512)",
        "chunked_total_size": "BIGINT",
        "chunked_sha256": "VARCHAR(64)",
    },
```

- [ ] **Step 4: Implement `resolve_manifests`**

In `app/services/catalog.py`, add the import and the method on `CatalogService`:

```python
import json

from app.services.chunking import MANIFEST_KIND


def parse_manifest(payload: bytes) -> tuple[str, int, str]:
    """(original_filename, total_size, sha256) from a manifest's bytes.

    Raises ValueError on anything that is not a manifest this repo wrote. The
    on-channel format is a contract: read it strictly rather than guessing.
    """
    document = json.loads(payload.decode("utf-8"))
    if document.get("kind") != MANIFEST_KIND:
        raise ValueError(f"not a chunked-file manifest: kind={document.get('kind')!r}")

    name = document["original_filename"]
    total_size = document["total_size"]
    sha256 = document["sha256"]
    if not isinstance(name, str) or not name:
        raise ValueError("original_filename missing or empty")
    if not isinstance(total_size, int) or total_size <= 0:
        raise ValueError(f"total_size not a positive integer: {total_size!r}")
    if not isinstance(sha256, str) or len(sha256) != 64:
        raise ValueError(f"sha256 not a 64-character digest: {sha256!r}")
    return name, total_size, sha256
```

And the method, placed next to `scan_all`:

```python
    async def resolve_manifests(self, limit: int = 50) -> dict[str, int]:
        """Read unresolved manifests and record the original each one describes.

        Bounded and resumable like every other loop that touches Telegram. A
        manifest that cannot be parsed records the reason and is never retried:
        one fetch per broken file, not one per run forever.
        """
        archive_ids = [int(spec.channel_id) for spec in self.archive_channels]
        if not archive_ids:
            return {"resolved": 0, "failed": 0, "remaining": 0}

        pending = (
            select(CatalogItem)
            .where(
                CatalogItem.artifact == "manifest",
                CatalogItem.channel_id.in_(archive_ids),
                CatalogItem.chunked_original_name.is_(None),
                CatalogItem.enrich_error.is_(None),
            )
            .order_by(CatalogItem.id)
        )

        async with AsyncSessionLocal() as session:
            rows = list((await session.scalars(pending.limit(limit))).all())

        resolved = failed = 0
        for row in rows:
            try:
                message = await self.client.get_messages(row.channel_id, row.tg_message_id)
                buffer = await self.client.download_media(message, in_memory=True)
                name, total_size, sha256 = parse_manifest(buffer.getvalue())
            except Exception as exc:  # noqa: BLE001 - recorded, not raised
                failed += 1
                async with AsyncSessionLocal() as session:
                    item = await session.get(CatalogItem, row.id)
                    item.enrich_error = f"manifest unreadable: {exc}"[:255]
                    await session.commit()
            else:
                resolved += 1
                async with AsyncSessionLocal() as session:
                    item = await session.get(CatalogItem, row.id)
                    item.chunked_original_name = name
                    item.chunked_total_size = total_size
                    item.chunked_sha256 = sha256
                    await session.commit()

            if self.scan_delay_seconds > 0:
                await asyncio.sleep(self.scan_delay_seconds)

        async with AsyncSessionLocal() as session:
            remaining = await session.scalar(
                select(func.count()).select_from(pending.subquery())
            )

        return {"resolved": resolved, "failed": failed, "remaining": int(remaining or 0)}
```

Add `func` to the existing `from sqlalchemy import ...` line if it is not already imported.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_catalog_manifest_rows.py -q`
Expected: PASS, 5 tests.

- [ ] **Step 6: Run the whole suite and the linter**

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
python3 -m compileall -q app scripts
```
Expected: all green. The three new columns are nullable, so existing catalog tests are unaffected.

- [ ] **Step 7: Commit**

```bash
git add app/models/database.py app/services/catalog.py tests/test_catalog_manifest_rows.py
git commit -m "feat(catalog): resolve chunked originals from their manifest rows

A chunked upload is N chunk messages plus one manifest message, and the
manifest's payload is the only place the original's name, total size and
whole-file hash exist on the channel. Reading it gives files above 2 GB the
strongest identity available, and leaves per-message rows per-message.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: what the server remembers about a device

**Files:**
- Modify: `app/models/database.py`
- Modify: `tests/test_db_migrations.py`

**Interfaces:**
- Consumes: `Base`, `_COLUMN_MIGRATIONS` from `app.models.database`.
- Produces: `DeviceVerdict`, `MatchTier`, `DeviceSnapshot`, `DeviceFinding`, `DeletionAudit`, all importable from `app.models.database`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db_migrations.py`:

```python
LEGACY_CATALOG_ITEMS = """
CREATE TABLE catalog_items (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    channel_id BIGINT NOT NULL,
    tg_message_id BIGINT NOT NULL,
    channel_role VARCHAR(8) NOT NULL,
    media_kind VARCHAR(32) NOT NULL,
    artifact VARCHAR(16),
    file_name VARCHAR(512),
    file_size BIGINT,
    message_date DATETIME,
    sha256 VARCHAR(64),
    source VARCHAR(16) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""


async def test_device_tables_are_created_on_a_legacy_database():
    """The three device tables appear on a database that predates them."""
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.exec_driver_sql(LEGACY_PHOTOS)

        await init_db()

        async with engine.begin() as conn:
            for table in ("device_snapshots", "device_findings", "deletion_audits"):
                result = await conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
                )
                assert result.fetchone() is not None, f"{table} was not created"
    finally:
        await engine.dispose()


async def test_catalog_items_gains_the_chunked_columns_in_place():
    """A catalog_items written before this feature is upgraded, not rebuilt."""
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.exec_driver_sql(LEGACY_CATALOG_ITEMS)
            await conn.exec_driver_sql(
                "INSERT INTO catalog_items "
                "(channel_id, tg_message_id, channel_role, media_kind, source, "
                " file_name, created_at, updated_at) "
                "VALUES (-1, 1, 'ARCHIVE', 'document', 'UNKNOWN', 'keep.jpg', "
                " '2026-01-01', '2026-01-01')"
            )

        await init_db()

        async with engine.begin() as conn:
            info = await conn.exec_driver_sql("PRAGMA table_info(catalog_items)")
            columns = {row[1] for row in info.fetchall()}
            assert {"chunked_original_name", "chunked_total_size", "chunked_sha256"} <= columns

            rows = await conn.exec_driver_sql("SELECT file_name FROM catalog_items")
            assert [r[0] for r in rows.fetchall()] == ["keep.jpg"], "existing rows survive"
    finally:
        await engine.dispose()


async def test_every_new_column_is_nullable_or_defaulted():
    """The additive rule: ALTER TABLE ADD COLUMN cannot add a bare NOT NULL."""
    for table, columns in _COLUMN_MIGRATIONS.items():
        for name, ddl in columns.items():
            upper = ddl.upper()
            assert "NOT NULL" not in upper or "DEFAULT" in upper, (
                f"{table}.{name} is NOT NULL without a DEFAULT: {ddl}"
            )
```

Add `DeletionAudit`, `DeviceFinding`, `DeviceSnapshot` to the existing import block at the top of the file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_db_migrations.py -q`
Expected: FAIL — `ImportError: cannot import name 'DeviceSnapshot'`.

- [ ] **Step 3: Add the enums and the three tables**

In `app/models/database.py`, after `RecoveryStatus`:

```python
class DeviceVerdict(str, Enum):
    """What a client may do with a local file.

    Only ARCHIVED authorises a deletion. The other three all mean "keep it",
    and differ in what the owner should do next: wait, decide, or investigate.
    """

    ARCHIVED = "ARCHIVED"
    IN_FLIGHT = "IN_FLIGHT"
    AMBIGUOUS = "AMBIGUOUS"
    NOT_ARCHIVED = "NOT_ARCHIVED"


class MatchTier(str, Enum):
    """How strong the evidence behind an ARCHIVED verdict is.

    HASH and FINGERPRINT are statements about content. NAME_SIZE is an
    inference from metadata, which is why it is the only tier the catalog's
    freshness can undermine.
    """

    HASH = "HASH"
    FINGERPRINT = "FINGERPRINT"
    NAME_SIZE = "NAME_SIZE"
```

And, after `RecoveryItem`:

```python
class DeviceSnapshot(Base):
    """One reconciliation run for one device: aggregates only.

    The ARCHIVED entries are the bulk and are deliberately not stored — they go
    back to the client in the response and, once acted on, survive here only as
    rows in deletion_audits.
    """

    __tablename__ = "device_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # The client's own clock, recorded as reported and never trusted for logic.
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    total_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    archived_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    archived_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    in_flight_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    in_flight_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    ambiguous_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ambiguous_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    not_archived_files: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    not_archived_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class DeviceFinding(Base):
    """A local file that is not ARCHIVED — the only rows worth keeping per file."""

    __tablename__ = "device_findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("device_snapshots.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relpath: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    verdict: Mapped[DeviceVerdict] = mapped_column(
        SqlEnum(DeviceVerdict, name="device_verdict", native_enum=False),
        nullable=False,
        index=True,
    )
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DeletionAudit(Base):
    """A local file the client reported deleting, and the message that holds it.

    Permanent, and deliberately without a foreign key to device_snapshots: it
    has to outlive snapshot pruning. This is what makes a deletion recoverable
    — from the channel, without this database.
    """

    __tablename__ = "deletion_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    relpath: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tier: Mapped[MatchTier] = mapped_column(
        SqlEnum(MatchTier, name="match_tier", native_enum=False), nullable=False
    )
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tg_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_db_migrations.py -q`
Expected: PASS.

- [ ] **Step 5: Run the whole suite and the linter**

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
python3 -m compileall -q app scripts
```

- [ ] **Step 6: Commit**

```bash
git add app/models/database.py tests/test_db_migrations.py
git commit -m "feat(db): device snapshots, findings and a permanent deletion audit

Stores the aggregate per device and the entries that need a decision. The
ARCHIVED bulk is never stored: it is the part nobody queries again, except
through the audit once it is gone.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: the verdict engine

The heart of the feature. `decide()` is pure — no database, no network, no clock — so its tests are a table of cases with no fakes at all.

**Files:**
- Create: `app/services/reconcile.py`
- Test: `tests/test_reconcile_rules.py`, `tests/test_reconcile_service.py`

**Interfaces:**
- Consumes: `CatalogItem`, `ChannelRole`, `Photo`, `PhotoStatus`, `DeviceVerdict`, `MatchTier` from `app.models.database`; `CatalogItem.chunked_*` from Task 1.
- Produces:
  - `Candidate(tg_message_id: int, channel_id: int, file_name: str, file_size: int | None, sha256: str | None)`
  - `Decision(verdict: DeviceVerdict, tier: MatchTier | None, reason: str | None, tg_message_id: int | None, channel_id: int | None)`
  - `decide(*, name, size, sha256, mtime, pipeline_status, candidates, catalog_newest) -> Decision`
  - `ReconcileService()` with `async def evaluate(entries: Sequence[dict]) -> list[Decision]` and `async def catalog_freshness() -> dict`
  - `CatalogNeverScanned` exception

- [ ] **Step 1: Write the failing rule tests**

```python
# tests/test_reconcile_rules.py
"""The verdict table, one case per row. No database, no fakes, no network.

decide() is pure on purpose: every rule that decides whether a photo may be
deleted is exercised here, in isolation, at the cost of nothing.
"""
from datetime import datetime, timezone

import pytest

from app.models.database import DeviceVerdict, MatchTier, PhotoStatus
from app.services.reconcile import Candidate, decide

NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
CATALOG_NEWEST = datetime(2026, 7, 20, tzinfo=timezone.utc)

PXL = Candidate(
    tg_message_id=48211,
    channel_id=-1002637897512,
    file_name="PXL_20260713_115033830.jpg",
    file_size=3_412_887,
    sha256=None,
)


def _decide(**overrides):
    kwargs = dict(
        name="PXL_20260713_115033830.jpg",
        size=3_412_887,
        sha256=None,
        mtime=NOW,
        pipeline_status=None,
        candidates=[PXL],
        catalog_newest=CATALOG_NEWEST,
    )
    kwargs.update(overrides)
    return decide(**kwargs)


def test_exact_name_and_size_is_archived_at_name_size_tier():
    result = _decide()
    assert result.verdict is DeviceVerdict.ARCHIVED
    assert result.tier is MatchTier.NAME_SIZE
    assert result.tg_message_id == 48211


def test_a_hash_match_is_archived_at_hash_tier():
    hashed = Candidate(1, -1, "movie.mp4", 5_000_000_000, "b" * 64)
    result = _decide(name="movie.mp4", size=5_000_000_000, sha256="b" * 64, candidates=[hashed])
    assert result.verdict is DeviceVerdict.ARCHIVED
    assert result.tier is MatchTier.HASH


def test_a_hash_match_wins_even_when_the_size_disagrees():
    """Content proof outranks metadata: a size that disagrees is the metadata's problem."""
    hashed = Candidate(1, -1, "movie.mp4", 999, "b" * 64)
    result = _decide(name="movie.mp4", size=5_000_000_000, sha256="b" * 64, candidates=[hashed])
    assert result.verdict is DeviceVerdict.ARCHIVED
    assert result.tier is MatchTier.HASH


def test_same_name_different_size_is_ambiguous():
    result = _decide(size=99)
    assert result.verdict is DeviceVerdict.AMBIGUOUS
    assert result.reason == "size_mismatch"
    assert result.tg_message_id == 48211, "the candidate to verify must be named"


def test_a_case_only_match_is_ambiguous_never_archived():
    """/sdcard is case-insensitive and Telegram is not: report it, do not trust it."""
    result = _decide(name="pxl_20260713_115033830.JPG")
    assert result.verdict is DeviceVerdict.AMBIGUOUS
    assert result.reason == "case_only_match"


def test_no_candidate_is_not_archived():
    result = _decide(candidates=[])
    assert result.verdict is DeviceVerdict.NOT_ARCHIVED


@pytest.mark.parametrize(
    "status",
    [
        PhotoStatus.PENDING,
        PhotoStatus.DOWNLOADED,
        PhotoStatus.CHUNK_UPLOADING,
        PhotoStatus.TG_UPLOADED,
        PhotoStatus.COMPRESSED,
        PhotoStatus.ODROID_UPLOADED,
    ],
)
def test_a_file_still_in_the_pipeline_is_in_flight(status):
    result = _decide(pipeline_status=status)
    assert result.verdict is DeviceVerdict.IN_FLIGHT
    assert result.reason == "pipeline_in_progress"


def test_a_failed_file_is_not_archived_and_says_why():
    result = _decide(pipeline_status=PhotoStatus.FAILED)
    assert result.verdict is DeviceVerdict.NOT_ARCHIVED
    assert result.reason == "pipeline_failed"


def test_a_skipped_file_is_not_archived_even_though_photos_has_a_row():
    """The 93 SKIPPED rows exist in photos and were never archived at all."""
    result = _decide(pipeline_status=PhotoStatus.SKIPPED)
    assert result.verdict is DeviceVerdict.NOT_ARCHIVED
    assert result.reason == "unsupported_type"


def test_completed_corroborates_but_does_not_decide():
    result = _decide(pipeline_status=PhotoStatus.COMPLETED)
    assert result.verdict is DeviceVerdict.ARCHIVED
    assert result.tier is MatchTier.NAME_SIZE


def test_completed_without_a_catalog_row_is_ambiguous_not_archived():
    """The 62 files in the measurements: the honest answer points at a rescan."""
    result = _decide(pipeline_status=PhotoStatus.COMPLETED, candidates=[])
    assert result.verdict is DeviceVerdict.AMBIGUOUS
    assert result.reason == "completed_but_absent_from_catalog"


def test_a_file_newer_than_the_catalog_cannot_be_archived_by_name_and_size():
    result = _decide(mtime=datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert result.verdict is DeviceVerdict.IN_FLIGHT
    assert result.reason == "catalog_older_than_file"


def test_a_missing_mtime_fails_closed():
    """The client supplies mtime and can get it wrong. Absent means unsafe."""
    result = _decide(mtime=None)
    assert result.verdict is DeviceVerdict.IN_FLIGHT
    assert result.reason == "catalog_older_than_file"


def test_freshness_does_not_demote_a_hash_match():
    """A content hash is proof; dates cannot undermine it."""
    hashed = Candidate(1, -1, "movie.mp4", 5_000_000_000, "b" * 64)
    result = _decide(
        name="movie.mp4",
        size=5_000_000_000,
        sha256="b" * 64,
        candidates=[hashed],
        mtime=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    assert result.verdict is DeviceVerdict.ARCHIVED
    assert result.tier is MatchTier.HASH


def test_a_zero_byte_file_is_never_archived_by_name_and_size():
    """Every empty file shares a size, so a size match proves nothing about content."""
    empty = Candidate(1, -1, "empty.jpg", 0, None)
    result = _decide(name="empty.jpg", size=0, candidates=[empty])
    assert result.verdict is DeviceVerdict.AMBIGUOUS
    assert result.reason == "zero_byte_file"
```

- [ ] **Step 2: Write the failing service tests**

```python
# tests/test_reconcile_service.py
"""Candidate selection against a real catalog_items, and the empty-catalog refusal."""
from datetime import datetime, timezone

import pytest

from app.models.database import (
    AsyncSessionLocal,
    CatalogItem,
    ChannelRole,
    DeviceVerdict,
    MatchTier,
    Photo,
    PhotoStatus,
)
from app.services.reconcile import CatalogNeverScanned, ReconcileService

ARCHIVE = -1002637897512
MIRROR = -1004367643112
SCANNED = datetime(2026, 7, 20, tzinfo=timezone.utc)
OLDER = datetime(2026, 7, 1, tzinfo=timezone.utc)


async def _row(**kwargs):
    defaults = dict(
        channel_id=ARCHIVE,
        channel_role=ChannelRole.ARCHIVE,
        media_kind="document",
        message_date=SCANNED,
    )
    defaults.update(kwargs)
    async with AsyncSessionLocal() as session:
        session.add(CatalogItem(**defaults))
        await session.commit()


def _entry(name="a.jpg", size=100, **kwargs):
    entry = {"relpath": f"DCIM/{name}", "name": name, "size": size,
             "mtime": OLDER.isoformat(), "sha256": None}
    entry.update(kwargs)
    return entry


async def test_a_match_in_an_archive_channel_is_archived(clean_db):
    await _row(tg_message_id=1, file_name="a.jpg", file_size=100)
    [decision] = await ReconcileService().evaluate([_entry()])
    assert decision.verdict is DeviceVerdict.ARCHIVED
    assert decision.tier is MatchTier.NAME_SIZE


async def test_a_match_only_in_the_mirror_channel_is_not_proof(clean_db):
    """The browse channel holds Telegram-recompressed copies, not originals."""
    await _row(tg_message_id=2, channel_id=MIRROR, channel_role=ChannelRole.MIRROR,
               file_name="a.jpg", file_size=100)
    await _row(tg_message_id=3, file_name="unrelated.jpg", file_size=1)
    [decision] = await ReconcileService().evaluate([_entry()])
    assert decision.verdict is DeviceVerdict.NOT_ARCHIVED


async def test_a_native_photo_row_is_never_proof(clean_db):
    await _row(tg_message_id=4, media_kind="photo", file_name="a.jpg", file_size=100)
    await _row(tg_message_id=5, file_name="unrelated.jpg", file_size=1)
    [decision] = await ReconcileService().evaluate([_entry()])
    assert decision.verdict is DeviceVerdict.NOT_ARCHIVED


async def test_a_chunk_part_row_is_never_a_candidate(clean_db):
    """A local file named like a chunk is not an archived original."""
    await _row(tg_message_id=6, artifact="chunk",
               file_name="movie.mp4.part001-of-003", file_size=1_950_000_000)
    [decision] = await ReconcileService().evaluate(
        [_entry(name="movie.mp4.part001-of-003", size=1_950_000_000)]
    )
    assert decision.verdict is DeviceVerdict.NOT_ARCHIVED


async def test_a_resolved_manifest_makes_the_chunked_original_findable(clean_db):
    await _row(tg_message_id=7, artifact="manifest",
               file_name="movie.mp4.manifest.json", file_size=800,
               chunked_original_name="movie.mp4", chunked_total_size=5_000_000_000,
               chunked_sha256="c" * 64)
    [decision] = await ReconcileService().evaluate(
        [_entry(name="movie.mp4", size=5_000_000_000)]
    )
    assert decision.verdict is DeviceVerdict.ARCHIVED
    assert decision.tier is MatchTier.NAME_SIZE
    assert decision.tg_message_id == 7


async def test_an_unresolved_manifest_leaves_the_original_invisible(clean_db):
    await _row(tg_message_id=8, artifact="manifest",
               file_name="movie.mp4.manifest.json", file_size=800)
    [decision] = await ReconcileService().evaluate(
        [_entry(name="movie.mp4", size=5_000_000_000)]
    )
    assert decision.verdict is DeviceVerdict.NOT_ARCHIVED


async def test_the_pipeline_overlay_is_read_from_photos(clean_db):
    await _row(tg_message_id=9, file_name="b.jpg", file_size=100)
    async with AsyncSessionLocal() as session:
        session.add(Photo(mega_path="/phone_bkp/b.jpg", status=PhotoStatus.SKIPPED))
        await session.commit()

    [decision] = await ReconcileService().evaluate([_entry(name="b.jpg")])
    assert decision.verdict is DeviceVerdict.NOT_ARCHIVED
    assert decision.reason == "unsupported_type"


async def test_an_unscanned_catalog_refuses_to_answer(clean_db):
    """Answering from an empty table would read as 'nothing you own is backed up'."""
    with pytest.raises(CatalogNeverScanned):
        await ReconcileService().evaluate([_entry()])
```

- [ ] **Step 3: Run both test files to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_reconcile_rules.py tests/test_reconcile_service.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.reconcile'`.

- [ ] **Step 4: Write `decide()`**

```python
# app/services/reconcile.py
"""From a device's file inventory to a verdict per file.

`decide` is deliberately pure: no database, no network, no clock. Every rule
that decides whether a photograph may be deleted lives in one function that can
be read in one sitting and tested without a single fake.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select

from app.models.database import (
    AsyncSessionLocal,
    CatalogItem,
    ChannelRole,
    DeviceVerdict,
    MatchTier,
    Photo,
    PhotoStatus,
)

IN_FLIGHT_STATUSES = frozenset(
    {
        PhotoStatus.PENDING,
        PhotoStatus.DOWNLOADED,
        PhotoStatus.CHUNK_UPLOADING,
        PhotoStatus.TG_UPLOADED,
        PhotoStatus.COMPRESSED,
        PhotoStatus.ODROID_UPLOADED,
    }
)


class CatalogNeverScanned(RuntimeError):
    """No archive channel has ever been scanned, so no verdict can be honest."""


@dataclass(frozen=True)
class Candidate:
    """A catalog row that could hold the bytes of a local file."""

    tg_message_id: int
    channel_id: int
    file_name: str
    file_size: int | None
    sha256: str | None


@dataclass(frozen=True)
class Decision:
    verdict: DeviceVerdict
    tier: MatchTier | None = None
    reason: str | None = None
    tg_message_id: int | None = None
    channel_id: int | None = None


def _archived(tier: MatchTier, candidate: Candidate) -> Decision:
    return Decision(
        verdict=DeviceVerdict.ARCHIVED,
        tier=tier,
        tg_message_id=candidate.tg_message_id,
        channel_id=candidate.channel_id,
    )


def decide(
    *,
    name: str,
    size: int,
    sha256: str | None,
    mtime: datetime | None,
    pipeline_status: PhotoStatus | None,
    candidates: Sequence[Candidate],
    catalog_newest: datetime | None,
) -> Decision:
    """The verdict for one local file. Same inputs, same answer, always."""
    # 1. What the pipeline knows, which the catalog cannot know.
    if pipeline_status is not None:
        if pipeline_status in IN_FLIGHT_STATUSES:
            return Decision(DeviceVerdict.IN_FLIGHT, reason="pipeline_in_progress")
        if pipeline_status is PhotoStatus.FAILED:
            return Decision(DeviceVerdict.NOT_ARCHIVED, reason="pipeline_failed")
        if pipeline_status is PhotoStatus.SKIPPED:
            return Decision(DeviceVerdict.NOT_ARCHIVED, reason="unsupported_type")
        # COMPLETED deliberately falls through: it is corroboration, not proof.

    # 2. Content proof. Dates cannot undermine a hash.
    if sha256:
        for candidate in candidates:
            if candidate.sha256 and candidate.sha256 == sha256:
                return _archived(MatchTier.HASH, candidate)

    exact_name = [c for c in candidates if c.file_name == name]

    # 3. Metadata inference, which a stale catalog can undermine.
    if size > 0:
        for candidate in exact_name:
            if candidate.file_size == size:
                if _newer_than_catalog(mtime, catalog_newest):
                    return Decision(
                        DeviceVerdict.IN_FLIGHT, reason="catalog_older_than_file"
                    )
                return _archived(MatchTier.NAME_SIZE, candidate)

    # 4. Everything a human should look at before deciding.
    if size <= 0 and exact_name:
        return Decision(
            DeviceVerdict.AMBIGUOUS,
            reason="zero_byte_file",
            tg_message_id=exact_name[0].tg_message_id,
            channel_id=exact_name[0].channel_id,
        )
    if exact_name:
        return Decision(
            DeviceVerdict.AMBIGUOUS,
            reason="size_mismatch",
            tg_message_id=exact_name[0].tg_message_id,
            channel_id=exact_name[0].channel_id,
        )

    lowered = name.lower()
    for candidate in candidates:
        if candidate.file_name.lower() == lowered:
            return Decision(
                DeviceVerdict.AMBIGUOUS,
                reason="case_only_match",
                tg_message_id=candidate.tg_message_id,
                channel_id=candidate.channel_id,
            )

    if pipeline_status is PhotoStatus.COMPLETED:
        return Decision(
            DeviceVerdict.AMBIGUOUS, reason="completed_but_absent_from_catalog"
        )

    return Decision(DeviceVerdict.NOT_ARCHIVED, reason="no_match")


def _newer_than_catalog(mtime: datetime | None, catalog_newest: datetime | None) -> bool:
    """Fail closed: an unknown date is treated as newer than anything scanned."""
    if catalog_newest is None:
        return True
    if mtime is None:
        return True
    if mtime.tzinfo is None:
        mtime = mtime.replace(tzinfo=timezone.utc)
    if catalog_newest.tzinfo is None:
        catalog_newest = catalog_newest.replace(tzinfo=timezone.utc)
    return mtime > catalog_newest
```

- [ ] **Step 5: Write `ReconcileService`**

Append to `app/services/reconcile.py`:

```python
def _parse_mtime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class ReconcileService:
    """Fetches candidates for an inventory and asks `decide` about each entry."""

    async def catalog_freshness(self) -> dict[str, object]:
        """Newest archive-channel message the catalog has seen, and how many rows."""
        async with AsyncSessionLocal() as session:
            newest = await session.scalar(
                select(func.max(CatalogItem.message_date)).where(
                    CatalogItem.channel_role == ChannelRole.ARCHIVE
                )
            )
            rows = await session.scalar(
                select(func.count())
                .select_from(CatalogItem)
                .where(CatalogItem.channel_role == ChannelRole.ARCHIVE)
            )
        return {"newest_message_date": newest, "archive_rows": int(rows or 0)}

    async def evaluate(self, entries: Sequence[dict]) -> list[Decision]:
        freshness = await self.catalog_freshness()
        if freshness["archive_rows"] == 0:
            raise CatalogNeverScanned(
                "No archive channel has been scanned. Run POST /api/catalog/scan first."
            )

        catalog_newest = freshness["newest_message_date"]
        names = {str(entry.get("name") or "") for entry in entries}
        names.discard("")
        candidates = await self._candidates_for(names)
        statuses = await self._pipeline_statuses(names)

        decisions = []
        for entry in entries:
            name = str(entry.get("name") or "")
            decisions.append(
                decide(
                    name=name,
                    size=int(entry.get("size") or 0),
                    sha256=entry.get("sha256"),
                    mtime=_parse_mtime(entry.get("mtime")),
                    pipeline_status=statuses.get(name),
                    candidates=candidates.get(name.lower(), ()),
                    catalog_newest=catalog_newest,
                )
            )
        return decisions

    async def _candidates_for(self, names: set[str]) -> dict[str, tuple[Candidate, ...]]:
        """Candidates keyed by lower-cased name, so a case-only match is findable.

        Two sources, both restricted to archive channels: ordinary documents,
        and resolved manifests standing in for the chunked original they
        describe. Native photos and chunk parts are excluded here rather than
        later, so no rule downstream can accidentally treat one as evidence.
        """
        if not names:
            return {}

        lowered = {n.lower() for n in names}
        found: dict[str, list[Candidate]] = {}

        async with AsyncSessionLocal() as session:
            documents = await session.scalars(
                select(CatalogItem).where(
                    CatalogItem.channel_role == ChannelRole.ARCHIVE,
                    CatalogItem.artifact.is_(None),
                    CatalogItem.media_kind != "photo",
                    func.lower(CatalogItem.file_name).in_(lowered),
                )
            )
            for row in documents:
                found.setdefault(row.file_name.lower(), []).append(
                    Candidate(
                        tg_message_id=row.tg_message_id,
                        channel_id=row.channel_id,
                        file_name=row.file_name,
                        file_size=row.file_size,
                        sha256=row.sha256,
                    )
                )

            manifests = await session.scalars(
                select(CatalogItem).where(
                    CatalogItem.channel_role == ChannelRole.ARCHIVE,
                    CatalogItem.artifact == "manifest",
                    CatalogItem.chunked_original_name.isnot(None),
                    func.lower(CatalogItem.chunked_original_name).in_(lowered),
                )
            )
            for row in manifests:
                found.setdefault(row.chunked_original_name.lower(), []).append(
                    Candidate(
                        tg_message_id=row.tg_message_id,
                        channel_id=row.channel_id,
                        file_name=row.chunked_original_name,
                        file_size=row.chunked_total_size,
                        sha256=row.chunked_sha256,
                    )
                )

        return {key: tuple(value) for key, value in found.items()}

    async def _pipeline_statuses(self, names: set[str]) -> dict[str, PhotoStatus]:
        """The worker's state for each name, by the basename of its mega_path."""
        if not names:
            return {}
        async with AsyncSessionLocal() as session:
            rows = await session.execute(select(Photo.mega_path, Photo.status))
        statuses: dict[str, PhotoStatus] = {}
        for mega_path, status in rows:
            basename = mega_path.rsplit("/", 1)[-1]
            if basename in names:
                statuses[basename] = status
        return statuses
```

Add `func` to the `from sqlalchemy import select` line: `from sqlalchemy import func, select`.

- [ ] **Step 6: Run both test files to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_reconcile_rules.py tests/test_reconcile_service.py -q`
Expected: PASS, 23 tests.

- [ ] **Step 7: Run the whole suite and the linter**

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
python3 -m compileall -q app scripts
```

- [ ] **Step 8: Commit**

```bash
git add app/services/reconcile.py tests/test_reconcile_rules.py tests/test_reconcile_service.py
git commit -m "feat(reconcile): the verdict engine

decide() is pure: no database, no network, no clock. Only ARCHIVED authorises
a deletion, and it needs either a content hash or an exact name and size
against a row in an archive channel. Native photos, chunk parts and mirror
copies are excluded from candidacy rather than rejected later, so no rule
downstream can mistake one for evidence.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: settling an ambiguous file without downloading it

**Files:**
- Modify: `app/services/telegram.py`
- Test: `tests/test_partial_fingerprint.py`

**Interfaces:**
- Consumes: `TelegramService` and its `self.client`.
- Produces: `TelegramService.partial_fingerprint(message, *, file_size: int, window: int = 262144) -> dict[str, str]` returning `{"head_sha256": ..., "tail_sha256": ...}`, and `TelegramService.fingerprint_message(channel_id: int, message_id: int, *, file_size: int, window: int = 262144) -> dict[str, str]`, which fetches the message first so a route never has to reach into `self.client`.

**One transport detail, stated rather than hidden.** Telegram streams files in 1 MiB chunks, so a range smaller than that cannot be requested. `window` is therefore the number of bytes *hashed* per side; the client fetches the first and last whole chunk and slices them. At the default of 256 KiB, settling one ambiguous file costs 2 MiB of traffic and no disk.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_partial_fingerprint.py
"""The fingerprint reads the two ends of a file and never the middle."""
import hashlib
from types import SimpleNamespace

from app.services.telegram import TelegramService

CHUNK = 1024 * 1024


class FakeClient:
    """Serves a known blob in 1 MiB chunks, recording which ones were asked for."""

    def __init__(self, blob: bytes):
        self.blob = blob
        self.requested: list[tuple[int, int]] = []

    async def stream_media(self, message, *, offset: int = 0, limit: int = 0):
        self.requested.append((offset, limit))
        start = offset * CHUNK
        end = start + (limit or 1) * CHUNK
        yield self.blob[start:end]


def _service(client):
    return TelegramService(client, channel_id=-1, upload_delay_seconds=0)


async def test_head_and_tail_are_hashed_and_the_middle_is_never_fetched():
    blob = bytes(range(256)) * 20_000  # 5.12 MB, > 5 chunks
    client = FakeClient(blob)
    window = 262_144

    result = await _service(client).partial_fingerprint(
        SimpleNamespace(id=1), file_size=len(blob), window=window
    )

    last_chunk = (len(blob) - 1) // CHUNK
    assert result["head_sha256"] == hashlib.sha256(blob[:window]).hexdigest()
    assert result["tail_sha256"] == hashlib.sha256(blob[-window:]).hexdigest()
    assert client.requested == [(0, 1), (last_chunk, 1)]
    assert len(client.requested) == 2, "exactly two chunks, whatever the file size"


async def test_a_file_smaller_than_the_window_hashes_what_exists():
    blob = b"tiny file contents"
    client = FakeClient(blob)

    result = await _service(client).partial_fingerprint(
        SimpleNamespace(id=1), file_size=len(blob), window=262_144
    )

    whole = hashlib.sha256(blob).hexdigest()
    assert result["head_sha256"] == whole
    assert result["tail_sha256"] == whole


async def test_two_identical_blobs_fingerprint_identically():
    blob = bytes(range(256)) * 20_000
    first = await _service(FakeClient(blob)).partial_fingerprint(
        SimpleNamespace(id=1), file_size=len(blob)
    )
    second = await _service(FakeClient(bytes(blob))).partial_fingerprint(
        SimpleNamespace(id=2), file_size=len(blob)
    )
    assert first == second


async def test_fingerprint_message_fetches_the_message_then_hashes_it():
    blob = bytes(range(256)) * 20_000

    class FetchingClient(FakeClient):
        def __init__(self, blob):
            super().__init__(blob)
            self.asked: list[tuple[int, int]] = []

        async def get_messages(self, chat_id, message_ids):
            self.asked.append((chat_id, message_ids))
            return SimpleNamespace(id=message_ids)

    client = FetchingClient(blob)
    result = await _service(client).fingerprint_message(-100, 7, file_size=len(blob))

    assert client.asked == [(-100, 7)]
    assert result["head_sha256"] == hashlib.sha256(blob[:262_144]).hexdigest()


async def test_a_difference_in_the_tail_changes_the_fingerprint():
    blob = bytes(range(256)) * 20_000
    tampered = blob[:-1] + b"\x00"
    first = await _service(FakeClient(blob)).partial_fingerprint(
        SimpleNamespace(id=1), file_size=len(blob)
    )
    second = await _service(FakeClient(tampered)).partial_fingerprint(
        SimpleNamespace(id=2), file_size=len(tampered)
    )
    assert first["head_sha256"] == second["head_sha256"]
    assert first["tail_sha256"] != second["tail_sha256"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_partial_fingerprint.py -q`
Expected: FAIL — `AttributeError: 'TelegramService' object has no attribute 'partial_fingerprint'`.

- [ ] **Step 3: Implement it**

In `app/services/telegram.py`, add `import hashlib` if absent, the constant, and the method on `TelegramService`:

```python
STREAM_CHUNK_BYTES = 1024 * 1024


    async def partial_fingerprint(
        self, message, *, file_size: int, window: int = 262_144
    ) -> dict[str, str]:
        """Hash the first and last `window` bytes of an archived file.

        Settles an ambiguous local file against its archived copy without
        downloading it. Telegram streams in 1 MiB chunks, so this fetches the
        first and last whole chunk and slices them: two chunks, whatever the
        file's size, and nothing written to disk.
        """
        head = await self._read_chunk(message, 0)
        last_index = max((file_size - 1) // STREAM_CHUNK_BYTES, 0)
        tail = head if last_index == 0 else await self._read_chunk(message, last_index)

        return {
            "head_sha256": hashlib.sha256(head[:window]).hexdigest(),
            "tail_sha256": hashlib.sha256(tail[-window:]).hexdigest(),
        }

    async def fingerprint_message(
        self, channel_id: int, message_id: int, *, file_size: int, window: int = 262_144
    ) -> dict[str, str]:
        """Fetch one archived message and fingerprint it."""
        message = await self.client.get_messages(channel_id, message_id)
        return await self.partial_fingerprint(message, file_size=file_size, window=window)

    async def _read_chunk(self, message, index: int) -> bytes:
        buffer = bytearray()
        async for part in self.client.stream_media(message, offset=index, limit=1):
            buffer.extend(part)
        return bytes(buffer)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_partial_fingerprint.py -q`
Expected: PASS, 5 tests.

- [ ] **Step 5: Run the whole suite and the linter**

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
python3 -m compileall -q app scripts
```

- [ ] **Step 6: Commit**

```bash
git add app/services/telegram.py tests/test_partial_fingerprint.py
git commit -m "feat(telegram): partial fingerprint for settling ambiguous files

Hashes the first and last window of an archived file by streaming two chunks,
so a local file whose size disagrees with the catalog can be settled for about
2 MiB of traffic instead of a full download.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: the endpoints, the wiring and the protocol document

**Files:**
- Modify: `app/services/reconcile.py` (constructor kwargs, snapshot persistence)
- Modify: `app/api/routes.py`
- Modify: `app/main.py`
- Modify: `docker-compose.yml`, `AGENTS.md`, `docs/REFERENCE.md`
- Test: `tests/test_reconcile_api.py`

**Interfaces:**
- Consumes: `ReconcileService.evaluate`, `CatalogNeverScanned`, `Decision` (Task 3); `DeviceSnapshot`, `DeviceFinding`, `DeletionAudit`, `DeviceVerdict`, `MatchTier` (Task 2); `TelegramService.partial_fingerprint` (Task 4).
- Produces: `ReconcileService.__init__(*, max_entries: int = 10000, fingerprint_bytes: int = 262144)`, `ReconcileService.reconcile(device_id, entries, *, snapshot_id=None, taken_at=None, final=True) -> dict`, `ReconcileService.record_deletions(device_id, records) -> int`, `ReconcileService.latest_snapshot(device_id) -> dict | None`, and `SnapshotConflict`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_reconcile_api.py
"""The endpoints: auth, the refusals, continuation rules and the audit.

Built like tests/test_api.py: a bare FastAPI with the router mounted and
app.state set by hand, never running the lifespan.
"""
import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import router
from app.models.database import (
    AsyncSessionLocal,
    CatalogItem,
    ChannelRole,
    DeletionAudit,
)
from app.services.reconcile import ReconcileService

ARCHIVE = -1002637897512
KEY = "test-key"


class FakeTelegram:
    """Duck-types only fingerprint_message, returning a fixed digest pair."""

    def __init__(self, fingerprint: dict[str, str]):
        self.fingerprint = fingerprint

    async def fingerprint_message(self, channel_id, message_id, *, file_size, window):
        return dict(self.fingerprint)


@pytest.fixture
def app_state(client):
    """The FastAPI state the client fixture built, for tests that add a service."""
    return client.app.state


@pytest.fixture
def client(clean_db, monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)

    async def seed():
        async with AsyncSessionLocal() as session:
            session.add(
                CatalogItem(
                    channel_id=ARCHIVE,
                    tg_message_id=1,
                    channel_role=ChannelRole.ARCHIVE,
                    media_kind="document",
                    file_name="a.jpg",
                    file_size=100,
                    message_date=datetime(2026, 7, 20, tzinfo=timezone.utc),
                )
            )
            await session.commit()

    asyncio.run(seed())

    app = FastAPI()
    app.include_router(router)
    app.state.reconcile = ReconcileService(max_entries=3)
    with TestClient(app) as test_client:
        yield test_client
    asyncio.run(_dispose())


async def _dispose():
    from app.models.database import engine

    await engine.dispose()


def _entry(name="a.jpg", size=100):
    return {"relpath": f"DCIM/{name}", "name": name, "size": size,
            "mtime": "2026-07-01T00:00:00Z", "sha256": None}


def _post(client, path, body):
    return client.post(path, json=body, headers={"X-Api-Key": KEY})


def test_reconcile_requires_the_api_key(client):
    response = client.post("/api/devices/pixel/reconcile", json={"entries": []})
    assert response.status_code == 401


def test_reconcile_returns_a_verdict_per_entry_and_a_summary(client):
    response = _post(client, "/api/devices/pixel/reconcile",
                     {"entries": [_entry(), _entry("missing.jpg", 5)]})
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["ARCHIVED"]["files"] == 1
    assert body["summary"]["NOT_ARCHIVED"]["files"] == 1
    assert [e["verdict"] for e in body["entries"]] == ["ARCHIVED", "NOT_ARCHIVED"]
    assert body["catalog"]["archive_rows"] == 1


def test_only_non_archived_entries_are_stored_as_findings(client):
    body = _post(client, "/api/devices/pixel/reconcile",
                 {"entries": [_entry(), _entry("missing.jpg", 5)]}).json()
    snapshot = client.get(f"/api/devices/pixel/snapshot", headers={"X-Api-Key": KEY}).json()
    assert snapshot["snapshot"]["archived_files"] == 1
    assert [f["file_name"] for f in snapshot["findings"]] == ["missing.jpg"]
    assert body["snapshot_id"] == snapshot["snapshot"]["id"]


def test_too_many_entries_is_refused_with_413(client):
    response = _post(client, "/api/devices/pixel/reconcile",
                     {"entries": [_entry(f"f{i}.jpg") for i in range(4)]})
    assert response.status_code == 413


def test_a_continuation_accumulates_into_one_snapshot(client):
    first = _post(client, "/api/devices/pixel/reconcile",
                  {"entries": [_entry()], "final": False}).json()
    second = _post(client, "/api/devices/pixel/reconcile",
                   {"entries": [_entry("missing.jpg", 5)],
                    "snapshot_id": first["snapshot_id"], "final": True}).json()

    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["summary"]["ARCHIVED"]["files"] == 1
    assert second["summary"]["NOT_ARCHIVED"]["files"] == 1


def test_a_snapshot_belonging_to_another_device_is_refused(client):
    first = _post(client, "/api/devices/pixel/reconcile",
                  {"entries": [_entry()], "final": False}).json()
    response = _post(client, "/api/devices/samsung/reconcile",
                     {"entries": [_entry()], "snapshot_id": first["snapshot_id"]})
    assert response.status_code == 409


def test_a_finished_snapshot_cannot_be_continued(client):
    first = _post(client, "/api/devices/pixel/reconcile",
                  {"entries": [_entry()], "final": True}).json()
    response = _post(client, "/api/devices/pixel/reconcile",
                     {"entries": [_entry()], "snapshot_id": first["snapshot_id"]})
    assert response.status_code == 409


def test_an_unscanned_catalog_is_a_409_not_a_confident_answer(clean_db, monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)
    app = FastAPI()
    app.include_router(router)
    app.state.reconcile = ReconcileService()
    with TestClient(app) as test_client:
        response = test_client.post("/api/devices/pixel/reconcile",
                                    json={"entries": [_entry()]},
                                    headers={"X-Api-Key": KEY})
    assert response.status_code == 409
    assert "scan" in response.json()["detail"].lower()
    asyncio.run(_dispose())


def test_recorded_deletions_are_audited(client):
    response = _post(client, "/api/devices/pixel/deletions", {
        "deleted": [{
            "relpath": "DCIM/a.jpg", "name": "a.jpg", "size": 100,
            "tier": "NAME_SIZE", "channel_id": ARCHIVE, "tg_message_id": 1,
            "deleted_at": "2026-09-24T10:00:00Z",
        }]
    })
    assert response.status_code == 200
    assert response.json()["recorded"] == 1

    async def read():
        async with AsyncSessionLocal() as session:
            from sqlalchemy import select

            return list((await session.scalars(select(DeletionAudit))).all())

    rows = asyncio.run(read())
    assert len(rows) == 1
    assert rows[0].tg_message_id == 1
    assert rows[0].device_id == "pixel"


def test_verify_compares_the_clients_fingerprint_with_the_archived_copy(client, app_state):
    app_state.telegram = FakeTelegram({"head_sha256": "h" * 64, "tail_sha256": "t" * 64})

    agreeing = _post(client, "/api/vault/verify", {
        "channel_id": ARCHIVE, "tg_message_id": 1, "file_size": 100,
        "head_sha256": "h" * 64, "tail_sha256": "t" * 64,
    })
    assert agreeing.json()["match"] is True

    disagreeing = _post(client, "/api/vault/verify", {
        "channel_id": ARCHIVE, "tg_message_id": 1, "file_size": 100,
        "head_sha256": "h" * 64, "tail_sha256": "x" * 64,
    })
    assert disagreeing.json()["match"] is False


def test_verify_without_a_telegram_service_is_503(client):
    response = _post(client, "/api/vault/verify", {
        "channel_id": ARCHIVE, "tg_message_id": 1, "file_size": 100,
        "head_sha256": "h" * 64, "tail_sha256": "t" * 64,
    })
    assert response.status_code == 503


def test_scan_and_resolve_routes_reach_the_catalog_service(client, app_state):
    """Without these two, nothing could ever populate the catalog."""

    class FakeCatalog:
        def __init__(self):
            self.calls = []

        async def scan_all(self):
            self.calls.append("scan_all")
            return {"-100": {"scanned": 3, "ingested": 3, "updated": 0}}

        async def match_all(self, state_db_path=None):
            self.calls.append("match_all")
            return {"worker": 0, "backup_script": 0}

        async def resolve_manifests(self, limit=50):
            self.calls.append(f"resolve_manifests:{limit}")
            return {"resolved": 1, "failed": 0, "remaining": 0}

    app_state.catalog = FakeCatalog()

    scanned = _post(client, "/api/catalog/scan", {})
    assert scanned.status_code == 200
    assert scanned.json()["scanned"]["-100"]["ingested"] == 3

    resolved = client.post("/api/catalog/resolve-manifests", params={"limit": 10},
                           headers={"X-Api-Key": KEY})
    assert resolved.json()["resolved"] == 1
    assert app_state.catalog.calls == ["scan_all", "match_all", "resolve_manifests:10"]


def test_the_catalog_routes_are_503_without_the_service(client):
    assert _post(client, "/api/catalog/scan", {}).status_code == 503


def test_catalog_freshness_reports_what_the_client_needs(client):
    body = client.get("/api/catalog/freshness", headers={"X-Api-Key": KEY}).json()
    assert body["archive_rows"] == 1
    assert body["newest_message_date"].startswith("2026-07-20")


def test_lookup_answers_a_single_file(client):
    body = client.get("/api/vault/lookup", params={"name": "a.jpg", "size": 100},
                      headers={"X-Api-Key": KEY}).json()
    assert body["verdict"] == "ARCHIVED"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_reconcile_api.py -q`
Expected: FAIL — `TypeError: ReconcileService() takes no keyword arguments`.

- [ ] **Step 3: Extend `ReconcileService` with persistence**

Append to `app/services/reconcile.py`:

```python
class SnapshotConflict(RuntimeError):
    """A continuation that does not belong to this device, or is already closed."""


_VERDICT_COLUMNS = {
    DeviceVerdict.ARCHIVED: ("archived_files", "archived_bytes"),
    DeviceVerdict.IN_FLIGHT: ("in_flight_files", "in_flight_bytes"),
    DeviceVerdict.AMBIGUOUS: ("ambiguous_files", "ambiguous_bytes"),
    DeviceVerdict.NOT_ARCHIVED: ("not_archived_files", "not_archived_bytes"),
}
```

Add the constructor at the top of the class, before `catalog_freshness`:

```python
    def __init__(self, *, max_entries: int = 10_000, fingerprint_bytes: int = 262_144) -> None:
        self.max_entries = max_entries
        self.fingerprint_bytes = fingerprint_bytes
```

And the three public methods, after `evaluate`:

```python
    async def reconcile(
        self,
        device_id: str,
        entries: Sequence[dict],
        *,
        snapshot_id: int | None = None,
        taken_at: datetime | None = None,
        final: bool = True,
    ) -> dict[str, object]:
        """Evaluate one batch, fold it into a snapshot, keep only what matters.

        ARCHIVED entries are returned and not stored: they are the bulk, and the
        only time anyone looks at one again is through the audit, once it is gone.
        """
        decisions = await self.evaluate(entries)

        async with AsyncSessionLocal() as session:
            if snapshot_id is None:
                snapshot = DeviceSnapshot(device_id=device_id, taken_at=taken_at)
                session.add(snapshot)
                await session.flush()
            else:
                snapshot = await session.get(DeviceSnapshot, snapshot_id)
                if snapshot is None or snapshot.device_id != device_id:
                    raise SnapshotConflict(
                        f"Snapshot {snapshot_id} does not belong to device {device_id!r}."
                    )
                if snapshot.completed_at is not None:
                    raise SnapshotConflict(f"Snapshot {snapshot_id} is already closed.")

            for entry, decision in zip(entries, decisions):
                size = int(entry.get("size") or 0)
                snapshot.total_files += 1
                snapshot.total_bytes += size
                files_column, bytes_column = _VERDICT_COLUMNS[decision.verdict]
                setattr(snapshot, files_column, getattr(snapshot, files_column) + 1)
                setattr(snapshot, bytes_column, getattr(snapshot, bytes_column) + size)

                if decision.verdict is not DeviceVerdict.ARCHIVED:
                    session.add(
                        DeviceFinding(
                            snapshot_id=snapshot.id,
                            relpath=str(entry.get("relpath") or ""),
                            file_name=str(entry.get("name") or ""),
                            file_size=size,
                            verdict=decision.verdict,
                            reason=decision.reason,
                        )
                    )

            if final:
                snapshot.completed_at = datetime.now(timezone.utc)

            await session.commit()
            await session.refresh(snapshot)
            summary = _summary_of(snapshot)
            new_id = snapshot.id

        return {
            "snapshot_id": new_id,
            "catalog": await self.catalog_freshness(),
            "summary": summary,
            "entries": [
                {
                    "relpath": entry.get("relpath"),
                    "verdict": decision.verdict.value,
                    "tier": decision.tier.value if decision.tier else None,
                    "reason": decision.reason,
                    "channel_id": decision.channel_id,
                    "tg_message_id": decision.tg_message_id,
                }
                for entry, decision in zip(entries, decisions)
            ],
        }

    async def latest_snapshot(self, device_id: str) -> dict[str, object] | None:
        async with AsyncSessionLocal() as session:
            snapshot = await session.scalar(
                select(DeviceSnapshot)
                .where(DeviceSnapshot.device_id == device_id)
                .order_by(DeviceSnapshot.id.desc())
                .limit(1)
            )
            if snapshot is None:
                return None
            findings = list(
                (
                    await session.scalars(
                        select(DeviceFinding)
                        .where(DeviceFinding.snapshot_id == snapshot.id)
                        .order_by(DeviceFinding.id)
                    )
                ).all()
            )
            return {
                "snapshot": {
                    "id": snapshot.id,
                    "device_id": snapshot.device_id,
                    "taken_at": snapshot.taken_at,
                    "completed_at": snapshot.completed_at,
                    "total_files": snapshot.total_files,
                    "total_bytes": snapshot.total_bytes,
                    "archived_files": snapshot.archived_files,
                    "archived_bytes": snapshot.archived_bytes,
                    "in_flight_files": snapshot.in_flight_files,
                    "ambiguous_files": snapshot.ambiguous_files,
                    "not_archived_files": snapshot.not_archived_files,
                },
                "findings": [
                    {
                        "relpath": f.relpath,
                        "file_name": f.file_name,
                        "file_size": f.file_size,
                        "verdict": f.verdict.value,
                        "reason": f.reason,
                    }
                    for f in findings
                ],
            }

    async def record_deletions(self, device_id: str, records: Sequence[dict]) -> int:
        """Write the permanent audit. This is what makes a deletion recoverable."""
        async with AsyncSessionLocal() as session:
            for record in records:
                session.add(
                    DeletionAudit(
                        device_id=device_id,
                        relpath=str(record["relpath"]),
                        file_name=str(record["name"]),
                        file_size=int(record["size"]),
                        tier=MatchTier(record["tier"]),
                        channel_id=int(record["channel_id"]),
                        tg_message_id=int(record["tg_message_id"]),
                        deleted_at=_parse_mtime(record.get("deleted_at")),
                    )
                )
            await session.commit()
        return len(records)


def _summary_of(snapshot: DeviceSnapshot) -> dict[str, dict[str, int]]:
    return {
        "ARCHIVED": {"files": snapshot.archived_files, "bytes": snapshot.archived_bytes},
        "IN_FLIGHT": {"files": snapshot.in_flight_files, "bytes": snapshot.in_flight_bytes},
        "AMBIGUOUS": {"files": snapshot.ambiguous_files, "bytes": snapshot.ambiguous_bytes},
        "NOT_ARCHIVED": {
            "files": snapshot.not_archived_files,
            "bytes": snapshot.not_archived_bytes,
        },
        "TOTAL": {"files": snapshot.total_files, "bytes": snapshot.total_bytes},
    }
```

Extend the import from `app.models.database` with `DeletionAudit`, `DeviceFinding`, `DeviceSnapshot`, `DeviceVerdict`, `MatchTier`.

- [ ] **Step 4: Add the endpoints**

In `app/api/routes.py`, add the imports and the models:

```python
from app.services.reconcile import CatalogNeverScanned, ReconcileService, SnapshotConflict


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
```

And the eight routes:

```python
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
    entry = {"relpath": name, "name": name, "size": size, "mtime": None, "sha256": None}
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
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
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
    archived = await telegram.fingerprint_message(
        payload.channel_id,
        payload.tg_message_id,
        file_size=payload.file_size,
        window=service.fingerprint_bytes,
    )
    matched = (
        archived["head_sha256"] == payload.head_sha256
        and archived["tail_sha256"] == payload.tail_sha256
    )
    return {"match": matched, **archived}
```

- [ ] **Step 5: Wire it into the composition root**

In `app/main.py`, inside `lifespan`, after the recovery service is built:

```python
    reconcile_service = ReconcileService(
        max_entries=int(os.getenv("RECONCILE_MAX_ENTRIES", "10000")),
        fingerprint_bytes=int(os.getenv("RECONCILE_FINGERPRINT_BYTES", "262144")),
    )
    app.state.reconcile = reconcile_service
    # The verify endpoint needs the service, not just the raw client already on state.
    app.state.telegram = telegram_service
```

Add `from app.services.reconcile import ReconcileService` to the imports.

- [ ] **Step 6: Document the two knobs and the protocol**

`docker-compose.yml`, under the service's `environment:`:

```yaml
      RECONCILE_MAX_ENTRIES: ${RECONCILE_MAX_ENTRIES:-10000}
      RECONCILE_FINGERPRINT_BYTES: ${RECONCILE_FINGERPRINT_BYTES:-262144}
```

`AGENTS.md`, in the environment-variable table:

```markdown
| `RECONCILE_MAX_ENTRIES` | `10000` | Inventory entries accepted per `reconcile` call; more is refused with 413 and a message telling the client to continue against the same `snapshot_id`. |
| `RECONCILE_FINGERPRINT_BYTES` | `262144` | Bytes hashed at each end of a file when settling an ambiguous match. Telegram streams in 1 MiB chunks, so the traffic cost is 2 MiB regardless. |
```

`docs/REFERENCE.md` gains a section documenting the inventory manifest as a stable format — one entry is `{relpath, name, size, mtime, sha256?}`, `sha256` optional, `mtime` ISO-8601 — the six endpoints with their request and response shapes, the four verdicts, the three tiers, and the rule that only `ARCHIVED` authorises a deletion. This is the contract any future client implements, so it belongs beside the chunk and manifest formats rather than in a plan.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_reconcile_api.py -q`
Expected: PASS, 16 tests.

- [ ] **Step 8: Run the whole suite, the linter and the compile check**

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
python3 -m compileall -q app scripts
```
Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add app/services/reconcile.py app/api/routes.py app/main.py \
        tests/test_reconcile_api.py docker-compose.yml AGENTS.md docs/REFERENCE.md
git commit -m "feat(api): device reconciliation endpoints and the inventory protocol

A client posts its inventory and gets a verdict per file, a summary, and the
catalog's age so it never has to guess. Large libraries continue against one
snapshot_id; a snapshot belonging to another device, or already closed, is
refused. An unscanned catalog is a 409 rather than a confident 'none of your
files are backed up'.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## What this plan deliberately leaves undone

- **No client.** Nothing here enumerates a phone or deletes a file. The protocol in `docs/REFERENCE.md` is the contract a Termux agent, an adb script or an Android app implements later, and the choice stays open.
- **No dashboard panel.** The endpoints return everything a panel would need; building one is a separate, purely presentational change.
- **No ingest path.** `NOT_ARCHIVED` tells the owner that MEGA's camera upload has a gap. Closing it by uploading from the device is its own feature.
- **Images present only as native `photo` messages stay invisible** and come back `NOT_ARCHIVED`, as the spec states. 3,201 messages are affected, and the verdict errs towards keeping files.
