# Telegram Photo Vault: Agent Guide

## Scope
This repository implements an async pipeline:
1. Discover files on MEGA (scheduled or on demand)
2. Download locally
3. Upload original to Telegram (files > 2 GB are split into chunks + manifest)
4. Compress images to WebP (videos skip this)
5. Upload WebP to Odroid via SFTP (videos skip this)
6. Delete remote source from MEGA and local temp files

Plus a **channel recovery / tidy** subsystem that scans existing channel
history and adds date-hashtag captions to media. It works *in place* (edits the
caption, keeps the media) using the filename or post date, and only downloads
image documents whose filename lacks a date so their EXIF can be read. Work runs
in bounded, resumable batches guarded by a free-space floor (see `PLAN.md` and
`docs/video-chunking-design.md`).

Core stack: FastAPI, SQLAlchemy 2.x async, SQLite, kurigram (maintained
Pyrogram fork, same `pyrogram` namespace), Pillow (+pillow-heif), asyncssh.

## Project Layout
- `app/main.py`: FastAPI app + lifespan bootstrap (DB init, worker + recovery wiring)
- `app/worker.py`: state machine loop, scheduling, chunked-upload steps
- `app/models/database.py`: async DB setup, `Photo`/`UploadChunk`/`RecoveryItem`
  models, additive column migrations
- `app/services/`: MEGA, Telegram, image compression, SFTP, media-type
  detection, chunking, channel recovery
- `app/api/routes.py`: `/api/*` endpoints (auth: `X-Api-Key`)
- `app/static/dashboard.html`: dashboard served at `/`
- `scripts/vault_merge.py`: standalone (stdlib-only) chunk verify+merge CLI
- `tests/`: pytest suite (pure functions + functional flows with fakes)
- `Dockerfile`, `docker-compose.yml`: containerized runtime
- `deploy/teldrive/`: optional self-hosted teldrive "family drive" (Telegram-backed
  shared cloud drive: compose + Postgres + DB-backup + iPhone-folder seed script)

## State machines
- Photo: `PENDING → DOWNLOADED → [CHUNK_UPLOADING →] TG_UPLOADED → COMPRESSED →
  ODROID_UPLOADED → COMPLETED`; `FAILED` (records `failed_status` for retry);
  `SKIPPED` for unsupported types. Videos jump `TG_UPLOADED → finalize`.
  Files larger than `CHUNK_THRESHOLD` go through `CHUNK_UPLOADING` (one chunk
  per worker visit; JSON manifest uploaded last as the commit marker).
- RecoveryItem (hybrid in-place tidy): `SCANNED → [PLANNED (dry-run)] →
  COMPLETED`. A run derives the date caption from the filename → post date (no
  download), or downloads an image document to read EXIF only when its filename
  has no date (disk-guarded), then edits the caption in place (existing free-text
  is preserved). `SKIPPED` (already tidy / message gone / no date derivable),
  `FAILED`. Items are deferred (left `SCANNED`) when a download would breach the
  free-space floor. `DOWNLOADED`/`REUPLOADED`/`DUPLICATE` are legacy states no
  longer produced by the in-place flow.

## API
- `GET /` dashboard (static, no key; calls the API with a stored key)
- `GET /health`
- `GET /api/status` — photo counts + worker state + recovery state
- `POST /api/run` — trigger a worker run now
- `GET /api/photos?status=&limit=&offset=` / `POST /api/photos/{id}/retry`
- `POST /api/recovery/scan`, `POST /api/recovery/run`
  (`{"dry_run": true|false, "limit"?, "max_download_bytes"?}`, dry_run default
  true; each call processes one batch), `GET /api/recovery/items?status=`
- `POST /api/recovery/backfill` (`{"limit"?, "max_video_bytes"?}`) — copy existing
  native photo/video/animation messages into `BROWSE_CHANNEL_ID` as a gallery,
  server-side (no download), resumable via `recovery_items.browse_tg_message_id`
- `GET /api/system` — disk usage

## Required Environment Variables
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_CHANNEL_ID`
- `ODROID_HOST`, `ODROID_USERNAME`
- `ODROID_KNOWN_HOSTS` (required unless insecure mode explicitly enabled)
- `API_KEY` (required to access `/api/*`, passed as `X-Api-Key`)
- `MEGA_EMAIL` + `MEGA_PASSWORD` OR an already-authenticated mounted MEGAcmd session

## Optional Environment Variables
- `DATABASE_URL` (default: `sqlite+aiosqlite:///./data/telegram_photo_vault.db`)
- `LOG_LEVEL` (default: `INFO`)
- `MEGA_TARGET_FOLDER` (default: `/Camera`)
- `TELEGRAM_SESSION_NAME` (default: `telegram_photo_vault`)
- `TELEGRAM_SESSION_STRING`
- `TELEGRAM_UPLOAD_DELAY` (default: `5`)
- `TELEGRAM_SLEEP_THRESHOLD` (default: `60`; auto-sleep on FloodWait below this)
- `BROWSE_CHANNEL_ID` (optional; a second channel that both people join. When set,
  the worker mirrors a native, date-captioned photo/video there for gallery-style
  scrolling + `#YYYY_MM_DD` search inside the Telegram app. Best-effort — a mirror
  failure never breaks the archival pipeline. The main channel keeps the archival
  documents/chunks/manifests.)
- `BROWSE_MAX_VIDEO_MB` (default: `0` = photos only; videos up to this size are
  also mirrored, larger videos stay archival-only)
- `ODROID_PORT` (default: `22`), `ODROID_PASSWORD`, `ODROID_KEY_PATH`
- `ODROID_REMOTE_DIR` (default: `/srv/photo-vault`)
- `ODROID_ALLOW_INSECURE_HOST_KEY` (default: `false`; test-only)
- `WORKER_MODE` (`interval` | `manual`, default: `interval`)
- `WORKER_RUN_INTERVAL` (seconds between scheduled runs, default: `900`)
- `WORKER_FILE_DELAY` (default: `0`), `WORKER_MAX_RETRIES` (default: `3`),
  `WORKER_BATCH_SIZE` (default: `50`)
- `WORKER_DOWNLOAD_ROOT` (default: `/data/tmp`),
  `WORKER_COMPRESSED_ROOT` (default: `/data/compressed`)
- `CHUNK_SIZE` (default: `1900000000`), `CHUNK_THRESHOLD` (default: `1950000000`;
  raise both only on a Premium account — standard accounts cap at 2 GB)
- `RECOVERY_DOWNLOAD_ROOT` (default: `/data/recovery`; scratch for EXIF-only
  downloads, cleaned per item)
- `RECOVERY_DELAY` (default: `5`; seconds between items in a batch),
  `RECOVERY_MAX_RETRIES` (default: `3`)
- `RECOVERY_KINDS` (default: `photo,video,document,animation`)
- `RECOVERY_BATCH_SIZE` (default: `300`; items processed per run)
- `RECOVERY_MIN_FREE_GB` (default: `10`; free-space floor — an item is deferred
  rather than downloaded if fetching it would drop below this)
- `RECOVERY_BATCH_MAX_DOWNLOAD_GB` (default: `5`; a batch stops early once it has
  downloaded this many GB of EXIF-only files)
- `RECOVERY_DELETE_OLD` (default: `true`; legacy — the in-place tidy never
  deletes originals, so this is currently informational only)
- `IPHONE_CHANNEL_ID` (default: unset; third channel to catalogue, created by
  `scripts/backup_local_folder.py`. Its id is in the `meta` table of that
  script's state DB. Unset means the channel is skipped.)
- `CATALOG_SCAN_DELAY` (default: `2`; seconds between channels during a full
  catalog scan)
- `RECONCILE_MAX_ENTRIES` (default: `10000`; inventory entries accepted per
  `POST /api/devices/{device_id}/reconcile` call. More is refused with `413` and a
  message telling the client to continue against the same `snapshot_id`.)
- `RECONCILE_FINGERPRINT_BYTES` (default: `262144`; bytes hashed at each end
  of a file when `POST /api/vault/verify` settles an ambiguous match. Telegram
  streams in 1 MiB chunks, so the traffic cost is 2 MiB regardless, and a value
  above 1 MiB is clamped to it. `GET /api/catalog/freshness` publishes the
  effective value as `fingerprint_window_bytes`, which is the window a client
  has to hash locally for the two sides to agree.)
- `BACKUP_STATE_DB` (default: unset; path to `scripts/backup_local_folder.py`'s
  own state DB. When set, a catalog scan also attributes rows to that script,
  using the channel recorded in that DB's `meta` table — never a bare message
  id, which means nothing outside its own channel.)

## Local Run
1. `pip install -r requirements.txt`
2. Ensure MEGAcmd is installed and authenticated (`mega-whoami` must succeed).
3. Export env vars.
4. `uvicorn app.main:app --host 0.0.0.0 --port 8000`

## Tests
- `pip install -r requirements-dev.txt`
- `pytest -q` (no network, no real Telegram/MEGA — flows are tested with fakes)
- CI runs compileall + pytest on Python 3.11 (`.github/workflows/ci.yml`)

## Docker Run
1. Create `.env` with required vars.
2. `docker compose up --build -d`
3. Dashboard: `http://host:8000/`

## Agent Notes
- Keep all I/O async-safe; SQLAlchemy models with server-side `onupdate` expire
  `updated_at` on commit — `await session.refresh(...)` before serializing a
  committed row.
- Preserve the `PhotoStatus` transition order in `app/worker.py`; one chunk
  upload per worker visit is intentional (fairness + crash granularity).
- On failure, increment `retry_count`, write traceback to `error_log`, set
  `FAILED` (+ `failed_status`) at max retries.
- Discovery must run before processing to ingest unseen MEGA files into DB as
  `PENDING` (or `SKIPPED` for unsupported types).
- The channel must stay self-describing: chunk naming, captions, and the
  trailing manifest are load-bearing (see `docs/video-chunking-design.md`);
  a plain `LC_ALL=C cat name.part* > name` merge must always remain valid.
