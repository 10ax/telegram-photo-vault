# Reference

Technical reference for Telegram Photo Vault. For setup and workflows, see the
[README](../README.md).

- [Environment variables](#environment-variables)
- [HTTP API](#http-api)
- [Device reconciliation](#device-reconciliation)
- [State machines](#state-machines)
- [Database schema](#database-schema)
- [Chunked-file formats](#chunked-file-formats)
- [Device inventory manifest](#device-inventory-manifest)
- [vault_merge CLI](#vault_merge-cli)

## Environment variables

### Required

| Variable | Purpose |
|---|---|
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | Telegram app credentials (my.telegram.org) |
| `TELEGRAM_CHANNEL_ID` | Target channel (`-100…` numeric form or `@username`) |
| `API_KEY` | Shared secret for `/api/*`, sent as `X-Api-Key` |
| `ODROID_HOST` / `ODROID_USERNAME` | SFTP mirror target |
| `ODROID_KNOWN_HOSTS` | Host-key file for SFTP verification (required unless insecure mode) |
| `MEGA_EMAIL` + `MEGA_PASSWORD` | MEGA login — *or* mount an authenticated MEGAcmd session at `/root/.megaCmd` |

### Optional

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/telegram_photo_vault.db` | Compose overrides to `/data/…` |
| `LOG_LEVEL` | `INFO` | Root logging level |
| `MEGA_TARGET_FOLDER` | `/Camera` | Remote folder watched for new files |
| `TELEGRAM_SESSION_NAME` | `telegram_photo_vault` | Session file name |
| `TELEGRAM_SESSION_STRING` | – | Portable session; avoids interactive login |
| `TELEGRAM_UPLOAD_DELAY` | `10` | Seconds slept after every upload. See `docs/telegram-rate-limits.md` |
| `TELEGRAM_SLEEP_THRESHOLD` | `60` | FloodWaits shorter than this are slept automatically |
| `WORKER_MODE` | `interval` | `interval` (scheduled) or `manual` (on-demand only) |
| `WORKER_RUN_INTERVAL` | `900` | Seconds between scheduled runs |
| `WORKER_FILE_DELAY` | `0` | Seconds slept between photos inside a run |
| `WORKER_MAX_RETRIES` | `3` | Step failures before a photo is marked `FAILED` |
| `WORKER_BATCH_SIZE` | `50` | Active photos fetched per pass |
| `WORKER_DOWNLOAD_ROOT` | `/data/tmp` | Original downloads |
| `WORKER_COMPRESSED_ROOT` | `/data/compressed` | WebP output |
| `CHUNK_THRESHOLD` | `1950000000` | Files above this many bytes are chunked |
| `CHUNK_SIZE` | `1900000000` | Chunk size in bytes. Raise both only on Premium (4 GB cap) |
| `RECOVERY_DOWNLOAD_ROOT` | `/data/recovery` | Recovery temp downloads |
| `RECOVERY_DELAY` | `8` | Seconds slept between recovery items |
| `RECOVERY_MAX_RETRIES` | `3` | Failures before a recovery item is `FAILED` |
| `RECOVERY_KINDS` | `photo,video,document,animation` | Message media kinds ingested by the scan |
| `RECOVERY_DELETE_OLD` | `true` | Delete originals after the tidy replacement is confirmed |
| `ODROID_PORT` | `22` | |
| `ODROID_PASSWORD` / `ODROID_KEY_PATH` | – | One of the two |
| `ODROID_REMOTE_DIR` | `/srv/photo-vault` | |
| `ODROID_ALLOW_INSECURE_HOST_KEY` | `false` | Test-only: skips host-key verification |
| `DATA_VOLUME_PATH` | `/data` | Disk reported by `/api/system` |
| `IPHONE_CHANNEL_ID` | unset | Third channel to catalogue, created by `scripts/backup_local_folder.py`. Its id is in the `meta` table of that script's state DB. Unset means the channel is skipped. |
| `CATALOG_SCAN_DELAY` | `2` | Seconds between channels during a full scan |
| `RECONCILE_MAX_ENTRIES` | `10000` | Inventory entries accepted per `reconcile` call; more is refused with `413` and a message telling the client to continue against the same `snapshot_id` |
| `RECONCILE_FINGERPRINT_BYTES` | `262144` | Bytes hashed at each end of a file when `/api/vault/verify` settles an ambiguous match. Telegram streams in 1 MiB chunks, so the traffic cost is 2 MiB regardless, and a value above 1 MiB is clamped to it. The effective value is published as `fingerprint_window_bytes` by `GET /api/catalog/freshness` |
| `BACKUP_STATE_DB` | unset | Path to `scripts/backup_local_folder.py`'s own state DB. When set, `POST /api/catalog/scan` also attributes catalog rows to that script. Unset means only the worker's own provenance is matched |

## HTTP API

`GET /` (dashboard) and `GET /health` are unauthenticated. Everything under
`/api` requires the `X-Api-Key` header; a wrong or missing key returns `401`,
an unconfigured `API_KEY` returns `503`.

### `GET /api/status`

```json
{
  "photos": {"PENDING": 0, "DOWNLOADED": 0, "CHUNK_UPLOADING": 0, "TG_UPLOADED": 0,
             "COMPRESSED": 0, "ODROID_UPLOADED": 0, "COMPLETED": 12, "FAILED": 1, "SKIPPED": 2},
  "worker": {
    "mode": "interval", "run_interval_seconds": 900.0, "running": false,
    "last_run_started_at": "2026-07-04T10:00:00+00:00",
    "last_run_finished_at": "2026-07-04T10:00:05+00:00",
    "next_run_at": "2026-07-04T10:15:05+00:00",
    "last_run_error": null
  },
  "recovery": {
    "running": false, "activity": null, "delete_old": true, "last_error": null,
    "items": {"SCANNED": 0, "DOWNLOADED": 0, "PLANNED": 0, "REUPLOADED": 0,
              "COMPLETED": 0, "SKIPPED": 0, "DUPLICATE": 0, "FAILED": 0}
  }
}
```

### `POST /api/run`

Wakes the worker immediately (also queues a fresh run if one is in progress).
Returns `{"triggered": true, "worker": {…}}`. `503` if the worker isn't running.

### `GET /api/photos`

Query: `status` (a `PhotoStatus` value; `422` on unknown), `limit` (1–500,
default 50), `offset`. Ordered by `updated_at` descending.

```json
{"total": 2, "limit": 50, "offset": 0, "items": [
  {"id": 7, "mega_path": "/Camera/x.jpg", "status": "FAILED", "media_type": "IMAGE",
   "failed_status": "TG_UPLOADED", "tg_message_id": null, "retry_count": 3,
   "error_log": "Traceback …", "created_at": "…", "updated_at": "…"}
]}
```

`error_log` is truncated to the last 4000 characters.

### `POST /api/photos/{id}/retry`

Requeues a `FAILED` photo at the step recorded in `failed_status`, resetting
its retry budget, and triggers a run. If a prerequisite file no longer exists
on disk, the resume point walks back (e.g. to `PENDING` for a re-download).
`404` unknown id, `409` if the photo isn't `FAILED`.

### `POST /api/recovery/scan`

Starts a background scan of the full channel history. `409` if a recovery task
is already running. Returns the recovery snapshot.

### `POST /api/recovery/run`

Body: `{"dry_run": true}` (default when omitted). Dry run stops after planning
captions (`PLANNED`) and touches nothing on Telegram; the real run re-uploads
and (if `RECOVERY_DELETE_OLD`) deletes originals. `409` if busy.

### `GET /api/recovery/items`

Same query parameters as `/api/photos` (statuses from `RecoveryStatus`).
Items include `tg_message_id`, `media_kind`, `file_name`, `file_size`,
`message_date`, `sha256`, `planned_caption`, `new_tg_message_id`.

### `GET /api/system`

`{"path": "/data", "total_bytes": …, "used_bytes": …, "free_bytes": …, "used_percent": 42.13}`

## Device reconciliation

A phone-side client (Termux script, adb script, Android app — none of which
exist in this repo) builds an [inventory manifest](#device-inventory-manifest)
of its local files, asks the server for a verdict on each, and only ever
deletes a file the server called `ARCHIVED`. **Nothing here writes to
Telegram, and the server never deletes anything**: `POST
/api/devices/{device_id}/deletions` records what a client says it already
deleted — it does not delete.

### Verdicts

| Verdict | Meaning |
|---|---|
| `ARCHIVED` | The bytes are already in the archive channel. **Only the `ARCHIVED` verdict that `POST /api/devices/{device_id}/reconcile` returns authorises a deletion.** `GET /api/vault/lookup`'s `ARCHIVED` is informational only — see that endpoint below for why. |
| `IN_FLIGHT` | The pipeline is still processing this file, or the catalog may be older than it. Wait and re-check. |
| `AMBIGUOUS` | Partial evidence only (size mismatch, hash mismatch, case-only name match, zero-byte file, …). Resolve with `POST /api/vault/verify` or by eye. |
| `NOT_ARCHIVED` | No credible match. Keep the file — the upload pipeline may have a gap. |

### Match tiers

| Tier | Evidence |
|---|---|
| `HASH` | The entry's `sha256` equals a catalog row's whole-file hash. |
| `FINGERPRINT` | Settled by `POST /api/vault/verify`: the head+tail hashes of the archived copy match the entry's own **and** the archived copy's own size equals the entry's. Both halves are required — two files can share their first and last window and differ in the middle, and a size mismatch is the commonest reason an entry was `AMBIGUOUS` to begin with. |
| `NAME_SIZE` | Filename and size match a catalog row, and the entry's `mtime` is not newer than the catalog's `frontier` (how recently the archive channels were last scanned) — otherwise the match is treated as possibly stale and the verdict falls back to `IN_FLIGHT` instead. |

### `GET /api/catalog/freshness`

How old the catalog is, so a client can judge whether to trust a
`NOT_ARCHIVED` verdict or ask for a rescan first.

```json
{"frontier": "2026-09-20T18:30:00+00:00",
 "archive_rows": 4213,
 "fingerprint_window_bytes": 262144,
 "channels": [
   {"channel_id": -1002637897512, "last_scanned_at": "2026-09-25T09:00:00+00:00", "newest_message_date": "2026-07-01T00:00:00+00:00", "rows": 4100},
   {"channel_id": -1002900000001, "last_scanned_at": "2026-09-20T18:30:00+00:00", "newest_message_date": "2026-08-18T00:00:00+00:00", "rows": 113}
 ]}
```

`archive_rows` is `0` before the first `POST /api/catalog/scan`. Only the two
endpoints that call `evaluate` — `GET /api/vault/lookup` and `POST
/api/devices/{device_id}/reconcile` — depend on a scanned catalog and answer
`409` until then; the rest of the endpoints on this page don't.

`frontier` is the **oldest** of the per-channel `last_scanned_at` values, and it
is what gates a `NAME_SIZE` match: an entry whose `mtime` is newer than the
frontier cannot be trusted to have been seen by the scan that vouches for its
channel. It is deliberately scan time, not message date. A dormant archive
channel must not pin it: the iPhone migration channel's newest message date
never advances, but a scan of it today is still current, and frontiering on
message dates would demote every newer local file forever. An archive channel
that has not been scanned since freshness started being tracked has no
`last_scanned_at`, so `frontier` is `null` and every metadata match fails closed
until it is scanned — `channels` is there to say which one that is.

`newest_message_date` is reported per channel for information only: it is how
far that channel's own timeline reaches, which is not the same as how fresh the
catalog is.

`fingerprint_window_bytes` is the window a client must hash at each end of a
local file for `POST /api/vault/verify` to agree with it. It is the effective
value of `RECONCILE_FINGERPRINT_BYTES` after clamping, so read it rather than
assuming the default.

### `GET /api/vault/lookup`

Query: `name`, `size`. A one-off spot check with no `mtime` or `sha256`. It
still consults the worker's own pipeline state like a real reconcile entry
does — a file with a `PENDING` photo row comes back `IN_FLIGHT` here too —
but having no local `mtime` to judge staleness with, it explicitly opts out
of the catalog-freshness check that protects a real reconcile entry's
`NAME_SIZE` match from a stale catalog. The response says so
(`"freshness_gate": false`) so a client can't miss it: **a lookup verdict is
informational only, and never authorises a deletion by itself — only
`POST /api/devices/{device_id}/reconcile`'s `ARCHIVED` does.**

```json
{"verdict": "ARCHIVED", "tier": "NAME_SIZE", "reason": null,
 "channel_id": -1002637897512, "tg_message_id": 1, "freshness_gate": false}
```

`409` if the catalog has never been scanned.

### `POST /api/devices/{device_id}/reconcile`

Body:

```json
{
  "entries": [
    {"relpath": "DCIM/Camera/IMG_1.jpg", "name": "IMG_1.jpg", "size": 4213556,
     "mtime": "2026-07-01T08:00:00+00:00", "sha256": null}
  ],
  "snapshot_id": null,
  "taken_at": null,
  "final": true
}
```

Each entry follows the [inventory manifest](#device-inventory-manifest)
format. `snapshot_id` continues a previous, not-yet-`final` call for the
*same* `device_id` into the same snapshot (send several calls for a library
larger than `RECONCILE_MAX_ENTRIES`); `final: true` (the default) closes it.

Response:

```json
{
  "snapshot_id": 7,
  "catalog": {"frontier": "2026-09-25T09:00:00+00:00", "archive_rows": 4213},
  "summary": {
    "ARCHIVED": {"files": 4100, "bytes": 812345678},
    "IN_FLIGHT": {"files": 3, "bytes": 9000000},
    "AMBIGUOUS": {"files": 2, "bytes": 400000},
    "NOT_ARCHIVED": {"files": 5, "bytes": 1200000},
    "TOTAL": {"files": 4110, "bytes": 822945678}
  },
  "entries": [
    {"relpath": "DCIM/Camera/IMG_1.jpg", "verdict": "ARCHIVED", "tier": "NAME_SIZE",
     "reason": null, "channel_id": -1002637897512, "tg_message_id": 1}
  ]
}
```

`entries` echoes a verdict for every entry sent in *this* call, in order —
including `ARCHIVED` ones, which are otherwise not stored: they are the bulk
of any library, and the only record of one that survives is a
`deletion_audits` row, once the client reports deleting it. Non-`ARCHIVED`
entries are additionally persisted as `device_findings` under the snapshot,
retrievable later from `GET /api/devices/{device_id}/snapshot`.

`summary` and `catalog` describe the *whole* snapshot to date — cumulative
across every call made against this `snapshot_id`, including earlier ones —
while `entries` covers only the files sent in *this* call. A client sending a
large library in several calls should read progress from `summary`, not by
summing `entries` across calls. `catalog` is the same object
[`GET /api/catalog/freshness`](#get-apicatalogfreshness) returns, including the
per-channel breakdown and `fingerprint_window_bytes`.

**A `reconcile` call is not idempotent, and retrying one is not safe.** The
snapshot counters are folded in by a read-modify-write with no lock, and
findings carry no uniqueness constraint, so a call that is sent twice against
the same open `snapshot_id` counts every entry in it twice and writes every
finding in it twice. The summary is what the owner reads before deciding how
much to delete, so a double-counted one is not a cosmetic problem. A client
that cannot tell whether a call landed — a timeout, a dropped connection, any
ambiguous outcome — must treat that as **fatal to the snapshot**: stop using
that `snapshot_id`, start a new one with `snapshot_id: null`, and send the
whole inventory again. Do not retry into an open snapshot.

Refusals:
- `413` — more entries than `RECONCILE_MAX_ENTRIES` in one call. Continue in
  several calls, passing the `snapshot_id` the first call returned.
- `404` — `snapshot_id` doesn't refer to any snapshot at all.
- `409` — the catalog has never been scanned; or `snapshot_id` refers to a
  snapshot that exists but belongs to a different `device_id`; or that
  snapshot is already closed (a previous call against it sent `final: true`).

### `GET /api/devices/{device_id}/snapshot`

The most recent snapshot for a device, and its findings (non-`ARCHIVED`
entries only):

```json
{
  "snapshot": {
    "id": 7, "device_id": "pixel", "taken_at": null,
    "completed_at": "2026-09-24T10:00:00+00:00",
    "total_files": 4110, "total_bytes": 822945678,
    "archived_files": 4100, "archived_bytes": 812345678,
    "in_flight_files": 3, "ambiguous_files": 2, "not_archived_files": 5
  },
  "findings": [
    {"relpath": "DCIM/Camera/IMG_9.jpg", "file_name": "IMG_9.jpg", "file_size": 300000,
     "verdict": "NOT_ARCHIVED", "reason": "no_match"}
  ]
}
```

`404` if the device has no snapshot yet.

### `POST /api/devices/{device_id}/deletions`

Body:

```json
{
  "deleted": [
    {"relpath": "DCIM/Camera/IMG_1.jpg", "name": "IMG_1.jpg", "size": 4213556,
     "tier": "NAME_SIZE", "channel_id": -1002637897512, "tg_message_id": 1,
     "deleted_at": "2026-09-24T10:00:00Z"}
  ]
}
```

Records that the client already deleted these local files — one
`deletion_audits` row per entry, permanent, with no foreign key to any
snapshot so it outlives snapshot pruning. **The server never deletes
anything**; this is a log of what a client did, kept so a deletion stays
traceable back to the channel message that justified it. Response:
`{"recorded": 1}`.

`tier` must be one of the three [match tiers](#match-tiers) above (`422` if
not) — rejected before anything in the batch is considered audited, rather
than losing the whole batch's audit partway through a loop after the client
has already deleted the files.

### `POST /api/catalog/scan`

Starts a **background** rescan of every configured channel
(`CatalogService.scan_all`), which then matches against the worker's own DB
and, if `BACKUP_STATE_DB` is set, against `scripts/backup_local_folder.py`'s
state DB. This is what takes `archive_rows` above `0` — see
[`GET /api/catalog/freshness`](#get-apicatalogfreshness) above for which
endpoints that gates. A real channel is tens of thousands of messages of paced
Telegram traffic, far longer than an HTTP client will wait, so the call returns
immediately with a status snapshot and the work continues behind it. `409` if a
scan is already running; `503` without a catalog service configured.

Provenance is matched **per channel**, because message ids restart at `1` in
every channel: worker rows are claimed only in `TELEGRAM_CHANNEL_ID`, and
backup-script rows only in the channel recorded in that script's own
`meta.channel_id`. A state DB that predates that record falls back to matching
on `sha256` alone.

```json
{"running": true, "activity": "scanning channel history", "last_error": null,
 "result": null,
 "channels": [{"channel_id": -1002637897512, "role": "ARCHIVE"}]}
```

### `GET /api/catalog/status`

The same snapshot, for polling a scan to completion. When one has finished,
`running` is `false` and `result` holds what it did:

```json
{"running": false, "activity": null, "last_error": null,
 "result": {"scanned": {"-1002637897512": {"scanned": 4213, "ingested": 12, "updated": 3}},
            "matched": {"worker": 4100, "backup_script": 113}},
 "channels": [{"channel_id": -1002637897512, "role": "ARCHIVE"}]}
```

`503` without a catalog service configured.

### `POST /api/catalog/resolve-manifests`

Query: `limit` (1–500, default 50). Gives a chunked original (a file split
because it was over 2 GB) an identity in the catalog by reading its manifest,
so it becomes findable by name+size like any other file. `503` without a
catalog service configured.

```json
{"resolved": 1, "failed": 0, "remaining": 0}
```

### `POST /api/vault/verify`

Body: `{"channel_id", "tg_message_id", "file_size", "head_sha256", "tail_sha256"}`.

Settles one `AMBIGUOUS` entry by hashing `RECONCILE_FINGERPRINT_BYTES` bytes
from each end of the archived copy — no download of the middle, ~2×
`RECONCILE_FINGERPRINT_BYTES` of Telegram traffic regardless of file size —
and comparing against the client's own head/tail hashes of the same window.
Hash exactly the first and last `fingerprint_window_bytes` bytes of the local
file, as [`GET /api/catalog/freshness`](#get-apicatalogfreshness) reports that
number; for a file shorter than the window, both hashes are of the whole file.

```json
{"match": true, "head_sha256": "…", "tail_sha256": "…", "archived_file_size": 4213556}
```

`archived_file_size` is the size Telegram reports for the archived message's
own media, read from the message and never echoed back from the request.
**`match` is `true` only when both digests agree *and* `archived_file_size`
equals the `file_size` sent** — that is the whole of tier `FINGERPRINT`, and
the size half is what makes it evidence rather than a coincidence of two ends.
A message carrying no sized media (a native `photo` mirror, say) reports
`null` and can never match.

`503` without a Telegram service configured. `404` — naming the channel and
message — if the message is missing or has been deleted from the channel.

## State machines

### Photo (`photos` table)

```
PENDING ──▶ DOWNLOADED ──▶ TG_UPLOADED ──▶ COMPRESSED ──▶ ODROID_UPLOADED ──▶ COMPLETED
                │              │ (VIDEO: skips straight to finalize)
                │ (> CHUNK_THRESHOLD)
                ▼
        CHUNK_UPLOADING ──(all chunks + manifest)──▶ TG_UPLOADED
```

- Finalize (the `ODROID_UPLOADED → COMPLETED` step, or `TG_UPLOADED →
  COMPLETED` for videos) deletes the MEGA source and local temp files. For
  chunked files this is what gates MEGA deletion behind the manifest upload.
- Any step failing `WORKER_MAX_RETRIES` times → `FAILED`, with the failing
  step stored in `failed_status` for retry.
- Unsupported file types are ingested directly as `SKIPPED` (never processed,
  left on MEGA).
- One chunk uploads per worker visit to a photo — deliberate, for fairness
  across files and small crash windows.

### Recovery item (`recovery_items` table)

```
SCANNED ──▶ DOWNLOADED ──▶ PLANNED (dry-run) ──▶ REUPLOADED ──▶ COMPLETED
                                                  (delete original happens between these two)
```

Terminal/side states: `SKIPPED` (already tidy, or source message gone),
`DUPLICATE` (same SHA-256 as an earlier item; never deleted), `FAILED`.
FloodWait pauses do not consume an item's retry budget.

## Database schema

SQLite via SQLAlchemy async; `init_db()` creates tables and applies **additive
column migrations** (`PRAGMA table_info` + `ALTER TABLE ADD COLUMN`) so older
databases upgrade in place.

**photos** — `id`, `mega_path` (unique), `local_path`, `compressed_path`,
`status`, `media_type` (`IMAGE|VIDEO|OTHER`), `failed_status`,
`tg_message_id`, `is_chunked`, `sha256` (whole file), `total_size`,
`manifest_tg_message_id`, `retry_count`, `error_log`, `created_at`, `updated_at`.

**upload_chunks** — `id`, `photo_id` (FK, cascade), `part_index`,
`part_count`, `offset`, `size`, `sha256`, `filename`, `status`
(`PENDING|UPLOADED`), `tg_message_id`, timestamps. Unique on
`(photo_id, part_index)`.

**recovery_items** — `id`, `tg_message_id` (unique), `media_kind`,
`file_name`, `file_size`, `message_date`, `status`, `local_path`, `sha256`,
`planned_caption`, `new_tg_message_id`, `retry_count`, `error_log`, timestamps.

**catalog_items** — one row per media message in one channel; the lookup
surface for [device reconciliation](#device-reconciliation). `id`,
`channel_id`, `tg_message_id`, `channel_role` (`ARCHIVE|MIRROR`), `media_kind`
(`photo|video|animation|document`), `artifact` (`chunk|manifest`, else NULL),
`file_name`, `file_size`, `mime_type`, `message_date`, `sha256`,
`chunked_original_name`, `chunked_total_size`, `chunked_sha256` (the original a
resolved manifest describes; NULL on every other row), `taken_at`, `gps_lat`,
`gps_lon`, `width`, `height`, `camera_model`, `enriched_at`, `enrich_error`,
`source` (`UNKNOWN|WORKER|BACKUP_SCRIPT`), `photo_id` (FK), `backup_rel_path`,
`exported_path`, `exported_at`, timestamps. Unique on
`(channel_id, tg_message_id)` — message ids restart at `1` in every channel, so
the pair is the key and the bare id is not.

**device_snapshots** — one reconciliation run for one device, aggregates only.
`id`, `device_id`, `taken_at` (the client's clock, recorded and never trusted
for logic), `completed_at`, `total_files`, `total_bytes`, and a files/bytes
pair per verdict: `archived_*`, `in_flight_*`, `ambiguous_*`,
`not_archived_*`, timestamps.

**device_findings** — the non-`ARCHIVED` entries of a snapshot, the only ones
worth a row per file. `id`, `snapshot_id` (FK, cascade), `relpath`,
`file_name`, `file_size`, `verdict`, `reason`, `created_at`.

**deletion_audits** — what a client reported deleting, and the message that
holds the bytes. `id`, `device_id`, `relpath`, `file_name`, `file_size`,
`tier`, `channel_id`, `tg_message_id`, `deleted_at` (the client's), `recorded_at`
(the server's). Deliberately **no** foreign key to `device_snapshots`, so it
outlives snapshot pruning: this is what makes a deletion reconstructible.

## Chunked-file formats

Full rationale in [`video-chunking-design.md`](video-chunking-design.md).

**Chunk naming** — `<original_filename>.part<NNN>-of-<MMM>`, zero-padded to at
least 3 digits (wider automatically if > 999 parts). Lexicographic order ==
numeric order, so `LC_ALL=C cat name.part* > name` is byte-exact.

**Chunk caption**

```
#2024 #06_2024 #2024_06_01
#chunked #part003_of_012
file=IMG_2024.mp4 size=22548578304 sha256=9f2b6c01deadbeef
```

Date hashtags come from the original file (EXIF → filename date → mtime);
`sha256=` is the first 16 hex chars of the whole-file hash.

**Manifest** — `<original_filename>.manifest.json`, uploaded **after** all
chunks (commit marker), caption = date hashtags + `#manifest`:

```json
{
  "manifest_version": 1,
  "kind": "telegram-photo-vault/chunked-file",
  "original_filename": "IMG_2024.mp4",
  "total_size": 22548578304,
  "sha256": "<whole-file sha256>",
  "chunk_size": 1900000000,
  "chunk_count": 12,
  "chunks": [
    {"index": 1, "filename": "IMG_2024.mp4.part001-of-012",
     "offset": 0, "size": 1900000000, "sha256": "…", "tg_message_id": 1234}
  ],
  "source": {
    "mega_path": "/Camera/IMG_2024.mp4",
    "mtime_utc": "2024-06-01T14:23:05+00:00",
    "capture_datetime": "2024-06-01T16:23:05",
    "capture_datetime_source": "exif|filename|fallback|mtime"
  },
  "created_utc": "…",
  "tool": "telegram-photo-vault"
}
```

## Device inventory manifest

The format a client (Termux script, adb script, Android app — none of which
exist in this repo) builds before calling
[`POST /api/devices/{device_id}/reconcile`](#post-apidevicesdevice_idreconcile). One
JSON object per local file:

```json
{
  "relpath": "DCIM/Camera/IMG_20260701_080000.jpg",
  "name": "IMG_20260701_080000.jpg",
  "size": 4213556,
  "mtime": "2026-07-01T08:00:00+00:00",
  "sha256": null
}
```

- `relpath` — path relative to the device's photo root. Round-tripped back in
  responses and findings; never parsed or matched by the server.
- `name` — the filename alone (`relpath`'s basename). This is what is matched
  against the catalog.
- `size` — size in bytes, as an integer.
- `mtime` — the file's local modification time, ISO-8601, **and it must carry
  a UTC offset** (e.g. `+00:00` or `Z`). A naive timestamp with no offset is
  read as UTC today — which biases towards *passing* the catalog-freshness
  gate for a file that may actually be newer than the last scan, the opposite
  of the fail-safe the gate exists for. Always send an offset.
- `sha256` — optional, lowercase hex, the whole-file SHA-256. Omit it or send
  `null` when it hasn't been computed; a present, matching hash is the
  strongest evidence (`HASH` tier) and settles the file regardless of name or
  size.

## vault_merge CLI

Standalone (Python 3 stdlib only — the file can be copied anywhere):

```
python scripts/vault_merge.py <manifest.json> [--parts-dir DIR] [--output PATH] [--keep-going]
```

Verifies every part's size and SHA-256 **before writing anything**, refuses to
overwrite an existing output, concatenates, verifies the whole-file hash
(deleting the output on mismatch), and restores the file's mtime from the
manifest. `--keep-going` reports all bad parts instead of stopping at the
first. Exit code 0 only on a fully verified merge.
