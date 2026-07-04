# Telegram Photo Vault: Agent Guide

## Scope
This repository implements an async pipeline:
1. Discover files on MEGA (scheduled or on demand)
2. Download locally
3. Upload original to Telegram (files > 2 GB are split into chunks + manifest)
4. Compress images to WebP (videos skip this)
5. Upload WebP to Odroid via SFTP (videos skip this)
6. Delete remote source from MEGA and local temp files

Plus a **channel recovery** subsystem that scans existing channel history and
re-uploads media as tidy captioned documents (see `PLAN.md` and
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

## State machines
- Photo: `PENDING → DOWNLOADED → [CHUNK_UPLOADING →] TG_UPLOADED → COMPRESSED →
  ODROID_UPLOADED → COMPLETED`; `FAILED` (records `failed_status` for retry);
  `SKIPPED` for unsupported types. Videos jump `TG_UPLOADED → finalize`.
  Files larger than `CHUNK_THRESHOLD` go through `CHUNK_UPLOADING` (one chunk
  per worker visit; JSON manifest uploaded last as the commit marker).
- RecoveryItem: `SCANNED → DOWNLOADED → PLANNED (dry-run) → REUPLOADED →
  COMPLETED`; `SKIPPED` (already tidy / message gone), `DUPLICATE` (same
  SHA-256, never deleted), `FAILED`.

## API
- `GET /` dashboard (static, no key; calls the API with a stored key)
- `GET /health`
- `GET /api/status` — photo counts + worker state + recovery state
- `POST /api/run` — trigger a worker run now
- `GET /api/photos?status=&limit=&offset=` / `POST /api/photos/{id}/retry`
- `POST /api/recovery/scan`, `POST /api/recovery/run` (`{"dry_run": true|false}`,
  default true), `GET /api/recovery/items?status=`
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
- `RECOVERY_DOWNLOAD_ROOT` (default: `/data/recovery`)
- `RECOVERY_DELAY` (default: `5`), `RECOVERY_MAX_RETRIES` (default: `3`)
- `RECOVERY_KINDS` (default: `photo,video,document,animation`)
- `RECOVERY_DELETE_OLD` (default: `true`; originals deleted only after the
  tidy replacement is confirmed)

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
