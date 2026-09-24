# Catalog (reconciled state) + Immich bridge — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one index over all three Telegram channels that says what exists, where it came from, what is duplicated and what is missing, enrich every image with its capture date and GPS by reading only the head of each file, and export the photo half of the archive into a read-only tree that Immich indexes as an external library.

**Architecture:** One new service, `app/services/catalog.py`, modelled on the existing `RecoveryService` (bounded resumable batches, one `AsyncSessionLocal` session per unit of work, state in a new `catalog_items` table keyed by `(channel_id, tg_message_id)`). One new standalone script, `scripts/export_gallery.py`, modelled on `scripts/backup_local_folder.py` (dry-run default, `--limit`, always prints a report, never deletes). One small best-effort addition to `PhotoWorker._finalize` so newly archived images land in the gallery tree for free. Immich itself is configured by hand and is not this repo's code.

**Tech Stack:** Python 3.11-compatible (local venv is 3.14, CI is 3.11), FastAPI, SQLAlchemy 2 async on SQLite via aiosqlite, kurigram (pyrogram namespace), Pillow + pillow-heif, pytest with `asyncio_mode = auto`.

**Spec:** `docs/superpowers/specs/2026-09-21-catalog-and-immich-bridge-design.md`

## Global Constraints

- **Nothing in this plan writes to Telegram.** No send, no caption edit, no delete. The catalog is strictly a reader. There is therefore no `--apply` flag anywhere in it — the dry-run-by-default rule exists for destructive features and this one has no destructive mode.
- **Never modify the on-channel formats** — chunk names (`name.partNNN-of-MMM`), chunk/manifest captions, manifest JSON. Reading and classifying them is the only interaction allowed.
- **`_finalize` remains the only step that deletes the source.** The gallery copy is written inside `_finalize`, strictly before the deletion, and a failure to write it must never fail the archival step (same best-effort posture as browse-channel mirroring).
- **Additive schema only.** `catalog_items` is a new table created by `create_all`; no existing column is renamed or dropped, and `_COLUMN_MIGRATIONS` is not touched.
- **Every new env var takes all five edits**: parse in `lifespan`, pass as a constructor kwarg, add to `docker-compose.yml`, document in `AGENTS.md` and in `docs/REFERENCE.md`. The `add-config-knob` skill covers the sequence. Each task that introduces a knob does all five in that task.
- **Test style is the house style**: hand-written fakes that duck-type only the methods under test (`SimpleNamespace` messages, a `FakeClient` class), never `unittest.mock` patching; `tmp_path` whenever the filesystem is touched; the `clean_db` fixture for anything using the database; no network, no credentials, no real Telegram/MEGA/SFTP call anywhere.
- **Pacing is a safety requirement, not a tuning knob.** Every loop that touches Telegram sleeps between items and processes a bounded batch. Losing the account means losing access to 322.6 GB of the only offsite copy.
- **Verification contract** — run all four from the repo root before claiming a task is done:
  ```bash
  .venv/bin/python -m pytest -q
  .venv/bin/python -m ruff check .
  python3 -m compileall -q app scripts
  ```
  (`ruff.toml` enables only `E9` + `F`, `target-version = "py311"`.)

**One refinement of the spec, decided here.** The spec's sketch of `catalog_items` carried `is_chunked` and `manifest_tg_message_id`. Those describe a *logical file*, but a catalog row describes a *single message*, and at scan time a chunked upload appears as N part-messages plus one manifest message. Counting eight chunk parts as eight photos would make every number in the report wrong. This plan therefore replaces both fields with a single per-message `artifact` column (`"chunk"` | `"manifest"` | `NULL`), and leaves the logical-file view to `photos`, which the `photo_id` link reaches. Everything else follows the spec as written.

---

## File Structure

**Create:**

| file | responsibility |
|---|---|
| `app/services/catalog.py` | `CatalogService`: scan, match, enrich, report. Pure helpers (`classify_artifact`, `extract_metadata`, `gallery_relpath`) live at module level so they are testable without a client or a database. |
| `scripts/export_gallery.py` | Standalone resumable backfill of image originals into the gallery tree. |
| `tests/test_catalog_scan.py` | Scan idempotency, multi-channel isolation, artifact classification, unset channel skipped. |
| `tests/test_catalog_match.py` | Provenance by message id, `sha256` fallback, unmatched stays `UNKNOWN`. |
| `tests/test_catalog_enrich.py` | Truncated-head EXIF parsing, native-photo short circuit, permanent-failure handling. |
| `tests/test_catalog_report.py` | Every counter, especially "in the DB but not in the channel" and hash-confirmed duplicates. |
| `tests/test_gallery_export.py` | Tree layout, date fallback, collisions, videos skipped, dry-run writes nothing, pre-existing file adopted without download. |

**Modify:**

| file | change |
|---|---|
| `app/models/database.py` | Add `ChannelRole`, `CatalogSource`, `CatalogItem`. Import `Float`. |
| `app/main.py` | Parse the new env vars, build `CatalogService`, store on `app.state.catalog`. |
| `app/api/routes.py` | `POST /api/catalog/scan`, `POST /api/catalog/enrich`, `GET /api/catalog/report`, `GET /api/catalog/items`. |
| `app/worker.py` | Best-effort gallery copy at the top of `_finalize`. |
| `tests/test_worker_state_machine.py` | Cover the forward-path copy and its failure mode. |
| `docker-compose.yml`, `AGENTS.md`, `docs/REFERENCE.md` | The four new knobs, per the five-edit rule. |

---

### Task 1: `catalog_items` schema

**Files:**
- Modify: `app/models/database.py`
- Test: `tests/test_catalog_scan.py` (created here, grows in Task 2)

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `ChannelRole`, `CatalogSource`, `CatalogItem` importable from `app.models.database`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_catalog_scan.py`:

```python
"""Catalog schema and channel scan."""
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.database import (
    AsyncSessionLocal,
    CatalogItem,
    CatalogSource,
    ChannelRole,
)


async def test_a_catalog_row_defaults_to_unknown_provenance_and_no_enrichment(clean_db):
    async with AsyncSessionLocal() as session:
        session.add(
            CatalogItem(
                channel_id=-1002637897512,
                tg_message_id=1,
                channel_role=ChannelRole.ARCHIVE,
                media_kind="document",
                file_name="PXL_20230331_135108850.jpg",
                file_size=2_400_000,
                message_date=datetime(2025, 5, 3, 2, 7, 53),
            )
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))

    assert item.source is CatalogSource.UNKNOWN
    assert item.artifact is None
    assert item.enriched_at is None
    assert item.taken_at is None
    assert item.gps_lat is None
    assert item.exported_path is None


async def test_the_same_message_cannot_be_catalogued_twice_in_one_channel(clean_db):
    async with AsyncSessionLocal() as session:
        session.add_all(
            [
                CatalogItem(
                    channel_id=-100,
                    tg_message_id=7,
                    channel_role=ChannelRole.ARCHIVE,
                    media_kind="photo",
                ),
                CatalogItem(
                    channel_id=-100,
                    tg_message_id=7,
                    channel_role=ChannelRole.ARCHIVE,
                    media_kind="photo",
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_the_same_message_id_in_two_channels_is_two_rows(clean_db):
    """Message ids restart per channel, so the key must be the pair."""
    async with AsyncSessionLocal() as session:
        session.add_all(
            [
                CatalogItem(
                    channel_id=-100,
                    tg_message_id=7,
                    channel_role=ChannelRole.ARCHIVE,
                    media_kind="photo",
                ),
                CatalogItem(
                    channel_id=-200,
                    tg_message_id=7,
                    channel_role=ChannelRole.ARCHIVE,
                    media_kind="photo",
                ),
            ]
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        assert len((await session.scalars(select(CatalogItem))).all()) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_catalog_scan.py -q
```

Expected: collection error — `ImportError: cannot import name 'CatalogItem' from 'app.models.database'`.

- [ ] **Step 3: Add the model**

In `app/models/database.py`, add `Float` to the existing `from sqlalchemy import (...)` block (keep the list alphabetical: it goes between `Enum as SqlEnum` and `ForeignKey`). Then append after the `RecoveryItem` class and *before* the `_COLUMN_MIGRATIONS` comment block:

```python
class ChannelRole(str, Enum):
    """What a channel is for.

    ARCHIVE holds originals and is a gallery source. MIRROR holds native
    Telegram copies of things already archived elsewhere: it is scanned so the
    report can show drift, and is never enriched or exported.
    """

    ARCHIVE = "ARCHIVE"
    MIRROR = "MIRROR"


class CatalogSource(str, Enum):
    WORKER = "WORKER"
    BACKUP_SCRIPT = "BACKUP_SCRIPT"
    UNKNOWN = "UNKNOWN"


class CatalogItem(Base):
    """One media message in one channel.

    The channel is the archive; this table is a cache of what we know about it.
    That is why the key is the message and not a local path: a row can only be
    created by seeing the message, and a row that stops matching a message is
    exactly the signal the reconciliation report exists to surface.
    """

    __tablename__ = "catalog_items"
    __table_args__ = (
        UniqueConstraint("channel_id", "tg_message_id", name="uq_catalog_channel_message"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    tg_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    channel_role: Mapped[ChannelRole] = mapped_column(
        SqlEnum(ChannelRole, name="channel_role", native_enum=False),
        default=ChannelRole.ARCHIVE,
        nullable=False,
    )

    media_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Per-message classification: a chunked upload is N "chunk" rows plus one
    # "manifest" row. Counting those as N+1 photos would corrupt every number in
    # the report, so they are labelled here and excluded by the counters.
    artifact: Mapped[str | None] = mapped_column(String(16), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # When it was posted, which for a migrated archive is not when it was shot.
    message_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gps_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    camera_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # NULL means never attempted. Set even on failure, so a permanently
    # unreadable file costs one fetch rather than one per run, forever.
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enrich_error: Mapped[str | None] = mapped_column(String(255), nullable=True)

    source: Mapped[CatalogSource] = mapped_column(
        SqlEnum(CatalogSource, name="catalog_source", native_enum=False),
        default=CatalogSource.UNKNOWN,
        nullable=False,
        index=True,
    )
    photo_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("photos.id"), nullable=True)
    backup_rel_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    exported_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_catalog_scan.py -q
```

Expected: 3 passed.

- [ ] **Step 5: Run the whole suite and the linter**

```bash
.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check .
```

Expected: everything passes. `test_db_migrations.py` in particular must stay green — it upgrades a legacy SQLite file in place, and a new table must not disturb it.

- [ ] **Step 6: Commit**

```bash
rtk git add app/models/database.py tests/test_catalog_scan.py
rtk git commit -m "feat(catalog): add catalog_items, keyed by channel and message"
```

---

### Task 2: channel scan

**Files:**
- Create: `app/services/catalog.py`
- Modify: `app/main.py`, `docker-compose.yml`, `AGENTS.md`, `docs/REFERENCE.md`
- Test: `tests/test_catalog_scan.py` (extend)

**Interfaces:**
- Consumes: `CatalogItem`, `ChannelRole`, `CatalogSource` from Task 1.
- Produces:
  - `classify_artifact(file_name: str | None) -> str | None` — `"chunk"`, `"manifest"` or `None`
  - `ChannelSpec` — frozen dataclass with `channel_id: int | str` and `role: ChannelRole`
  - `CatalogService(client, channels: Sequence[ChannelSpec], *, scan_delay_seconds: float = 0.0)`
  - `await CatalogService.scan_channel(spec: ChannelSpec) -> dict[str, int]` — keys `scanned`, `ingested`, `updated`
  - `await CatalogService.scan_all() -> dict[str, dict[str, int]]` — keyed by `str(channel_id)`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_catalog_scan.py`:

```python
from types import SimpleNamespace

from app.services.catalog import CatalogService, ChannelSpec, classify_artifact


def _doc(mid, name, size=1000, date=None):
    return SimpleNamespace(
        id=mid,
        photo=None,
        video=None,
        animation=None,
        document=SimpleNamespace(file_name=name, file_size=size, mime_type="image/jpeg"),
        caption=None,
        date=date or datetime(2025, 6, 1, 12, 0, 0),
        empty=False,
    )


def _photo(mid, size=5000, date=None):
    return SimpleNamespace(
        id=mid,
        photo=SimpleNamespace(file_size=size),
        video=None,
        animation=None,
        document=None,
        caption=None,
        date=date or datetime(2025, 6, 1, 12, 0, 0),
        empty=False,
    )


class FakeClient:
    """Duck-types only get_chat_history, which is all the scan uses."""

    def __init__(self, history: dict[int, list]):
        self.history = history
        self.calls: list[int] = []

    async def get_chat_history(self, chat_id):
        self.calls.append(chat_id)
        for message in self.history.get(chat_id, []):
            yield message


def test_chunk_parts_and_manifests_are_classified_and_plain_names_are_not():
    assert classify_artifact("movie.mp4.part001-of-012") == "chunk"
    assert classify_artifact("movie.mp4.manifest.json") == "manifest"
    assert classify_artifact("PXL_20230331_135108850.jpg") is None
    assert classify_artifact(None) is None


async def test_a_scan_ingests_every_media_message_with_its_channel_and_role(clean_db):
    client = FakeClient({-100: [_doc(1, "a.jpg"), _photo(2), _doc(3, "b.mp4.part001-of-002")]})
    service = CatalogService(client, [ChannelSpec(-100, ChannelRole.ARCHIVE)])

    result = await service.scan_channel(ChannelSpec(-100, ChannelRole.ARCHIVE))

    assert result == {"scanned": 3, "ingested": 3, "updated": 0}
    async with AsyncSessionLocal() as session:
        rows = {i.tg_message_id: i for i in (await session.scalars(select(CatalogItem))).all()}
    assert rows[1].media_kind == "document" and rows[1].artifact is None
    assert rows[2].media_kind == "photo" and rows[2].file_name is None
    assert rows[3].artifact == "chunk"
    assert all(r.channel_id == -100 for r in rows.values())
    assert all(r.channel_role is ChannelRole.ARCHIVE for r in rows.values())


async def test_rescanning_preserves_enrichment_and_export_state(clean_db):
    """The scan is a cache refresh, not a reset: it must never lose derived work."""
    client = FakeClient({-100: [_doc(1, "a.jpg")]})
    spec = ChannelSpec(-100, ChannelRole.ARCHIVE)
    service = CatalogService(client, [spec])
    await service.scan_channel(spec)

    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
        item.taken_at = datetime(2023, 3, 31, 15, 51, 8)
        item.gps_lat, item.gps_lon = 44.49, 11.34
        item.enriched_at = datetime(2026, 9, 21, 10, 0, 0)
        item.exported_path = "2023/2023-03-31/a.jpg"
        await session.commit()

    second = await service.scan_channel(spec)

    assert second == {"scanned": 1, "ingested": 0, "updated": 0}
    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.taken_at == datetime(2023, 3, 31, 15, 51, 8)
    assert item.gps_lat == 44.49
    assert item.exported_path == "2023/2023-03-31/a.jpg"


async def test_a_changed_file_size_updates_the_row_without_clearing_enrichment(clean_db):
    spec = ChannelSpec(-100, ChannelRole.ARCHIVE)
    service = CatalogService(FakeClient({-100: [_doc(1, "a.jpg", size=1000)]}), [spec])
    await service.scan_channel(spec)

    grown = CatalogService(FakeClient({-100: [_doc(1, "a.jpg", size=2000)]}), [spec])
    result = await grown.scan_channel(spec)

    assert result == {"scanned": 1, "ingested": 0, "updated": 1}
    async with AsyncSessionLocal() as session:
        assert (await session.scalar(select(CatalogItem))).file_size == 2000


async def test_scan_all_covers_every_configured_channel_with_its_own_role(clean_db):
    client = FakeClient({-100: [_doc(1, "a.jpg")], -200: [_photo(1)]})
    service = CatalogService(
        client,
        [ChannelSpec(-100, ChannelRole.ARCHIVE), ChannelSpec(-200, ChannelRole.MIRROR)],
    )

    results = await service.scan_all()

    assert set(results) == {"-100", "-200"}
    async with AsyncSessionLocal() as session:
        roles = {
            (i.channel_id, i.channel_role)
            for i in (await session.scalars(select(CatalogItem))).all()
        }
    assert roles == {(-100, ChannelRole.ARCHIVE), (-200, ChannelRole.MIRROR)}


async def test_a_service_with_no_channels_scans_nothing_rather_than_erroring(clean_db):
    """An unset IPHONE_CHANNEL_ID must be a no-op, not a crash on boot."""
    service = CatalogService(FakeClient({}), [])
    assert await service.scan_all() == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_catalog_scan.py -q
```

Expected: collection error — `ModuleNotFoundError: No module named 'app.services.catalog'`.

- [ ] **Step 3: Write the service**

Create `app/services/catalog.py`:

```python
"""One index over every channel this vault writes to.

The governing principle is that the channel is the archive and this table is a
cache of what we know about it. Scans therefore only ever add or refresh facts
that come from the message itself; anything derived later (EXIF, export state)
is owned by other operations and is never cleared by a rescan.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import select

from app.models.database import (
    AsyncSessionLocal,
    CatalogItem,
    ChannelRole,
)

logger = logging.getLogger(__name__)

MEDIA_KINDS = ("photo", "video", "document", "animation")

# Same contract as app/services/chunking.py: name.partNNN-of-MMM plus a JSON
# manifest. These are vault artifacts, not user media, and must not be counted
# as photos by anything downstream.
CHUNK_PART_RE = re.compile(r"\.part\d+-of-\d+$")
MANIFEST_SUFFIX = ".manifest.json"

PROGRESS_EVERY = 500


def classify_artifact(file_name: str | None) -> str | None:
    if not file_name:
        return None
    stem = file_name.strip()
    if CHUNK_PART_RE.search(stem):
        return "chunk"
    if stem.endswith(MANIFEST_SUFFIX):
        return "manifest"
    return None


@dataclass(frozen=True)
class ChannelSpec:
    channel_id: int | str
    role: ChannelRole


def _media_info(message) -> tuple[str, str | None, int | None, str | None] | None:
    """(kind, file_name, file_size, mime_type) or None for a non-media message."""
    if getattr(message, "photo", None) is not None:
        # Native photos carry no file name: Telegram re-encoded them on upload.
        return "photo", None, message.photo.file_size, "image/jpeg"
    if getattr(message, "video", None) is not None:
        video = message.video
        return "video", video.file_name, video.file_size, getattr(video, "mime_type", None)
    if getattr(message, "animation", None) is not None:
        anim = message.animation
        return "animation", anim.file_name, anim.file_size, getattr(anim, "mime_type", None)
    if getattr(message, "document", None) is not None:
        doc = message.document
        return "document", doc.file_name, doc.file_size, getattr(doc, "mime_type", None)
    return None


class CatalogService:
    def __init__(
        self,
        client,
        channels: Sequence[ChannelSpec],
        *,
        scan_delay_seconds: float = 0.0,
    ) -> None:
        self.client = client
        self.channels = tuple(channels)
        self.scan_delay_seconds = scan_delay_seconds

    @property
    def archive_channels(self) -> tuple[ChannelSpec, ...]:
        return tuple(c for c in self.channels if c.role is ChannelRole.ARCHIVE)

    async def scan_channel(self, spec: ChannelSpec) -> dict[str, int]:
        scanned = ingested = updated = 0

        async for message in self.client.get_chat_history(spec.channel_id):
            scanned += 1
            info = _media_info(message)
            if info is None:
                continue

            kind, file_name, file_size, mime_type = info
            outcome = await self._upsert(spec, message, kind, file_name, file_size, mime_type)
            if outcome == "ingested":
                ingested += 1
            elif outcome == "updated":
                updated += 1

            if scanned % PROGRESS_EVERY == 0:
                logger.info(
                    "Catalog scan %s: %s seen, %s new, %s refreshed.",
                    spec.channel_id,
                    scanned,
                    ingested,
                    updated,
                )

        logger.info(
            "Catalog scan %s finished: %s messages, %s new, %s refreshed.",
            spec.channel_id,
            scanned,
            ingested,
            updated,
        )
        return {"scanned": scanned, "ingested": ingested, "updated": updated}

    async def scan_all(self) -> dict[str, dict[str, int]]:
        results: dict[str, dict[str, int]] = {}
        for spec in self.channels:
            results[str(spec.channel_id)] = await self.scan_channel(spec)
            if self.scan_delay_seconds > 0:
                await asyncio.sleep(self.scan_delay_seconds)
        return results

    async def _upsert(
        self,
        spec: ChannelSpec,
        message,
        kind: str,
        file_name: str | None,
        file_size: int | None,
        mime_type: str | None,
    ) -> str:
        channel_id = int(spec.channel_id)
        async with AsyncSessionLocal() as session:
            existing = await session.scalar(
                select(CatalogItem).where(
                    CatalogItem.channel_id == channel_id,
                    CatalogItem.tg_message_id == message.id,
                )
            )

            if existing is None:
                session.add(
                    CatalogItem(
                        channel_id=channel_id,
                        tg_message_id=message.id,
                        channel_role=spec.role,
                        media_kind=kind,
                        artifact=classify_artifact(file_name),
                        file_name=file_name,
                        file_size=file_size,
                        mime_type=mime_type,
                        message_date=getattr(message, "date", None),
                    )
                )
                await session.commit()
                return "ingested"

            # Refresh only facts that come from the message. Enrichment and
            # export state belong to other operations and are left alone.
            changed = False
            for field, value in (
                ("media_kind", kind),
                ("file_name", file_name),
                ("file_size", file_size),
                ("mime_type", mime_type),
                ("message_date", getattr(message, "date", None)),
                ("channel_role", spec.role),
                ("artifact", classify_artifact(file_name)),
            ):
                if value is not None and getattr(existing, field) != value:
                    setattr(existing, field, value)
                    changed = True

            if changed:
                await session.commit()
                return "updated"
            return "unchanged"
```

- [ ] **Step 4: Add the `mime_type` column the service writes**

The tests in Task 1 did not need it, but `_upsert` stores it. In `app/models/database.py`, add to `CatalogItem` directly after `file_size`:

```python
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_catalog_scan.py -q
```

Expected: 9 passed.

- [ ] **Step 6: Wire it into the composition root**

In `app/main.py`, inside `lifespan` after the `RecoveryService` is built, add:

```python
        iphone_channel_raw = _optional_env("IPHONE_CHANNEL_ID")
        catalog_channels = [ChannelSpec(telegram_channel_id, ChannelRole.ARCHIVE)]
        if browse_channel_id is not None:
            catalog_channels.append(ChannelSpec(browse_channel_id, ChannelRole.MIRROR))
        if iphone_channel_raw:
            catalog_channels.append(
                ChannelSpec(_parse_int_or_str(iphone_channel_raw), ChannelRole.ARCHIVE)
            )

        catalog = CatalogService(
            telegram_client,
            catalog_channels,
            scan_delay_seconds=float(os.getenv("CATALOG_SCAN_DELAY", "2")),
        )
```

and next to the other `app.state` assignments:

```python
        app.state.catalog = catalog
```

Add the import beside the existing service imports:

```python
from app.models.database import ChannelRole
from app.services.catalog import CatalogService, ChannelSpec
```

(If `browse_channel_id` is bound inside a narrower scope in the current file, hoist it to the same scope as the other service variables — it is already computed from `BROWSE_CHANNEL_ID` a few lines above.)

- [ ] **Step 7: Document the two new knobs**

`docker-compose.yml`, under the app service's `environment:`:

```yaml
      - IPHONE_CHANNEL_ID=${IPHONE_CHANNEL_ID:-}
      - CATALOG_SCAN_DELAY=${CATALOG_SCAN_DELAY:-2}
```

Add to the environment-variable tables in both `AGENTS.md` and `docs/REFERENCE.md` — **add rows, do not rewrite surrounding prose**:

| variable | default | meaning |
|---|---|---|
| `IPHONE_CHANNEL_ID` | unset | Third channel to catalogue, created by `scripts/backup_local_folder.py`. Its id is in the `meta` table of that script's state DB. Unset means the channel is skipped. |
| `CATALOG_SCAN_DELAY` | `2` | Seconds between channels during a full scan. |

- [ ] **Step 8: Verify the whole suite, the linter and the compile check**

```bash
.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check . && python3 -m compileall -q app scripts
```

- [ ] **Step 9: Commit**

```bash
rtk git add app/services/catalog.py app/models/database.py app/main.py tests/test_catalog_scan.py docker-compose.yml AGENTS.md docs/REFERENCE.md
rtk git commit -m "feat(catalog): scan every configured channel into catalog_items"
```

---

### Task 3: provenance matching

**Files:**
- Modify: `app/services/catalog.py`
- Test: `tests/test_catalog_match.py`

**Interfaces:**
- Consumes: `CatalogService`, `CatalogItem`, `CatalogSource` from Tasks 1-2.
- Produces:
  - `await CatalogService.match_worker() -> int` — rows newly attributed to the worker
  - `await CatalogService.match_backup_db(state_db_path: str | Path) -> int` — rows newly attributed to `backup_local_folder.py`
  - `await CatalogService.match_all(state_db_path: str | Path | None = None) -> dict[str, int]` — keys `worker`, `backup_script`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_catalog_match.py`:

```python
"""Provenance: which local record, if any, claims each message in the channel."""
import sqlite3
from datetime import datetime

from sqlalchemy import select

from app.models.database import (
    AsyncSessionLocal,
    CatalogItem,
    CatalogSource,
    ChannelRole,
    MediaType,
    Photo,
    PhotoStatus,
)
from app.services.catalog import CatalogService


async def _item(**kwargs) -> None:
    defaults = {
        "channel_id": -100,
        "channel_role": ChannelRole.ARCHIVE,
        "media_kind": "document",
    }
    async with AsyncSessionLocal() as session:
        session.add(CatalogItem(**{**defaults, **kwargs}))
        await session.commit()


def _state_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE files (rel_path TEXT PRIMARY KEY, size INTEGER, sha256 TEXT, "
        "status TEXT, tg_message_id INTEGER)"
    )
    conn.executemany("INSERT INTO files VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


async def test_a_message_the_worker_uploaded_is_attributed_to_the_worker(clean_db):
    async with AsyncSessionLocal() as session:
        session.add(
            Photo(
                mega_path="/phone_bkp/a.jpg",
                status=PhotoStatus.COMPLETED,
                media_type=MediaType.IMAGE,
                tg_message_id=42,
                sha256="a" * 64,
            )
        )
        await session.commit()
    await _item(tg_message_id=42, file_name="a.jpg")

    assert await CatalogService(None, []).match_worker() == 1

    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.source is CatalogSource.WORKER
    assert item.photo_id is not None
    assert item.sha256 == "a" * 64


async def test_a_message_no_local_record_claims_stays_unknown(clean_db):
    """15,193 of 18,294 rows are in this state. It is the legacy, not a failure."""
    await _item(tg_message_id=999, file_name="mystery.jpg")

    assert await CatalogService(None, []).match_worker() == 0

    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.source is CatalogSource.UNKNOWN
    assert item.photo_id is None


async def test_the_backup_script_db_attributes_its_own_channel(clean_db, tmp_path):
    state = tmp_path / "iphone.db"
    _state_db(state, [("100APPLE/IMG_0014.JPG", 2_400_000, "b" * 64, "VERIFIED", 7)])
    await _item(channel_id=-200, tg_message_id=7, file_name="IMG_0014.JPG")

    assert await CatalogService(None, []).match_backup_db(state) == 1

    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.source is CatalogSource.BACKUP_SCRIPT
    assert item.backup_rel_path == "100APPLE/IMG_0014.JPG"
    assert item.sha256 == "b" * 64


async def test_matching_falls_back_to_sha256_when_message_ids_disagree(clean_db, tmp_path):
    """A re-upload changes the message id but not the bytes."""
    state = tmp_path / "iphone.db"
    _state_db(state, [("100APPLE/IMG_0015.JPG", 10, "c" * 64, "VERIFIED", 11)])
    await _item(channel_id=-200, tg_message_id=99, file_name="IMG_0015.JPG", sha256="c" * 64)

    assert await CatalogService(None, []).match_backup_db(state) == 1

    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.source is CatalogSource.BACKUP_SCRIPT
    assert item.backup_rel_path == "100APPLE/IMG_0015.JPG"


async def test_matching_is_idempotent(clean_db):
    async with AsyncSessionLocal() as session:
        session.add(
            Photo(
                mega_path="/phone_bkp/b.jpg",
                status=PhotoStatus.COMPLETED,
                media_type=MediaType.IMAGE,
                tg_message_id=5,
            )
        )
        await session.commit()
    await _item(tg_message_id=5, file_name="b.jpg")

    service = CatalogService(None, [])
    assert await service.match_worker() == 1
    assert await service.match_worker() == 0


async def test_a_missing_state_db_is_reported_not_raised(clean_db, tmp_path):
    assert await CatalogService(None, []).match_backup_db(tmp_path / "nope.db") == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_catalog_match.py -q
```

Expected: `AttributeError: 'CatalogService' object has no attribute 'match_worker'`.

- [ ] **Step 3: Implement matching**

Add to `app/services/catalog.py`. Extend the imports at the top:

```python
import sqlite3
from pathlib import Path

from app.models.database import CatalogSource, Photo
```

and add these methods to `CatalogService`:

```python
    async def match_worker(self) -> int:
        """Attribute rows to the MEGA worker by tg_message_id.

        photos.tg_message_id is unique per archive channel, so this is an exact
        join; there is no sha256 fallback because the worker never re-uploads a
        file under a new message without also updating its row.
        """
        matched = 0
        async with AsyncSessionLocal() as session:
            photos = {
                photo.tg_message_id: photo
                for photo in (
                    await session.scalars(select(Photo).where(Photo.tg_message_id.is_not(None)))
                ).all()
            }
            if not photos:
                return 0

            items = (
                await session.scalars(
                    select(CatalogItem).where(CatalogItem.source == CatalogSource.UNKNOWN)
                )
            ).all()

            for item in items:
                photo = photos.get(item.tg_message_id)
                if photo is None:
                    continue
                item.source = CatalogSource.WORKER
                item.photo_id = photo.id
                if item.sha256 is None:
                    item.sha256 = photo.sha256
                matched += 1

            if matched:
                await session.commit()
        return matched

    async def match_backup_db(self, state_db_path: str | Path) -> int:
        """Attribute rows to scripts/backup_local_folder.py.

        That script keeps its own stdlib sqlite3 state DB, never the app's, so
        this reads it directly and read-only. A missing file is a normal
        configuration state, not an error: report zero and move on.
        """
        path = Path(state_db_path)
        if not path.exists():
            logger.info("Catalog match: no backup state DB at %s, skipping.", path)
            return 0

        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT rel_path, sha256, tg_message_id FROM files WHERE tg_message_id IS NOT NULL"
            ).fetchall()
        finally:
            conn.close()

        by_message = {int(mid): (rel, sha) for rel, sha, mid in rows}
        by_sha = {sha: (rel, int(mid)) for rel, sha, mid in rows if sha}

        matched = 0
        async with AsyncSessionLocal() as session:
            items = (
                await session.scalars(
                    select(CatalogItem).where(CatalogItem.source == CatalogSource.UNKNOWN)
                )
            ).all()

            for item in items:
                hit = by_message.get(item.tg_message_id)
                if hit is not None:
                    rel_path, sha = hit
                elif item.sha256 and item.sha256 in by_sha:
                    rel_path, _ = by_sha[item.sha256]
                    sha = item.sha256
                else:
                    continue

                item.source = CatalogSource.BACKUP_SCRIPT
                item.backup_rel_path = rel_path
                if item.sha256 is None:
                    item.sha256 = sha
                matched += 1

            if matched:
                await session.commit()
        return matched

    async def match_all(self, state_db_path: str | Path | None = None) -> dict[str, int]:
        return {
            "worker": await self.match_worker(),
            "backup_script": (
                await self.match_backup_db(state_db_path) if state_db_path else 0
            ),
        }
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_catalog_match.py -q
```

Expected: 6 passed.

> **Note on the message-id match across channels.** `match_backup_db` matches on `tg_message_id` alone, and message ids restart per channel, so in principle an archive-channel message could collide with an iPhone-channel id. In practice the backup script's ids only exist in its own channel and the worker claims the archive channel first (`match_all` runs `match_worker` before `match_backup_db`, and both only consider rows still `UNKNOWN`). Task 5's report exposes any residual mis-attribution as a source/channel mismatch, which is the check that would catch it.

- [ ] **Step 5: Verify and commit**

```bash
.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check .
rtk git add app/services/catalog.py tests/test_catalog_match.py
rtk git commit -m "feat(catalog): attribute channel messages to the worker or the backup script"
```

---

### Task 4: EXIF enrichment from the head of the file

**Files:**
- Modify: `app/services/catalog.py`, `app/main.py`, `docker-compose.yml`, `AGENTS.md`, `docs/REFERENCE.md`
- Test: `tests/test_catalog_enrich.py`

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces:
  - `ImageMetadata` — frozen dataclass: `taken_at: datetime | None`, `gps_lat: float | None`, `gps_lon: float | None`, `width: int | None`, `height: int | None`, `camera_model: str | None`
  - `extract_metadata(blob: bytes) -> ImageMetadata` — pure, no I/O, tolerates truncation
  - `await CatalogService.enrich_batch(limit: int | None = None) -> dict[str, int]` — keys `attempted`, `with_date`, `with_gps`, `failed`, `skipped_native`

This is the task the spike validated: a 30-image sample read 96% of capture dates and 73% of GPS from the **first 1 MiB** of each file, 26.5 MB of traffic in total. `extract_metadata` is a pure function precisely so that fact is testable offline, forever, with no network.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_catalog_enrich.py`:

```python
"""EXIF read from the head of a file, which is where EXIF lives."""
import io
from datetime import datetime
from types import SimpleNamespace

import pytest
from PIL import Image
from PIL.ExifTags import GPS, Base
from PIL.TiffImagePlugin import IFDRational
from sqlalchemy import select

from app.models.database import AsyncSessionLocal, CatalogItem, ChannelRole
from app.services.catalog import CatalogService, ChannelSpec, extract_metadata


def _jpeg(*, date="2023:03:31 15:51:08", gps=True, model="Pixel 6", size=(64, 48)) -> bytes:
    image = Image.new("RGB", size, "red")
    exif = image.getexif()
    exif[Base.Orientation.value] = 1
    if model:
        exif[Base.Model.value] = model
    if date:
        exif.get_ifd(0x8769)[Base.DateTimeOriginal.value] = date
    if gps:
        block = exif.get_ifd(0x8825)
        block[GPS.GPSLatitudeRef.value] = "N"
        block[GPS.GPSLatitude.value] = (IFDRational(44), IFDRational(29), IFDRational(30))
        block[GPS.GPSLongitudeRef.value] = "W"
        block[GPS.GPSLongitude.value] = (IFDRational(11), IFDRational(20), IFDRational(0))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def test_date_gps_and_dimensions_are_read_from_a_whole_jpeg():
    meta = extract_metadata(_jpeg())

    assert meta.taken_at == datetime(2023, 3, 31, 15, 51, 8)
    assert meta.gps_lat == pytest.approx(44.491666, abs=1e-5)  # 44 + 29/60 + 30/3600
    assert meta.gps_lon == pytest.approx(-11.333333, abs=1e-5)  # W is negative
    assert meta.width == 64 and meta.height == 48
    assert meta.camera_model == "Pixel 6"


def test_the_same_metadata_is_read_from_a_truncated_head():
    """The whole enrichment budget rests on this: EXIF is at the front."""
    full = _jpeg(size=(800, 600))
    head = full[: len(full) // 4]
    assert len(head) < len(full)

    meta = extract_metadata(head)

    assert meta.taken_at == datetime(2023, 3, 31, 15, 51, 8)
    assert meta.gps_lat is not None
    assert meta.camera_model == "Pixel 6"


def test_a_photo_with_a_date_and_no_gps_yields_a_date_and_no_gps():
    meta = extract_metadata(_jpeg(gps=False))
    assert meta.taken_at == datetime(2023, 3, 31, 15, 51, 8)
    assert meta.gps_lat is None and meta.gps_lon is None


def test_a_photo_with_neither_yields_neither_without_raising():
    meta = extract_metadata(_jpeg(date=None, gps=False, model=None))
    assert meta.taken_at is None and meta.gps_lat is None and meta.camera_model is None


def test_unreadable_bytes_yield_an_empty_result_rather_than_an_exception():
    meta = extract_metadata(b"this is not an image")
    assert meta.taken_at is None and meta.width is None


class FakeStreamClient:
    """Duck-types get_messages + stream_media, which is all enrichment uses."""

    def __init__(self, content: dict[int, bytes]):
        self.content = content
        self.streamed: list[int] = []

    async def get_messages(self, chat_id, message_ids):
        return SimpleNamespace(id=message_ids, empty=False)

    async def stream_media(self, message, limit=1):
        self.streamed.append(message.id)
        blob = self.content.get(message.id)
        if blob is None:
            raise RuntimeError("no such media")
        yield blob


async def _item(**kwargs):
    defaults = {
        "channel_id": -100,
        "channel_role": ChannelRole.ARCHIVE,
        "media_kind": "document",
        "file_name": "a.jpg",
    }
    async with AsyncSessionLocal() as session:
        session.add(CatalogItem(**{**defaults, **kwargs}))
        await session.commit()


def _service(content):
    return CatalogService(
        FakeStreamClient(content),
        [ChannelSpec(-100, ChannelRole.ARCHIVE)],
        enrich_delay_seconds=0,
    )


async def test_enrichment_stores_date_and_gps_on_the_row(clean_db):
    await _item(tg_message_id=1)

    result = await _service({1: _jpeg()}).enrich_batch()

    assert result["attempted"] == 1 and result["with_date"] == 1 and result["with_gps"] == 1
    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.taken_at == datetime(2023, 3, 31, 15, 51, 8)
    assert item.gps_lat is not None
    assert item.enriched_at is not None
    assert item.enrich_error is None


async def test_an_enriched_row_is_never_fetched_again(clean_db):
    await _item(tg_message_id=1)
    service = _service({1: _jpeg()})

    await service.enrich_batch()
    second = await service.enrich_batch()

    assert second["attempted"] == 0
    assert service.client.streamed == [1]


async def test_a_permanently_unreadable_item_costs_exactly_one_fetch(clean_db):
    await _item(tg_message_id=1)
    service = _service({})  # stream_media raises

    first = await service.enrich_batch()
    second = await service.enrich_batch()

    assert first["failed"] == 1
    assert second["attempted"] == 0
    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.enriched_at is not None
    assert item.enrich_error


async def test_a_native_telegram_photo_is_short_circuited_without_a_fetch(clean_db):
    """Telegram re-encodes native photos: the format guarantees there is no EXIF."""
    await _item(tg_message_id=1, media_kind="photo", file_name=None)
    service = _service({1: _jpeg()})

    result = await service.enrich_batch()

    assert result["skipped_native"] == 1 and result["attempted"] == 0
    assert service.client.streamed == []
    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.enriched_at is not None
    assert item.enrich_error == "telegram-native, no exif"


async def test_videos_chunks_manifests_and_mirror_rows_are_not_enrichment_targets(clean_db):
    await _item(tg_message_id=1, media_kind="video", file_name="a.mp4")
    await _item(tg_message_id=2, file_name="big.mp4.part001-of-003", artifact="chunk")
    await _item(tg_message_id=3, file_name="big.mp4.manifest.json", artifact="manifest")
    await _item(tg_message_id=4, channel_id=-200, channel_role=ChannelRole.MIRROR)
    service = _service({})

    result = await service.enrich_batch()

    assert result["attempted"] == 0
    assert service.client.streamed == []


async def test_the_batch_limit_is_respected(clean_db):
    for mid in range(1, 6):
        await _item(tg_message_id=mid, file_name=f"{mid}.jpg")

    result = await _service({m: _jpeg() for m in range(1, 6)}).enrich_batch(limit=2)

    assert result["attempted"] == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_catalog_enrich.py -q
```

Expected: `ImportError: cannot import name 'extract_metadata' from 'app.services.catalog'`.

- [ ] **Step 3: Implement the pure metadata reader**

Add to `app/services/catalog.py`. Extend the imports:

```python
import io
from datetime import datetime, timezone

from PIL import Image
from PIL.ExifTags import GPS, Base
from pillow_heif import register_heif_opener

register_heif_opener()
```

and add at module level:

```python
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".dng", ".webp"}

DEFAULT_ENRICH_BATCH_SIZE = 200
DEFAULT_ENRICH_HEAD_BYTES = 1024 * 1024  # EXIF lives at the front; 1 MiB is plenty
NATIVE_PHOTO_REASON = "telegram-native, no exif"


@dataclass(frozen=True)
class ImageMetadata:
    taken_at: datetime | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None
    width: int | None = None
    height: int | None = None
    camera_model: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.taken_at is None and self.gps_lat is None and self.width is None


def _exif_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


def _dms_to_degrees(value: object, ref: object) -> float | None:
    """EXIF stores coordinates as degrees/minutes/seconds plus a hemisphere."""
    try:
        degrees, minutes, seconds = (float(part) for part in value)  # type: ignore[misc]
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    decimal = degrees + minutes / 60 + seconds / 3600
    if isinstance(ref, str) and ref.strip().upper() in ("S", "W"):
        decimal = -decimal
    if not -180.0 <= decimal <= 180.0:
        return None
    return decimal


def extract_metadata(blob: bytes) -> ImageMetadata:
    """Read EXIF out of (possibly truncated) image bytes.

    Never raises. A truncated file is the normal case here — enrichment fetches
    only the head — and Pillow reads the header lazily, so metadata survives
    even though the pixel data does not.
    """
    try:
        with Image.open(io.BytesIO(blob)) as image:
            width, height = image.size
            exif = image.getexif()
            taken = _exif_datetime(
                exif.get_ifd(0x8769).get(Base.DateTimeOriginal.value)
            ) or _exif_datetime(exif.get(Base.DateTime.value))

            gps_block = dict(exif.get_ifd(0x8825))
            lat = _dms_to_degrees(
                gps_block.get(GPS.GPSLatitude.value), gps_block.get(GPS.GPSLatitudeRef.value)
            )
            lon = _dms_to_degrees(
                gps_block.get(GPS.GPSLongitude.value), gps_block.get(GPS.GPSLongitudeRef.value)
            )
            if lat is None or lon is None:
                lat = lon = None

            model = exif.get(Base.Model.value)
            return ImageMetadata(
                taken_at=taken,
                gps_lat=lat,
                gps_lon=lon,
                width=width,
                height=height,
                camera_model=str(model).strip()[:128] if model else None,
            )
    except Exception:
        return ImageMetadata()
```

- [ ] **Step 4: Implement the enrichment loop**

Add the two new constructor keywords to `CatalogService.__init__` (after `scan_delay_seconds`):

```python
        enrich_batch_size: int = DEFAULT_ENRICH_BATCH_SIZE,
        enrich_head_bytes: int = DEFAULT_ENRICH_HEAD_BYTES,
        enrich_delay_seconds: float = 0.5,
```

storing them as `self.enrich_batch_size`, `self.enrich_head_bytes`, `self.enrich_delay_seconds`. Then add:

```python
    def _is_image(self, item: CatalogItem) -> bool:
        if item.file_name:
            return Path(item.file_name).suffix.lower() in IMAGE_SUFFIXES
        return item.media_kind == "photo"

    async def enrich_batch(self, limit: int | None = None) -> dict[str, int]:
        """Read capture date and GPS for one bounded batch of images.

        Fetches only the first `enrich_head_bytes` of each file and discards
        them: nothing is written to disk. Failures set `enriched_at` alongside
        `enrich_error` so a permanently unreadable file costs one fetch in total
        rather than one per run.
        """
        limit = limit or self.enrich_batch_size
        counters = {
            "attempted": 0,
            "with_date": 0,
            "with_gps": 0,
            "failed": 0,
            "skipped_native": 0,
        }

        async with AsyncSessionLocal() as session:
            candidates = (
                await session.scalars(
                    select(CatalogItem)
                    .where(
                        CatalogItem.enriched_at.is_(None),
                        CatalogItem.channel_role == ChannelRole.ARCHIVE,
                        CatalogItem.artifact.is_(None),
                    )
                    .order_by(CatalogItem.id)
                    .limit(limit * 4)
                )
            ).all()
            targets = [item for item in candidates if self._is_image(item)][:limit]
            ids = [item.id for item in targets]

        now = datetime.now(timezone.utc)

        for item_id in ids:
            async with AsyncSessionLocal() as session:
                item = await session.get(CatalogItem, item_id)
                if item is None or item.enriched_at is not None:
                    continue

                if item.media_kind == "photo":
                    item.enriched_at = now
                    item.enrich_error = NATIVE_PHOTO_REASON
                    await session.commit()
                    counters["skipped_native"] += 1
                    continue

                try:
                    blob = await self._head_bytes(item)
                except Exception as exc:
                    item.enriched_at = now
                    item.enrich_error = f"{type(exc).__name__}: {exc}"[:255]
                    await session.commit()
                    counters["attempted"] += 1
                    counters["failed"] += 1
                    continue

                meta = extract_metadata(blob)
                item.enriched_at = now
                item.taken_at = meta.taken_at
                item.gps_lat = meta.gps_lat
                item.gps_lon = meta.gps_lon
                item.width = meta.width
                item.height = meta.height
                item.camera_model = meta.camera_model
                item.enrich_error = "no exif in head" if meta.is_empty else None
                await session.commit()

                counters["attempted"] += 1
                counters["with_date"] += meta.taken_at is not None
                counters["with_gps"] += meta.gps_lat is not None

            if self.enrich_delay_seconds > 0:
                await asyncio.sleep(self.enrich_delay_seconds)

        logger.info("Catalog enrich batch: %s", counters)
        return counters

    async def _head_bytes(self, item: CatalogItem) -> bytes:
        message = await self.client.get_messages(item.channel_id, message_ids=item.tg_message_id)
        if message is None or getattr(message, "empty", False):
            raise RuntimeError("message gone from channel")

        collected = bytearray()
        async for chunk in self.client.stream_media(message, limit=1):
            collected += chunk
            if len(collected) >= self.enrich_head_bytes:
                break
        return bytes(collected[: self.enrich_head_bytes])
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_catalog_enrich.py -q
```

Expected: 12 passed.

- [ ] **Step 6: Wire the knobs**

In `app/main.py`, extend the `CatalogService(...)` construction from Task 2:

```python
            enrich_batch_size=int(os.getenv("CATALOG_ENRICH_BATCH_SIZE", "200")),
            enrich_head_bytes=int(os.getenv("CATALOG_ENRICH_HEAD_BYTES", str(1024 * 1024))),
            enrich_delay_seconds=float(os.getenv("CATALOG_ENRICH_DELAY", "0.5")),
```

Add to `docker-compose.yml`:

```yaml
      - CATALOG_ENRICH_BATCH_SIZE=${CATALOG_ENRICH_BATCH_SIZE:-200}
      - CATALOG_ENRICH_HEAD_BYTES=${CATALOG_ENRICH_HEAD_BYTES:-1048576}
      - CATALOG_ENRICH_DELAY=${CATALOG_ENRICH_DELAY:-0.5}
```

Add rows to the env tables in `AGENTS.md` and `docs/REFERENCE.md`:

| variable | default | meaning |
|---|---|---|
| `CATALOG_ENRICH_BATCH_SIZE` | `200` | Images enriched per `POST /api/catalog/enrich` call. |
| `CATALOG_ENRICH_HEAD_BYTES` | `1048576` | Bytes fetched per image. EXIF sits at the front of the file; a 30-image sample read 96% of dates and 73% of GPS from the first 1 MiB. |
| `CATALOG_ENRICH_DELAY` | `0.5` | Seconds between fetches. Pacing, not tuning — see the plan's global constraints. |

- [ ] **Step 7: Verify and commit**

```bash
.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check . && python3 -m compileall -q app scripts
rtk git add app/services/catalog.py app/main.py tests/test_catalog_enrich.py docker-compose.yml AGENTS.md docs/REFERENCE.md
rtk git commit -m "feat(catalog): read capture date and GPS from the head of each image"
```

---

### Task 5: the reconciliation report and its API

**Files:**
- Modify: `app/services/catalog.py`, `app/api/routes.py`
- Test: `tests/test_catalog_report.py`

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces:
  - `await CatalogService.build_report(state_db_path: str | Path | None = None) -> dict[str, object]`
  - `POST /api/catalog/scan`, `POST /api/catalog/enrich`, `GET /api/catalog/report`, `GET /api/catalog/items`

The report's reason for existing is the `missing_from_channel` counter: it is the only query in the system that can say something has been lost, and no current code path can answer it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_catalog_report.py`:

```python
"""The reconciliation report: what exists, what is duplicated, what is gone."""
from datetime import datetime

from app.models.database import (
    AsyncSessionLocal,
    CatalogItem,
    CatalogSource,
    ChannelRole,
    MediaType,
    Photo,
    PhotoStatus,
)
from app.services.catalog import CatalogService


async def _item(**kwargs):
    defaults = {
        "channel_id": -100,
        "channel_role": ChannelRole.ARCHIVE,
        "media_kind": "document",
        "file_name": "a.jpg",
    }
    async with AsyncSessionLocal() as session:
        session.add(CatalogItem(**{**defaults, **kwargs}))
        await session.commit()


async def test_totals_split_media_from_vault_artifacts(clean_db):
    await _item(tg_message_id=1, file_name="a.jpg")
    await _item(tg_message_id=2, file_name="big.mp4.part001-of-002", artifact="chunk")
    await _item(tg_message_id=3, file_name="big.mp4.manifest.json", artifact="manifest")

    report = await CatalogService(None, []).build_report()

    assert report["totals"]["media"] == 1
    assert report["totals"]["artifacts"] == 2


async def test_provenance_counts_unknown_separately(clean_db):
    await _item(tg_message_id=1, source=CatalogSource.WORKER)
    await _item(tg_message_id=2)
    await _item(tg_message_id=3)

    report = await CatalogService(None, []).build_report()

    assert report["provenance"]["WORKER"] == 1
    assert report["provenance"]["UNKNOWN"] == 2


async def test_a_photo_row_whose_message_is_not_in_the_channel_is_reported_missing(clean_db):
    """The counter this whole component exists for."""
    async with AsyncSessionLocal() as session:
        session.add_all(
            [
                Photo(
                    mega_path="/phone_bkp/present.jpg",
                    status=PhotoStatus.COMPLETED,
                    media_type=MediaType.IMAGE,
                    tg_message_id=1,
                ),
                Photo(
                    mega_path="/phone_bkp/vanished.jpg",
                    status=PhotoStatus.COMPLETED,
                    media_type=MediaType.IMAGE,
                    tg_message_id=2,
                ),
            ]
        )
        await session.commit()
    await _item(tg_message_id=1)

    report = await CatalogService(None, []).build_report()

    assert report["missing_from_channel"]["count"] == 1
    assert report["missing_from_channel"]["sample"] == ["/phone_bkp/vanished.jpg"]


async def test_duplicates_are_counted_by_hash_and_separately_by_name(clean_db):
    """Name duplicates are suggestive; only a hash match is evidence."""
    await _item(tg_message_id=1, file_name="dup.mp4", sha256="a" * 64, file_size=100)
    await _item(tg_message_id=2, file_name="dup.mp4", sha256="a" * 64, file_size=100)
    await _item(tg_message_id=3, file_name="same-name.mp4", sha256=None, file_size=50)
    await _item(tg_message_id=4, file_name="same-name.mp4", sha256=None, file_size=50)

    report = await CatalogService(None, []).build_report()

    assert report["duplicates"]["by_hash"]["excess_messages"] == 1
    assert report["duplicates"]["by_hash"]["excess_bytes"] == 100
    assert report["duplicates"]["by_name_unconfirmed"]["excess_messages"] == 1


async def test_metadata_coverage_counts_dates_gps_and_the_message_date_fallback(clean_db):
    await _item(
        tg_message_id=1,
        taken_at=datetime(2023, 1, 1),
        gps_lat=44.0,
        gps_lon=11.0,
        enriched_at=datetime(2026, 9, 21),
    )
    await _item(tg_message_id=2, taken_at=datetime(2023, 1, 2), enriched_at=datetime(2026, 9, 21))
    await _item(
        tg_message_id=3,
        enriched_at=datetime(2026, 9, 21),
        enrich_error="telegram-native, no exif",
        media_kind="photo",
        file_name=None,
    )
    await _item(tg_message_id=4)

    report = await CatalogService(None, []).build_report()
    coverage = report["metadata"]

    assert coverage["with_taken_at"] == 2
    assert coverage["with_gps"] == 1
    assert coverage["not_enriched"] == 1
    assert coverage["dated_by_message_only"] == 2  # rows 3 and 4 have no taken_at


async def test_an_empty_catalog_reports_zeroes_rather_than_dividing_by_zero(clean_db):
    report = await CatalogService(None, []).build_report()
    assert report["totals"]["media"] == 0
    assert report["duplicates"]["by_hash"]["excess_messages"] == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_catalog_report.py -q
```

Expected: `AttributeError: 'CatalogService' object has no attribute 'build_report'`.

- [ ] **Step 3: Implement the report**

Add to `app/services/catalog.py`. Extend the imports with `from sqlalchemy import func, select` (replacing the bare `select` import) and add the method:

```python
    REPORT_SAMPLE = 20

    async def build_report(self, state_db_path: str | Path | None = None) -> dict[str, object]:
        """Everything the three separate databases could never say together."""
        async with AsyncSessionLocal() as session:
            media = select(CatalogItem).where(CatalogItem.artifact.is_(None))

            totals = {
                "media": int(
                    (
                        await session.execute(
                            select(func.count(CatalogItem.id)).where(CatalogItem.artifact.is_(None))
                        )
                    ).scalar_one()
                ),
                "artifacts": int(
                    (
                        await session.execute(
                            select(func.count(CatalogItem.id)).where(
                                CatalogItem.artifact.is_not(None)
                            )
                        )
                    ).scalar_one()
                ),
            }

            by_channel = {
                str(channel): {"count": int(count), "bytes": int(size or 0)}
                for channel, count, size in (
                    await session.execute(
                        select(
                            CatalogItem.channel_id,
                            func.count(CatalogItem.id),
                            func.sum(CatalogItem.file_size),
                        )
                        .where(CatalogItem.artifact.is_(None))
                        .group_by(CatalogItem.channel_id)
                    )
                ).all()
            }

            by_kind = {
                kind: int(count)
                for kind, count in (
                    await session.execute(
                        select(CatalogItem.media_kind, func.count(CatalogItem.id))
                        .where(CatalogItem.artifact.is_(None))
                        .group_by(CatalogItem.media_kind)
                    )
                ).all()
            }

            provenance = {
                source.value: int(count)
                for source, count in (
                    await session.execute(
                        select(CatalogItem.source, func.count(CatalogItem.id))
                        .where(CatalogItem.artifact.is_(None))
                        .group_by(CatalogItem.source)
                    )
                ).all()
            }

            # Anti-join in Python rather than SQL: the id set is ~25k integers,
            # which is trivial in memory and avoids a large IN () clause.
            catalogued_ids = {
                mid for (mid,) in (await session.execute(select(CatalogItem.tg_message_id))).all()
            }
            orphan_photos = [
                (photo.id, photo.mega_path)
                for photo in (
                    await session.scalars(
                        select(Photo).where(Photo.tg_message_id.is_not(None))
                    )
                ).all()
                if photo.tg_message_id not in catalogued_ids
            ]

            dup_hash = (
                await session.execute(
                    select(
                        func.count(CatalogItem.id) - func.count(func.distinct(CatalogItem.sha256)),
                    ).where(CatalogItem.sha256.is_not(None))
                )
            ).scalar_one()
            dup_hash_bytes = 0
            for _sha, count, size in (
                await session.execute(
                    select(
                        CatalogItem.sha256,
                        func.count(CatalogItem.id),
                        func.max(CatalogItem.file_size),
                    )
                    .where(CatalogItem.sha256.is_not(None))
                    .group_by(CatalogItem.sha256)
                    .having(func.count(CatalogItem.id) > 1)
                )
            ).all():
                dup_hash_bytes += int(size or 0) * (int(count) - 1)

            dup_name = 0
            dup_name_bytes = 0
            for _name, count, size in (
                await session.execute(
                    select(
                        CatalogItem.file_name,
                        func.count(CatalogItem.id),
                        func.max(CatalogItem.file_size),
                    )
                    .where(CatalogItem.file_name.is_not(None), CatalogItem.sha256.is_(None))
                    .group_by(CatalogItem.file_name)
                    .having(func.count(CatalogItem.id) > 1)
                )
            ).all():
                dup_name += int(count) - 1
                dup_name_bytes += int(size or 0) * (int(count) - 1)

            enriched_media = media.where(CatalogItem.enriched_at.is_not(None))

            async def _count(stmt) -> int:
                return int(
                    (
                        await session.execute(
                            select(func.count()).select_from(stmt.subquery())
                        )
                    ).scalar_one()
                )

            metadata = {
                "with_taken_at": await _count(media.where(CatalogItem.taken_at.is_not(None))),
                "with_gps": await _count(media.where(CatalogItem.gps_lat.is_not(None))),
                "enriched": await _count(enriched_media),
                "not_enriched": await _count(media.where(CatalogItem.enriched_at.is_(None))),
                "dated_by_message_only": await _count(media.where(CatalogItem.taken_at.is_(None))),
                "native_no_exif": await _count(
                    media.where(CatalogItem.enrich_error == NATIVE_PHOTO_REASON)
                ),
            }

            exported = await _count(media.where(CatalogItem.exported_path.is_not(None)))

        return {
            "totals": totals,
            "by_channel": by_channel,
            "by_kind": by_kind,
            "provenance": provenance,
            "missing_from_channel": {
                "count": len(orphan_photos),
                "sample": [path for _id, path in orphan_photos[: self.REPORT_SAMPLE]],
            },
            "duplicates": {
                "by_hash": {
                    "excess_messages": int(dup_hash or 0),
                    "excess_bytes": dup_hash_bytes,
                },
                "by_name_unconfirmed": {
                    "excess_messages": dup_name,
                    "excess_bytes": dup_name_bytes,
                },
            },
            "metadata": metadata,
            "exported": exported,
        }
```

`REPORT_SAMPLE` is a class attribute; put it immediately below the `class CatalogService:` docstring, above `__init__`.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_catalog_report.py -q
```

Expected: 6 passed.

- [ ] **Step 5: Add the API surface**

In `app/api/routes.py`, add near the other `_require_*` helpers:

```python
def _require_catalog(request: Request):
    catalog = getattr(request.app.state, "catalog", None)
    if catalog is None:
        raise HTTPException(status_code=503, detail="Catalog service unavailable")
    return catalog
```

and the routes at the end of the file:

```python
@router.post("/catalog/scan")
async def catalog_scan(request: Request) -> dict[str, object]:
    catalog = _require_catalog(request)
    results = await catalog.scan_all()
    matched = await catalog.match_all(os.getenv("BACKUP_STATE_DB") or None)
    return {"scanned": results, "matched": matched}


@router.post("/catalog/enrich")
async def catalog_enrich(
    request: Request, limit: int | None = Query(default=None, ge=1, le=2000)
) -> dict[str, int]:
    return await _require_catalog(request).enrich_batch(limit=limit)


@router.get("/catalog/report")
async def catalog_report(request: Request) -> dict[str, object]:
    return await _require_catalog(request).build_report()


@router.get("/catalog/items")
async def catalog_items(
    channel_id: int | None = Query(default=None),
    source: str | None = Query(default=None),
    has_gps: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    query = select(CatalogItem)
    count_query = select(func.count(CatalogItem.id))

    for clause in (
        CatalogItem.channel_id == channel_id if channel_id is not None else None,
        CatalogItem.source == CatalogSource(source) if source is not None else None,
        (CatalogItem.gps_lat.is_not(None) if has_gps else CatalogItem.gps_lat.is_(None))
        if has_gps is not None
        else None,
    ):
        if clause is not None:
            query = query.where(clause)
            count_query = count_query.where(clause)

    async with AsyncSessionLocal() as session:
        total = (await session.execute(count_query)).scalar_one()
        rows = await session.scalars(
            query.order_by(CatalogItem.id.desc()).limit(limit).offset(offset)
        )
        items = [
            {
                "id": item.id,
                "channel_id": item.channel_id,
                "tg_message_id": item.tg_message_id,
                "media_kind": item.media_kind,
                "artifact": item.artifact,
                "file_name": item.file_name,
                "file_size": item.file_size,
                "taken_at": item.taken_at.isoformat() if item.taken_at else None,
                "gps": [item.gps_lat, item.gps_lon] if item.gps_lat is not None else None,
                "source": item.source.value,
                "exported_path": item.exported_path,
                "enrich_error": item.enrich_error,
            }
            for item in rows
        ]

    return {"total": int(total), "limit": limit, "offset": offset, "items": items}
```

Extend the existing model import in that file to include `CatalogItem` and `CatalogSource`. An invalid `source` value raises `ValueError` from the enum — wrap it the way `recovery_items` wraps `RecoveryStatus`:

```python
    if source is not None:
        try:
            CatalogSource(source)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown source: {source}")
```

placed above the filter loop.

- [ ] **Step 6: Verify and commit**

```bash
.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check . && python3 -m compileall -q app scripts
rtk git add app/services/catalog.py app/api/routes.py tests/test_catalog_report.py
rtk git commit -m "feat(catalog): reconciliation report over every channel and local record"
```

---

### Task 6: gallery export

**Files:**
- Create: `scripts/export_gallery.py`
- Modify: `app/services/catalog.py`, `app/main.py`, `docker-compose.yml`, `AGENTS.md`, `docs/REFERENCE.md`
- Test: `tests/test_gallery_export.py`

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces:
  - `gallery_relpath(taken_at: datetime | None, message_date: datetime | None, file_name: str | None, tg_message_id: int) -> Path`
  - `resolve_collision(root: Path, relpath: Path, expected_size: int | None) -> tuple[Path, bool]` — returns the path to write and whether an acceptable file is already there
  - `scripts/export_gallery.py` CLI: `--root`, `--limit`, `--apply`, `--include-native`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gallery_export.py`:

```python
"""The read-only tree Immich indexes as an external library."""
from datetime import datetime
from pathlib import Path

from app.services.catalog import gallery_relpath, resolve_collision


def test_the_tree_is_built_from_the_capture_date():
    path = gallery_relpath(
        taken_at=datetime(2023, 3, 31, 15, 51, 8),
        message_date=datetime(2026, 7, 7),
        file_name="PXL_20230331_135108850.jpg",
        tg_message_id=17,
    )
    assert path == Path("2023/2023-03-31/PXL_20230331_135108850.jpg")


def test_without_a_capture_date_it_falls_back_to_the_post_date():
    """A migrated archive posts 2016 photos in 2026; the report counts these."""
    path = gallery_relpath(
        taken_at=None,
        message_date=datetime(2026, 7, 7, 6, 48),
        file_name="mystery.jpg",
        tg_message_id=17,
    )
    assert path == Path("2026/2026-07-07/mystery.jpg")


def test_without_any_date_it_lands_in_an_undated_bucket():
    path = gallery_relpath(None, None, "x.jpg", 17)
    assert path == Path("undated/x.jpg")


def test_a_missing_filename_is_replaced_by_the_message_id():
    path = gallery_relpath(datetime(2023, 3, 31), None, None, 17)
    assert path == Path("2023/2023-03-31/00000017.jpg")


def test_path_separators_in_a_filename_cannot_escape_the_tree():
    path = gallery_relpath(datetime(2023, 3, 31), None, "../../etc/passwd", 17)
    assert path == Path("2023/2023-03-31/passwd")


def test_an_already_present_file_of_the_right_size_is_adopted_without_rewriting(tmp_path):
    target = tmp_path / "2023/2023-03-31/a.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x" * 100)

    path, already_there = resolve_collision(tmp_path, Path("2023/2023-03-31/a.jpg"), 100)

    assert already_there is True
    assert path == target


def test_a_different_file_at_the_same_path_gets_a_suffix(tmp_path):
    target = tmp_path / "2023/2023-03-31/a.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x" * 50)

    path, already_there = resolve_collision(tmp_path, Path("2023/2023-03-31/a.jpg"), 100)

    assert already_there is False
    assert path == tmp_path / "2023/2023-03-31/a (2).jpg"


def test_an_absent_file_is_reported_as_absent(tmp_path):
    path, already_there = resolve_collision(tmp_path, Path("2023/2023-03-31/a.jpg"), 100)
    assert already_there is False
    assert path == tmp_path / "2023/2023-03-31/a.jpg"
```

Then the exporter's own behaviour, in the same file:

```python
import sqlite3  # noqa: E402  (grouped with the script-level imports below)

from sqlalchemy import select  # noqa: E402

from app.models.database import (  # noqa: E402
    AsyncSessionLocal,
    CatalogItem,
    ChannelRole,
)
from scripts.export_gallery import export_batch  # noqa: E402


class FakeDownloadClient:
    def __init__(self, content: dict[int, bytes]):
        self.content = content
        self.downloaded: list[int] = []

    async def get_messages(self, chat_id, message_ids):
        from types import SimpleNamespace

        return SimpleNamespace(id=message_ids, empty=False)

    async def download_media(self, message, file_name):
        self.downloaded.append(message.id)
        Path(file_name).parent.mkdir(parents=True, exist_ok=True)
        Path(file_name).write_bytes(self.content[message.id])
        return file_name


async def _item(**kwargs):
    defaults = {
        "channel_id": -100,
        "channel_role": ChannelRole.ARCHIVE,
        "media_kind": "document",
        "file_name": "a.jpg",
        "file_size": 4,
        "taken_at": datetime(2023, 3, 31, 15, 51, 8),
        "enriched_at": datetime(2026, 9, 21),
    }
    async with AsyncSessionLocal() as session:
        session.add(CatalogItem(**{**defaults, **kwargs}))
        await session.commit()


async def test_a_dry_run_writes_nothing_and_downloads_nothing(clean_db, tmp_path):
    await _item(tg_message_id=1)
    client = FakeDownloadClient({1: b"data"})

    result = await export_batch(client, tmp_path, limit=10, apply=False)

    assert result["would_export"] == 1 and result["exported"] == 0
    assert client.downloaded == []
    assert list(tmp_path.rglob("*")) == []
    async with AsyncSessionLocal() as session:
        assert (await session.scalar(select(CatalogItem))).exported_path is None


async def test_applying_writes_the_file_and_records_the_path(clean_db, tmp_path):
    await _item(tg_message_id=1)
    client = FakeDownloadClient({1: b"data"})

    result = await export_batch(client, tmp_path, limit=10, apply=True)

    assert result["exported"] == 1
    written = tmp_path / "2023/2023-03-31/a.jpg"
    assert written.read_bytes() == b"data"
    async with AsyncSessionLocal() as session:
        assert (await session.scalar(select(CatalogItem))).exported_path == "2023/2023-03-31/a.jpg"


async def test_a_second_run_exports_nothing_and_re_downloads_nothing(clean_db, tmp_path):
    await _item(tg_message_id=1)
    client = FakeDownloadClient({1: b"data"})

    await export_batch(client, tmp_path, limit=10, apply=True)
    second = await export_batch(client, tmp_path, limit=10, apply=True)

    assert second["exported"] == 0
    assert client.downloaded == [1]


async def test_a_file_the_worker_already_placed_is_adopted_without_downloading(clean_db, tmp_path):
    """The forward path writes the file; the backfill must not fetch it again."""
    await _item(tg_message_id=1)
    target = tmp_path / "2023/2023-03-31/a.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"data")
    client = FakeDownloadClient({1: b"data"})

    result = await export_batch(client, tmp_path, limit=10, apply=True)

    assert result["adopted"] == 1 and result["exported"] == 0
    assert client.downloaded == []
    async with AsyncSessionLocal() as session:
        assert (await session.scalar(select(CatalogItem))).exported_path == "2023/2023-03-31/a.jpg"


async def test_videos_artifacts_mirror_rows_and_native_photos_are_not_exported(clean_db, tmp_path):
    await _item(tg_message_id=1, media_kind="video", file_name="a.mp4")
    await _item(tg_message_id=2, file_name="b.mp4.part001-of-002", artifact="chunk")
    await _item(tg_message_id=3, channel_id=-200, channel_role=ChannelRole.MIRROR)
    await _item(tg_message_id=4, media_kind="photo", file_name=None)
    client = FakeDownloadClient({})

    result = await export_batch(client, tmp_path, limit=10, apply=True)

    assert result["exported"] == 0
    assert client.downloaded == []


async def test_native_photos_are_exported_only_when_explicitly_asked_for(clean_db, tmp_path):
    await _item(tg_message_id=4, media_kind="photo", file_name=None, file_size=4)
    client = FakeDownloadClient({4: b"data"})

    result = await export_batch(client, tmp_path, limit=10, apply=True, include_native=True)

    assert result["exported"] == 1
    assert (tmp_path / "2023/2023-03-31/00000004.jpg").read_bytes() == b"data"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_gallery_export.py -q
```

Expected: `ImportError: cannot import name 'gallery_relpath' from 'app.services.catalog'`.

- [ ] **Step 3: Add the path helpers to the service**

Add to `app/services/catalog.py` at module level:

```python
GALLERY_UNDATED = "undated"


def gallery_relpath(
    taken_at: datetime | None,
    message_date: datetime | None,
    file_name: str | None,
    tg_message_id: int,
) -> Path:
    """Where an item lives inside the gallery tree.

    Immich does not need the tree — it reads EXIF — but a date-shaped layout
    makes the export idempotent, resumable and browsable by hand, and it makes a
    wrong date visible as a wrong folder instead of an invisible database value.
    """
    name = Path(file_name).name.strip() if file_name else ""
    if not name or name in (".", ".."):
        name = f"{tg_message_id:08d}.jpg"

    stamp = taken_at or message_date
    if stamp is None:
        return Path(GALLERY_UNDATED) / name
    return Path(f"{stamp:%Y}") / f"{stamp:%Y-%m-%d}" / name


def resolve_collision(
    root: Path, relpath: Path, expected_size: int | None
) -> tuple[Path, bool]:
    """(path to write, an acceptable file is already there).

    A file of the expected size is treated as ours — that is how the worker's
    forward-path copies get adopted by the backfill instead of re-downloaded.
    """
    target = root / relpath
    if not target.exists():
        return target, False
    if expected_size is None or target.stat().st_size == expected_size:
        return target, True

    for index in range(2, 100):
        candidate = target.with_name(f"{target.stem} ({index}){target.suffix}")
        if not candidate.exists():
            return candidate, False
        if expected_size is not None and candidate.stat().st_size == expected_size:
            return candidate, True
    raise RuntimeError(f"too many colliding names for {relpath}")
```

- [ ] **Step 4: Write the exporter script**

Create `scripts/export_gallery.py`:

```python
"""Backfill the gallery tree Immich indexes as a read-only external library.

Standalone and resumable in the style of scripts/backup_local_folder.py: state
is catalog_items.exported_path, dry-run is the default, and it never deletes
anything. Downloads originals in full — unlike enrichment, Immich needs the
whole file for thumbnails and machine learning.

    python -m scripts.export_gallery --limit 50            # dry run
    python -m scripts.export_gallery --limit 50 --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.models.database import AsyncSessionLocal, CatalogItem, ChannelRole, init_db
from app.services.catalog import IMAGE_SUFFIXES, gallery_relpath, resolve_collision

DEFAULT_DELAY_SECONDS = 1.0


def _is_exportable(item: CatalogItem, include_native: bool) -> bool:
    if item.channel_role is not ChannelRole.ARCHIVE or item.artifact is not None:
        return False
    if item.media_kind == "photo":
        return include_native
    if item.media_kind != "document":
        return False
    return bool(item.file_name) and Path(item.file_name).suffix.lower() in IMAGE_SUFFIXES


async def export_batch(
    client,
    root: Path,
    *,
    limit: int,
    apply: bool,
    include_native: bool = False,
    delay_seconds: float = 0.0,
) -> dict[str, int]:
    root = Path(root)
    counters = {"considered": 0, "would_export": 0, "exported": 0, "adopted": 0, "failed": 0}

    async with AsyncSessionLocal() as session:
        candidates = (
            await session.scalars(
                select(CatalogItem)
                .where(
                    CatalogItem.exported_path.is_(None),
                    CatalogItem.channel_role == ChannelRole.ARCHIVE,
                    CatalogItem.artifact.is_(None),
                )
                .order_by(CatalogItem.taken_at.desc().nullslast(), CatalogItem.id)
                .limit(limit * 4)
            )
        ).all()
        targets = [i for i in candidates if _is_exportable(i, include_native)][:limit]
        plan = [
            (
                item.id,
                item.channel_id,
                item.tg_message_id,
                item.file_size,
                gallery_relpath(
                    item.taken_at, item.message_date, item.file_name, item.tg_message_id
                ),
            )
            for item in targets
        ]

    for item_id, channel_id, message_id, size, relpath in plan:
        counters["considered"] += 1
        target, already_there = resolve_collision(root, relpath, size)

        if already_there:
            if apply:
                await _record(item_id, target.relative_to(root))
            counters["adopted"] += 1
            continue

        if not apply:
            counters["would_export"] += 1
            print(f"  would export msg {message_id} -> {relpath}")
            continue

        try:
            message = await client.get_messages(channel_id, message_ids=message_id)
            if message is None or getattr(message, "empty", False):
                raise RuntimeError("message gone from channel")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                staging = Path(handle.name)
            await client.download_media(message, file_name=str(staging))
            # Rename last so a crash never leaves a half file that the next run
            # would adopt as complete.
            staging.replace(target)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            counters["failed"] += 1
            print(f"  FAILED msg {message_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        await _record(item_id, target.relative_to(root))
        counters["exported"] += 1

        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

    return counters


async def _record(item_id: int, relpath: Path) -> None:
    async with AsyncSessionLocal() as session:
        item = await session.get(CatalogItem, item_id)
        if item is not None:
            item.exported_path = str(relpath)
            item.exported_at = datetime.now(timezone.utc)
            await session.commit()


def _build_client():
    from pyrogram import Client

    kwargs: dict[str, object] = {
        "name": os.getenv("TELEGRAM_SESSION_NAME", "telegram_photo_vault"),
        "api_id": int(os.environ["TELEGRAM_API_ID"]),
        "api_hash": os.environ["TELEGRAM_API_HASH"],
        "sleep_threshold": int(os.getenv("TELEGRAM_SLEEP_THRESHOLD", "60")),
    }
    session_string = os.getenv("TELEGRAM_SESSION_STRING")
    if session_string:
        kwargs["session_string"] = session_string
    return Client(**kwargs)


async def main_async(args: argparse.Namespace) -> int:
    await init_db()
    root = Path(args.root or os.getenv("GALLERY_EXPORT_ROOT") or "./local-data/gallery")
    root.mkdir(parents=True, exist_ok=True)

    print(f"Gallery root : {root}")
    print(f"Mode         : {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"Limit        : {args.limit}\n")

    async with _build_client() as client:
        counters = await export_batch(
            client,
            root,
            limit=args.limit,
            apply=args.apply,
            include_native=args.include_native,
            delay_seconds=args.delay,
        )

    print("\n" + "=" * 52)
    for key, value in counters.items():
        print(f"{key:14}: {value}")
    if not args.apply:
        print("\nDry run: nothing was written. Re-run with --apply.")
    return 1 if counters["failed"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Export archived images into the gallery tree.")
    parser.add_argument("--root", help="Gallery root (default: $GALLERY_EXPORT_ROOT)")
    parser.add_argument("--limit", type=int, default=100, help="Items per run (default: 100)")
    parser.add_argument("--apply", action="store_true", help="Actually download and write")
    parser.add_argument(
        "--include-native",
        action="store_true",
        help="Also export Telegram-native photos. They carry no EXIF, so they land in the "
        "timeline dated by post date; off by default for that reason.",
    )
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_gallery_export.py -q
```

Expected: 14 passed.

- [ ] **Step 6: Document the knob**

`docker-compose.yml`:

```yaml
      - GALLERY_EXPORT_ROOT=${GALLERY_EXPORT_ROOT:-/data/gallery}
```

Add a row to the env tables in `AGENTS.md` and `docs/REFERENCE.md`:

| variable | default | meaning |
|---|---|---|
| `GALLERY_EXPORT_ROOT` | `<DATA_VOLUME_PATH>/gallery` | Tree of image originals that Immich mounts read-only as an external library. This repo is its only writer. |

Also add a short section to `docs/REFERENCE.md` describing the layout contract — `YYYY/YYYY-MM-DD/<name>`, `undated/` when no date is known, ` (N)` on collision — since Immich and any future tooling depend on it.

- [ ] **Step 7: Verify and commit**

```bash
.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check . && python3 -m compileall -q app scripts
rtk git add scripts/export_gallery.py app/services/catalog.py tests/test_gallery_export.py docker-compose.yml AGENTS.md docs/REFERENCE.md
rtk git commit -m "feat(gallery): export archived images into an Immich external library tree"
```

---

### Task 7: the worker's forward path

**Files:**
- Modify: `app/worker.py`
- Test: `tests/test_worker_state_machine.py`

**Interfaces:**
- Consumes: `gallery_relpath`, `resolve_collision`, `IMAGE_SUFFIXES` from Task 6.
- Produces: nothing new — an internal best-effort side effect.

The worker already holds the original on local disk between the MEGA download and `_finalize`'s cleanup. Copying it into the gallery there costs one `copy2` and no network at all, which is why the forward path is effectively free and only the backfill is expensive.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_worker_state_machine.py`:

```python
async def test_finalize_copies_an_image_into_the_gallery_before_deleting_it(
    clean_db, tmp_path
):
    gallery = tmp_path / "gallery"
    source = tmp_path / "IMG_0001.jpg"
    source.write_bytes(b"original bytes")

    worker = _worker(tmp_path, gallery_root=gallery)
    photo = await _photo(
        status=PhotoStatus.TG_UPLOADED,
        media_type=MediaType.IMAGE,
        local_path=str(source),
    )

    await worker._finalize(photo)

    written = list(gallery.rglob("*.jpg"))
    assert len(written) == 1
    assert written[0].read_bytes() == b"original bytes"
    assert not source.exists()  # _finalize is still the only deleter


async def test_finalize_does_not_copy_videos_into_the_gallery(clean_db, tmp_path):
    gallery = tmp_path / "gallery"
    source = tmp_path / "IMG_0002.mov"
    source.write_bytes(b"video bytes")

    worker = _worker(tmp_path, gallery_root=gallery)
    photo = await _photo(
        status=PhotoStatus.TG_UPLOADED,
        media_type=MediaType.VIDEO,
        local_path=str(source),
    )

    await worker._finalize(photo)

    assert not gallery.exists() or list(gallery.rglob("*")) == []


async def test_a_gallery_write_failure_never_fails_the_archival_step(clean_db, tmp_path):
    """Best-effort, exactly like the browse-channel mirror."""
    source = tmp_path / "IMG_0003.jpg"
    source.write_bytes(b"bytes")
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")

    worker = _worker(tmp_path, gallery_root=blocked)
    photo = await _photo(
        status=PhotoStatus.TG_UPLOADED,
        media_type=MediaType.IMAGE,
        local_path=str(source),
    )

    await worker._finalize(photo)

    assert photo.status is PhotoStatus.COMPLETED


async def test_no_gallery_root_configured_means_no_copy_and_no_error(clean_db, tmp_path):
    source = tmp_path / "IMG_0004.jpg"
    source.write_bytes(b"bytes")

    worker = _worker(tmp_path, gallery_root=None)
    photo = await _photo(
        status=PhotoStatus.TG_UPLOADED,
        media_type=MediaType.IMAGE,
        local_path=str(source),
    )

    await worker._finalize(photo)

    assert photo.status is PhotoStatus.COMPLETED
```

These use the file's existing helpers. If `_worker(...)` and `_photo(...)` are not already factored out in `tests/test_worker_state_machine.py`, extract them from the nearest existing test first, keeping the current construction arguments identical and adding only the new `gallery_root` keyword. Read the file before writing this step.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_worker_state_machine.py -q
```

Expected: `TypeError: PhotoWorker.__init__() got an unexpected keyword argument 'gallery_root'`.

- [ ] **Step 3: Implement the copy**

In `app/worker.py`, add the constructor keyword (beside the other path roots):

```python
        gallery_root: str | Path | None = None,
```

stored as:

```python
        self.gallery_root = Path(gallery_root) if gallery_root else None
```

Add the import:

```python
from app.services.catalog import IMAGE_SUFFIXES, gallery_relpath, resolve_collision
```

Add the helper:

```python
    def _copy_to_gallery(self, photo: Photo) -> None:
        """Place a newly archived image where Immich will find it.

        Best-effort by design: the bytes are already on disk at this point, so
        the copy is nearly free, but a gallery failure must never cost us an
        archived photo. Same posture as browse-channel mirroring.
        """
        if self.gallery_root is None or photo.media_type is not MediaType.IMAGE:
            return
        if not photo.local_path:
            return

        source = Path(photo.local_path)
        if not source.is_file() or source.suffix.lower() not in IMAGE_SUFFIXES:
            return

        relpath = gallery_relpath(None, datetime.now(timezone.utc), source.name, photo.id)
        target, already_there = resolve_collision(self.gallery_root, relpath, source.stat().st_size)
        if already_there:
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
```

Note the date arguments: the worker has no parsed EXIF at this point, so it files the copy under today's date. The catalog's `enrich` pass later reads the real capture date, and the item's `exported_path` records where the file actually is — Immich reads EXIF regardless of the folder, so a provisional folder is cosmetic, not incorrect. (Add `shutil`, `datetime`, `timezone` and `MediaType` to that module's imports if they are not already there.)

Call it as the **first** statement of `_finalize`, wrapped so it can never propagate:

```python
    async def _finalize(self, photo: Photo) -> None:
        try:
            self._copy_to_gallery(photo)
        except Exception:
            logger.warning("Gallery copy failed for photo %s", photo.id, exc_info=True)

        # ... existing body unchanged: MEGA delete, temp cleanup, COMPLETED
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_worker_state_machine.py -q
```

Expected: all pass, including every pre-existing test in the file.

- [ ] **Step 5: Wire the knob**

In `app/main.py`, pass to the `PhotoWorker(...)` construction:

```python
            gallery_root=os.getenv("GALLERY_EXPORT_ROOT")
            or str(Path(os.getenv("DATA_VOLUME_PATH", "/data")) / "gallery"),
        )
```

- [ ] **Step 6: Verify and commit**

```bash
.venv/bin/python -m pytest -q && .venv/bin/python -m ruff check . && python3 -m compileall -q app scripts
rtk git add app/worker.py app/main.py tests/test_worker_state_machine.py
rtk git commit -m "feat(gallery): copy newly archived images into the gallery on finalize"
```

---

### Task 8: manual runs against real Telegram

No new code. This is the sequence that turns a green suite into a working archive, and it is deliberately the last task: everything before it is verifiable offline, and nothing before it has touched the network.

**Run these from the repo root with the venv active and `.env` loaded.**

- [ ] **Step 1: Measure the HEIC assumption — the one number the spec assumes**

The 30-image probe was all JPEG, because that is what the archive channel holds. The 2,934 HEIC files are in the iPhone channel, and HEIC stores metadata in a `meta` box whose position is not guaranteed to be near the head. Re-run the sampling probe against `-1004373247014` before trusting a head-only read there:

```bash
.venv/bin/python -m scripts.export_gallery --help   # confirms the module imports cleanly
```

Then adapt the spike probe (kept in the session scratchpad, reproduced in the spec's Context section) to select `IMG_*.HEIC` rows and point at the iPhone channel. Record the HEIC hit rate.

**Decision rule:** if HEIC date extraction from 1 MiB is above ~80%, keep the head-only read. If it is below, raise `CATALOG_ENRICH_HEAD_BYTES` and re-measure; if that does not help, add a HEIC-only full-download path in a follow-up — roughly 4,717 files and 4.8 GB, which changes the pacing budget and nothing else in the design.

- [ ] **Step 2: Set `IPHONE_CHANNEL_ID` and run the first scan**

```bash
echo 'IPHONE_CHANNEL_ID="-1004373247014"' >> .env
curl -s -X POST -H "X-Api-Key: $API_KEY" localhost:8000/api/catalog/scan | python3 -m json.tool
```

Expected order of magnitude: ~18,294 items for the archive channel and ~6,916 for the iPhone one. A wildly different number means the scan is wrong; stop and investigate before enriching.

- [ ] **Step 3: Read the report before doing anything expensive**

```bash
curl -s -H "X-Api-Key: $API_KEY" localhost:8000/api/catalog/report | python3 -m json.tool
```

Check specifically:
- `provenance.UNKNOWN` around 15,193 for the archive channel — the legacy, now counted.
- `missing_from_channel.count` — **if this is not zero, stop and investigate.** It means a local record points at a message that is no longer there.
- `duplicates.by_name_unconfirmed.excess_messages` around 1,272 / ~62.8 GB. These are unconfirmed until hashes exist; do not act on them yet.

- [ ] **Step 4: Enrich in small batches, watching for FloodWait**

```bash
curl -s -X POST -H "X-Api-Key: $API_KEY" "localhost:8000/api/catalog/enrich?limit=50" | python3 -m json.tool
```

Run this a few times and watch the logs. Only once it is clearly stable, raise the limit. Expected shape from the sample: roughly 96% with a date and 73% with GPS, and a visibly lower GPS rate on pre-2019 material. Full enrichment is ~16,000 fetches; budget it as a background task over days, not an afternoon.

- [ ] **Step 5: Export a small slice and point Immich at it**

```bash
.venv/bin/python -m scripts.export_gallery --limit 50            # dry run first
.venv/bin/python -m scripts.export_gallery --limit 50 --apply
```

Stand up Immich with its own compose file, bind mount `GALLERY_EXPORT_ROOT` **`:ro`**, and add an external library pointing at the mount. Leave "delete offline files" off. Confirm on those 50 that dates are right and that geotagged shots appear on the map.

- [ ] **Step 6: Check disk, then run the full backfill**

```bash
df -h /home
```

Budget: gallery 33.0 GB + Immich thumbnails and previews ~7 GB + Postgres, against 167 GB free. Then run the backfill in slices (`--limit 500 --apply`, repeatedly) rather than in one pass, so a FloodWait or a reboot costs one slice.

- [ ] **Step 7: Record what was learned**

Add a short entry to `PLAN.md` — the top-level phase history — and note anything surprising in `docs/TROUBLESHOOTING.md`'s Known issues section, particularly the measured HEIC rate from Step 1 and any rate-limit behaviour worth remembering.

---

## Self-Review

**Spec coverage.**

| spec section | task |
|---|---|
| `catalog_items` schema | 1 |
| scan (all three channels, idempotent, iPhone channel included) | 2 |
| channel `role`, mirror never enriched or exported | 1 (column), 2 (scan), 4 (enrich filter), 6 (export filter) |
| match / provenance, `UNKNOWN` is not an error | 3 |
| enrich from the head, native-photo short circuit, one-fetch failures | 4 |
| reconciliation report + API | 5 |
| gallery export: layout, forward path, backfill, videos skipped | 6 (layout + backfill), 7 (forward path) |
| native photos excluded from export by default, behind a flag | 6 (`--include-native`) |
| Immich configuration (manual) | 8, Step 5 |
| four new env vars, five-edit rule | 2 (`IPHONE_CHANNEL_ID`, `CATALOG_SCAN_DELAY`), 4 (`CATALOG_ENRICH_*`), 6 (`GALLERY_EXPORT_ROOT`) |
| testing list | 2, 3, 4, 5, 6, 7 — one test file per named area |
| risk 1, pacing | Global Constraints; delays in 2, 4, 6; Steps 4 and 6 of Task 8 |
| risk 2, pre-pipeline legacy | 4 (short circuit), 5 (`native_no_exif`), 6 (excluded by default) |
| risk 3, partial GPS | 5 (`metadata.with_gps`), 8 Step 4 |
| risk 4, `message_date` is not capture date | 5 (`dated_by_message_only`), 6 (fallback + `undated/`) |
| risk 5, HEIC unproven | 8, Step 1, with an explicit decision rule |

No spec requirement is unimplemented. The spec's `is_chunked` / `manifest_tg_message_id` fields are deliberately replaced by `artifact`, with the reasoning stated in Global Constraints.

**Placeholder scan.** No "TBD", no "add error handling", no "similar to Task N". Every code step carries the code. Two steps intentionally require reading a file first — Task 7 Step 1 (the existing helpers in `tests/test_worker_state_machine.py`) and Task 2 Step 6 (`browse_channel_id`'s scope in `lifespan`) — and both say so explicitly rather than guessing at contents this plan cannot see.

**Type consistency.** `classify_artifact`, `extract_metadata`, `gallery_relpath` and `resolve_collision` keep the same signatures everywhere they appear. `ImageMetadata` field names match the `CatalogItem` columns they are assigned to. `IMAGE_SUFFIXES` is defined once in Task 4 and imported by Tasks 6 and 7. `enrich_batch` returns the same five keys in the service, the tests and the API. `export_batch`'s counters are the same five keys in the script, the tests and the printed report. `CatalogService.__init__` gains keywords in Tasks 2 and 4 only, and Task 4 lists them in the order they are appended.

**One thing this plan cannot verify for you.** Task 8, Step 3 says to stop if `missing_from_channel` is not zero. That is the first time anything in this system has been able to detect a lost file, so there is no prior expectation to compare against — a non-zero result may be a genuine loss or a matching bug, and telling them apart needs a human looking at a specific message id.
