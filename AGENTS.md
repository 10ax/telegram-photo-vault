# Telegram Photo Vault: Agent Guide

## Scope
This repository implements an async pipeline:
1. Discover files on MEGA
2. Download locally
3. Upload original to Telegram
4. Compress to WebP
5. Upload WebP to Odroid via SFTP
6. Delete remote source from MEGA and local temp files

Core stack: FastAPI, SQLAlchemy 2.x async, SQLite, Pyrogram, Pillow, asyncssh.

## Project Layout
- `app/main.py`: FastAPI app + lifespan bootstrap (DB init + worker startup)
- `app/worker.py`: state machine loop and retry logic
- `app/models/database.py`: async DB setup + `Photo` model
- `app/services/`: MEGA, Telegram, image compression, SFTP services
- `app/api/routes.py`: `/api/status` and `/api/system`
- `Dockerfile`, `docker-compose.yml`: containerized runtime

## Required Environment Variables
- `TELEGRAM_API_ID`
- `TELEGRAM_API_HASH`
- `TELEGRAM_CHANNEL_ID`
- `ODROID_HOST`
- `ODROID_USERNAME`
- `ODROID_KNOWN_HOSTS` (required unless insecure mode explicitly enabled)
- `API_KEY` (required to access `/api/*`, passed as `X-Api-Key`)
- `MEGA_EMAIL` + `MEGA_PASSWORD` OR an already-authenticated mounted MEGAcmd session

## Optional Environment Variables
- `DATABASE_URL` (default: `sqlite+aiosqlite:///./data/telegram_photo_vault.db`)
- `MEGA_TARGET_FOLDER` (default: `/Camera`)
- `TELEGRAM_SESSION_NAME` (default: `telegram_photo_vault`)
- `TELEGRAM_SESSION_STRING`
- `TELEGRAM_UPLOAD_DELAY` (default: `5`)
- `ODROID_PORT` (default: `22`)
- `ODROID_PASSWORD`
- `ODROID_KEY_PATH`
- `ODROID_REMOTE_DIR` (default: `/srv/photo-vault`)
- `ODROID_ALLOW_INSECURE_HOST_KEY` (default: `false`; test-only)
- `WORKER_POLL_DELAY` (default: `5`)
- `WORKER_FILE_DELAY` (default: `0`)
- `WORKER_MAX_RETRIES` (default: `3`)
- `WORKER_BATCH_SIZE` (default: `50`)

## Local Run
1. Install dependencies:
   - `pip install -r requirements.txt`
2. Ensure MEGAcmd is installed and authenticated (`mega-whoami` must succeed).
3. Export env vars.
4. Start API:
   - `uvicorn app.main:app --host 0.0.0.0 --port 8000`

## Docker Run
1. Create `.env` with required vars.
2. Start:
   - `docker compose up --build -d`
3. API endpoints:
   - `GET /health`
   - `GET /api/status`
   - `GET /api/system`

## Agent Notes
- Keep all I/O async-safe.
- Preserve the `PhotoStatus` transition order in `app/worker.py`.
- On failure, increment `retry_count`, write traceback to `error_log`, set `FAILED` at max retries.
- Discovery must run before processing to ingest unseen MEGA files into DB as `PENDING`.
