# Catalog (reconciled state) + Immich bridge

Date: 2026-09-21. First spec in a two-phase effort. This one covers the catalog, the
reconciliation report, and the photo half of the gallery bridge. Videos in the gallery
are deliberately deferred to a second spec (see Non-goals).

## Goal

Two outcomes the archive cannot deliver today:

1. **A reconciled view of what is actually in Telegram.** One index over all three
   channels, keyed by the channel message rather than by whatever a local database
   happens to remember, answering: what exists, where it came from, what is missing,
   what is duplicated, and what has no usable metadata.
2. **A Google-Photos-grade client**, without writing one. Immich runs on the local
   machine and reads an exported, read-only photo tree as an *external library*; this
   repo stays the only writer. Date and location filtering, the map, faces, semantic
   search and the Android app all come from Immich unchanged.

## Context: what the survey and the measurements actually showed

**There is nothing to fork and no PR to send.** Immich (114.6k stars) has no storage
abstraction: object storage is deferred behind a large refactor — maintainer `bo0tzz`,
"making the codebase ready for S3 is a long term endeavour that needs significant
coordination and refactoring before making any S3 calls is even in the picture" — and
discussion #24608 was closed in December 2025 with "if it ever happens, S3 support will
be first/likely the only thing we offer". The v3 plugin system (Extism/Wasm, July 2026)
exposes album, tag and `httpRequest` host functions on `AssetCreate` /
`AssetMetadataExtraction` / `AssetTagged` events — no storage or file-read hooks. The
Telegram-as-filesystem projects (teldrive 3.1k, tgfs 113, tgmount-ng 54 and "VERY ALPHA")
each own their own on-channel format and cannot adopt channels written by this repo. The
Telegram photo-gallery clones (Telephoto 306, CloudGallery 453) are Bot-API, phone-side
pipelines with no server, no location and no reconciliation. External libraries are the
one integration point, and they need no fork.

**The database accounts for 17% of the archive channel.** Measured against
`local-data/telegram_photo_vault.db`:

```
archive channel items scanned (recovery_items)   18,294
claimed by the worker (photos COMPLETED)          3,101   = 17%
no matching row by tg_message_id                 15,193   = 83%
of those, media_kind = 'photo'                    3,201   native, Telegram-recompressed
```

Native `photo` messages are re-encoded by Telegram by definition: those 3,201 items have
no EXIF and are not original quality. They are pre-pipeline legacy and are not
recoverable — the report must make them visible, not pretend otherwise.

**There is a third channel nobody scans.** `IMG_*.HEIC` count in the archive channel: 0.
The iPhone migration (6,916 files, 105.1 GB) went to `-1004373247014`, recorded in the
`meta` table of `local-data/iphone_backup_state-2026-08-18.db`, and has never been
scanned. Three channels, three sources of truth, no view over the union:

```
-1002637897512   TELEGRAM_CHANNEL_ID   archive (Pixel/Samsung, 2016-2026)
-1004367643112   BROWSE_CHANNEL_ID     browse mirror
-1004373247014   (no env var yet)      iPhone migration
```

**Metadata survives, and costs almost nothing to read.** A 30-image sample spread across
the archive's message-date range, reading **only the first 1 MB of each file**:

```
DateTimeOriginal   29/30  (96%)
GPS                22/30  (73%)          26.5 MB read in total
```

GPS is present in 21 of the 22 shots from 2019 onward and absent in the 2016-2018 Samsung
files and in derived files (`-modificato`, `TS_exported`). EXIF lives at the head of the
file, so enrichment never needs a full download. This measurement is what makes Part 1
cheap, and the same truncated-blob parse is directly testable offline.

**The archive is 322.6 GB, and the photo half of it fits on disk with room to spare.**
Measured from the channel scan and the iPhone state DB, not from what the app DB claims:

```
archive channel (-1002637897512)                      18,294 items   217.5 GB
  images as documents (EXIF intact)                   11,337          25.7 GB
  native photos (Telegram-recompressed, no EXIF)       3,201           2.0 GB
  video / animation                                    2,300          70.2 GB
  other documents (MP4 uploaded as documents)          1,456         119.6 GB
iPhone channel (-1004373247014)                        6,916 files   105.1 GB
  of which images (HEIC/JPG/PNG)                       4,717           7.3 GB
                                                      ----------------------
total                                                                322.6 GB
gallery candidates (images with real EXIF)            16,054          33.0 GB
```

33.0 GB of photos against 167 GB free on `/home`: the photo half is comfortable. The
~290 GB of video is the half that has to stay cold, and it is deferred.

**About a fifth of the archive channel is duplicated.** Grouping the scan by file name:

```
names appearing more than once    1,246       (1,222 twice, 22 three times, 2 four times)
excess messages                   1,272
storage those excess copies hold   62.8 GB    = 29% of the archive channel
```

The iPhone channel is comparatively clean: 53 duplicate `sha256` values, 108 excess files.
Matching file names is suggestive, not conclusive — the report must confirm by `sha256`
before anyone treats a message as redundant, and this spec never deletes either way. But
it is a strong indication that reconciliation has an immediate, measurable payoff beyond
feeding a gallery.

**The WebP mirror discards all metadata.** Verified, not inferred:
`_compress_to_webp_sync` in `app/services/image.py:32` calls
`save(target, format="WEBP", quality=...)` with no `exif=`. A source JPEG carrying 196
bytes of EXIF (DateTimeOriginal + 4 GPS tags) mirrors to a WebP with 0 bytes of EXIF.
The ~2,300 images already on the Odroid are metadata-blind.

## Non-goals

- **No fork of Immich and no PR to it.** Settled above.
- **No FUSE mount, no videos in the gallery.** Videos are catalogued and enriched but not
  exported. How they reach the gallery (streaming mount vs. downscaled local copies vs.
  staying out) is a separate spec, decided after Immich has been seen working on the real
  photo archive rather than in theory.
- **No custom gallery UI.** Immich is the client. The dashboard gains a reconciliation
  panel and nothing more.
- **No change to the on-channel formats** — chunk names, chunk/manifest captions,
  manifest JSON. Files already uploaded carry them forever.
- **No deletion of anything, anywhere.** The catalog is read-only with respect to
  Telegram. It never edits a caption, never deletes a message. The existing
  dry-run-by-default posture is untouched because nothing here has a destructive mode.
- **The WebP EXIF fix is not in this spec.** It is real and worth doing, but it changes
  existing behaviour and is no longer on the critical path now that Immich reads true
  originals. It ships as its own small change with its own test.
- **No migration tool.** `catalog_items` is a new table created by `create_all`; no
  existing column is renamed or dropped.

## Part 1 — `catalog_items`

One row per media object **present in a channel**, keyed `(channel_id, tg_message_id)`.
The governing principle: *the channel is the archive; this table is a cache of what we
know about it.* That is the same posture that keeps the channel self-describing without
this repo, and it is why the key is the message and not a local path.

```python
class CatalogItem(Base):
    __tablename__ = "catalog_items"

    id: int
    channel_id: int                  # BigInteger, indexed
    tg_message_id: int               # BigInteger; UNIQUE(channel_id, tg_message_id)
    media_kind: str                  # document | photo | video | animation
    file_name: str | None
    file_size: int | None
    mime_type: str | None
    message_date: datetime | None    # when it was posted, not when it was shot

    # content identity
    sha256: str | None               # carried over from photos / the iPhone state DB
    is_chunked: bool
    manifest_tg_message_id: int | None

    # derived metadata — the new part
    taken_at: datetime | None
    gps_lat: float | None
    gps_lon: float | None
    width: int | None
    height: int | None
    camera_model: str | None
    enriched_at: datetime | None     # NULL = never attempted
    enrich_error: str | None         # why it has no metadata, when it has none

    # provenance
    source: str                      # worker | backup_script | unknown
    photo_id: int | None             # FK-ish to photos.id when matched
    backup_rel_path: str | None      # rel_path in the iPhone state DB when matched

    # gallery
    exported_path: str | None        # relative to the gallery root; NULL = not exported
    exported_at: datetime | None
```

Three operations, each independently resumable and each safe to re-run:

**scan** — walks a channel's history and upserts by `(channel_id, tg_message_id)`. Reuses
the traversal `RecoveryService` already performs; the new part is that it runs over all
three channels, including the iPhone one that has never been touched. Re-scanning is
idempotent: an existing row keeps its enrichment and its export state.

The three channels are not equivalent and the catalog must not treat them as such. The
archive and iPhone channels hold originals and are gallery sources. The browse channel
holds native re-compressed copies of things already archived elsewhere: it is scanned so
the report can show drift between an original and its mirror, and it is **never** a
gallery source nor an enrichment target. A `role` column (`archive` | `mirror`) on the
channel configuration makes this explicit rather than implied by an id comparison.

**match** — sets `source` by joining `photos` on `tg_message_id` (worker) and the iPhone
state DB on `tg_message_id`, falling back to `sha256` where both sides have one. Rows
that match nothing stay `unknown`. **`unknown` is not an error condition** — it is the
15,193-item legacy, counted for the first time.

**enrich** — for image rows with `enriched_at IS NULL`: stream the first
`CATALOG_ENRICH_HEAD_BYTES` (default 1 MiB), parse EXIF in memory, store `taken_at`,
`gps_lat`/`gps_lon` and the dimensions, discard the bytes. Nothing is written to disk.
Failures record `enrich_error` and set `enriched_at`, so a permanently unreadable file is
attempted once rather than retried forever. Processes one bounded batch per invocation,
in the style of the recovery tidy, with `RECOVERY_DELAY`-equivalent pacing between
fetches.

Native `photo` rows are marked enriched with `enrich_error = "telegram-native, no exif"`
without a fetch: the format guarantees the answer, so spending a request on it is waste.

## Part 2 — the reconciliation report

`GET /api/catalog/report` (behind `require_api_key`, like every other `/api/*` route) plus
a dashboard panel. It answers, per channel and in total:

| question | today |
|---|---|
| items by channel, by year of capture, by kind | unknown |
| items with no known provenance | 15,193, never counted |
| **rows in a local DB with no matching message in the channel** | **unknown — the one that matters** |
| duplicate `sha256` within and across channels | 1,272 excess by name / ~62.8 GB, unconfirmed by hash |
| items with no `taken_at` / no GPS, and why | unknown |
| items not yet exported to the gallery | n/a |

The "in the DB but not in the channel" count is the reason this component exists: it is
the only query that can tell the user something has been lost, and no current code path
can answer it.

## Part 3 — gallery export

Immich reads `GALLERY_EXPORT_ROOT` (default `<DATA_VOLUME_PATH>/gallery`) as a read-only
external library. Layout `YYYY/YYYY-MM-DD/<file_name>`, date taken from
`catalog_items.taken_at`, falling back to `message_date` when absent. Immich does not need
the tree — it reads EXIF — but the tree makes the export idempotent, resumable and
browsable by hand. Name collisions get a ` (2)` suffix; an existing file with the right
size is left alone.

**Forward path — effectively free.** Between the MEGA download and `_finalize`, the
worker already holds the original on local disk and then deletes it. For `MediaType.IMAGE`
only, copy it into the gallery tree before cleanup. The bytes were already there; the
cost is the copy. The invariant that `_finalize` is the only step that deletes the source
is preserved — the gallery write happens strictly before it, and a failed gallery write
must not fail the archival step (same best-effort posture as browse-channel mirroring).

**Backfill — one-off.** A standalone `scripts/export_gallery.py` in the style of
`backup_local_folder.py`: its own resumable state (here, `catalog_items.exported_path`),
`--dry-run` by default, `--limit` for a bounded first run, prints a report. It drains
image rows with `exported_path IS NULL`, downloads each in full, writes it into the tree.
16,054 files and 33.0 GB across both channels — an overnight job, not an afternoon one,
and resumable precisely so it can be run in slices. It never deletes anything.

Native `photo` rows are **excluded from the export by default**, behind a flag. They
carry no EXIF, so they would land in the timeline dated by `message_date` (2025-2026)
regardless of when they were actually taken, polluting ten years of otherwise correct
chronology with 3,201 misplaced items. Whether to take them anyway is the owner's call,
made against the report rather than by default.

Videos are catalogued and enriched but skipped by the exporter.

## Part 4 — Immich configuration (manual, not code)

Not automated, and not this repo's job. Recorded here so the spec is complete:

1. Immich via its own compose file on the local machine, with `GALLERY_EXPORT_ROOT` bind
   mounted **`:ro`**. This repo stays the only writer of that tree.
2. An external library pointing at the mount. Leave "delete offline files" off.
3. Expected local footprint: gallery 33.0 GB + Immich thumbnails and previews ~7 GB for
   16,054 photos + Postgres, so roughly 40-45 GB against 167 GB free. Comfortable, but no
   longer negligible — worth re-checking `df` before the backfill rather than after.

## New configuration

Each knob follows the repo's five-edit rule (parse in `lifespan`, pass as a constructor
kwarg, add to `docker-compose.yml`, document in `AGENTS.md` and `docs/REFERENCE.md`) —
the `add-config-knob` skill covers it.

| variable | default | purpose |
|---|---|---|
| `IPHONE_CHANNEL_ID` | unset | third channel to scan; scanning is skipped when unset |
| `GALLERY_EXPORT_ROOT` | `<DATA_VOLUME_PATH>/gallery` | what Immich mounts read-only |
| `CATALOG_ENRICH_BATCH_SIZE` | `200` | items enriched per invocation |
| `CATALOG_ENRICH_HEAD_BYTES` | `1048576` | bytes fetched per item for EXIF |

## Testing

House style throughout: hand-written fakes that duck-type only what is under test, no
`unittest.mock` patching, `tmp_path` wherever the filesystem is touched, the `clean_db`
fixture, and no network anywhere.

- `test_catalog_scan.py` — upsert idempotency (scanning twice yields the same rows and
  does not clear enrichment or export state); multi-channel isolation; a channel id left
  unset is skipped rather than erroring.
- `test_catalog_match.py` — provenance by `tg_message_id` for both sources, the `sha256`
  fallback, and the unmatched case staying `unknown` without being treated as a failure.
- `test_catalog_enrich.py` — **EXIF parsed from a truncated blob**, using a fixture built
  the way the spike built one (JPEG with `DateTimeOriginal` + GPS, cut to the head): a
  file with both, one with date only, one with neither, one that is unreadable (records
  `enrich_error`, is not retried), and a native `photo` row short-circuited with no fetch.
- `test_catalog_report.py` — each counter, especially "in the DB but not in the channel",
  built from a hand-made mismatch.
- `test_gallery_export.py` — tree layout from `taken_at` and the `message_date` fallback;
  idempotent re-run; collision suffixing; videos skipped; `--dry-run` writing nothing.
- `test_worker_state_machine.py` (extended) — the forward-path copy happens before
  `_finalize`, only for images, and a gallery-write failure does not fail the step.

## Open risks, explicitly accepted

1. **Telegram rate limits, and this is the largest risk.** Enrichment is ~16,000 partial
   fetches (~16 GB streamed and discarded) and the backfill ~16,000 full downloads
   (33.0 GB). Mitigated by the client's existing `sleep_threshold`, per-item pacing and
   bounded batches, and by both being fully resumable at item granularity. The risk is
   elapsed time, not data loss — but teldrive's own README warns that Telegram API misuse
   gets accounts banned instantly, and losing this account means losing access to 322.6 GB
   of the only offsite copy. Pacing is not a tuning parameter here; it is a safety
   requirement, and the first runs must be `--limit`ed and observed.
2. **The pre-pipeline legacy is not recoverable.** The 3,201 native `photo` items were
   re-encoded by Telegram when they were posted. They will appear in the gallery, if
   exported at all, at reduced quality with no EXIF. The report surfaces them; what to do
   about them is the owner's decision and is out of scope here.
3. **GPS coverage is 73%, not 100%**, and is concentrated in 2019+. The map will have
   holes for the 2016-2018 material. Accepted: this is a property of the source files, not
   something the pipeline can fix.
4. **`message_date` is not capture date.** Rows that fail enrichment fall back to the date
   the file was posted, which for the migrated archive is 2025-2026 regardless of when the
   photo was taken. Such items will cluster wrongly in the timeline. The report counts
   them so the size of the problem is visible rather than silently wrong.
5. **Truncated-head EXIF is proven for JPEG and unproven for HEIC.** The 30-image sample
   was all JPEG, because that is what the archive channel holds; the 2,934 HEIC files live
   in the iPhone channel, which has never been scanned. HEIC stores metadata in a `meta`
   box whose position is not guaranteed to be near the head, and `pillow_heif` may refuse a
   truncated file outright. The first enrichment task is therefore to re-run the sampling
   probe against the iPhone channel and measure the HEIC hit rate. If the head-only read
   fails there, the fallback is a full download for HEIC only — correct, but roughly
   4,717 files and 4.8 GB instead of a partial read, which changes the pacing budget and
   nothing else. This is the one number in this spec that is assumed rather than measured.
