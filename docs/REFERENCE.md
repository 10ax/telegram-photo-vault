# Reference

Technical reference for Telegram Photo Vault. For setup and workflows, see the
[README](../README.md).

- [Environment variables](#environment-variables)
- [HTTP API](#http-api)
- [State machines](#state-machines)
- [Database schema](#database-schema)
- [Chunked-file formats](#chunked-file-formats)
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
| `TELEGRAM_UPLOAD_DELAY` | `5` | Seconds slept after every upload |
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
| `RECOVERY_DELAY` | `5` | Seconds slept between recovery items |
| `RECOVERY_MAX_RETRIES` | `3` | Failures before a recovery item is `FAILED` |
| `RECOVERY_KINDS` | `photo,video,document,animation` | Message media kinds ingested by the scan |
| `RECOVERY_DELETE_OLD` | `true` | Delete originals after the tidy replacement is confirmed |
| `ODROID_PORT` | `22` | |
| `ODROID_PASSWORD` / `ODROID_KEY_PATH` | – | One of the two |
| `ODROID_REMOTE_DIR` | `/srv/photo-vault` | |
| `ODROID_ALLOW_INSECURE_HOST_KEY` | `false` | Test-only: skips host-key verification |
| `DATA_VOLUME_PATH` | `/data` | Disk reported by `/api/system` |

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
