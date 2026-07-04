from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pyrogram import Client

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from app.api.routes import router as api_router
from app.models.database import init_db
from app.services.mega import MegaService
from app.services.recovery import MEDIA_KINDS, RecoveryService
from app.services.sftp import SFTPService
from app.services.telegram import TelegramService
from app.worker import PhotoWorker


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


def _parse_int_or_str(value: str) -> int | str:
    cleaned = value.strip()
    if cleaned.lstrip("-").isdigit():
        return int(cleaned)
    return cleaned


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    telegram_api_id = int(_required_env("TELEGRAM_API_ID"))
    telegram_api_hash = _required_env("TELEGRAM_API_HASH")
    telegram_channel_id = _parse_int_or_str(_required_env("TELEGRAM_CHANNEL_ID"))
    telegram_session_name = os.getenv("TELEGRAM_SESSION_NAME", "telegram_photo_vault")
    telegram_session_string = _optional_env("TELEGRAM_SESSION_STRING")

    pyrogram_kwargs: dict[str, object] = {
        "name": telegram_session_name,
        "api_id": telegram_api_id,
        "api_hash": telegram_api_hash,
        # Auto-sleep on FloodWait errors shorter than this many seconds.
        "sleep_threshold": int(os.getenv("TELEGRAM_SLEEP_THRESHOLD", "60")),
    }
    if telegram_session_string:
        pyrogram_kwargs["session_string"] = telegram_session_string

    telegram_client = Client(**pyrogram_kwargs)
    worker_task: asyncio.Task[None] | None = None

    await telegram_client.start()

    try:
        mega_service = MegaService(target_folder=os.getenv("MEGA_TARGET_FOLDER", "/Camera"))
        telegram_service = TelegramService(
            telegram_client,
            telegram_channel_id,
            upload_delay_seconds=float(os.getenv("TELEGRAM_UPLOAD_DELAY", "5")),
        )

        sftp_key_path = _optional_env("ODROID_KEY_PATH")
        sftp_keys = [sftp_key_path] if sftp_key_path else None
        allow_insecure_host_key = _parse_bool(
            os.getenv("ODROID_ALLOW_INSECURE_HOST_KEY"), default=False
        )
        sftp_service = SFTPService(
            host=_required_env("ODROID_HOST"),
            username=_required_env("ODROID_USERNAME"),
            port=int(os.getenv("ODROID_PORT", "22")),
            password=_optional_env("ODROID_PASSWORD"),
            client_keys=sftp_keys,
            known_hosts=_optional_env("ODROID_KNOWN_HOSTS"),
            allow_insecure_host_key=allow_insecure_host_key,
        )

        worker = PhotoWorker(
            mega_service=mega_service,
            telegram_service=telegram_service,
            sftp_service=sftp_service,
            odroid_remote_dir=os.getenv("ODROID_REMOTE_DIR", "/srv/photo-vault"),
            download_root=os.getenv("WORKER_DOWNLOAD_ROOT", "/data/tmp"),
            compressed_root=os.getenv("WORKER_COMPRESSED_ROOT", "/data/compressed"),
            mode=os.getenv("WORKER_MODE", "interval"),
            run_interval=float(os.getenv("WORKER_RUN_INTERVAL", "900")),
            per_file_delay=float(os.getenv("WORKER_FILE_DELAY", "0")),
            max_retries=int(os.getenv("WORKER_MAX_RETRIES", "3")),
            batch_size=int(os.getenv("WORKER_BATCH_SIZE", "50")),
            chunk_size=int(os.getenv("CHUNK_SIZE", "1900000000")),
            chunk_threshold=int(os.getenv("CHUNK_THRESHOLD", "1950000000")),
        )

        recovery_kinds = tuple(
            kind.strip()
            for kind in os.getenv("RECOVERY_KINDS", ",".join(MEDIA_KINDS)).split(",")
            if kind.strip()
        )
        recovery = RecoveryService(
            telegram_service,
            download_root=os.getenv("RECOVERY_DOWNLOAD_ROOT", "/data/recovery"),
            delay_seconds=float(os.getenv("RECOVERY_DELAY", "5")),
            max_retries=int(os.getenv("RECOVERY_MAX_RETRIES", "3")),
            kinds=recovery_kinds,
            delete_old=_parse_bool(os.getenv("RECOVERY_DELETE_OLD"), default=True),
        )

        worker_task = asyncio.create_task(worker.run_forever(), name="photo-worker")
        app.state.worker = worker
        app.state.worker_task = worker_task
        app.state.recovery = recovery
        app.state.telegram_client = telegram_client

        yield
    finally:
        recovery_service = getattr(app.state, "recovery", None)
        if recovery_service is not None:
            await recovery_service.shutdown()

        if worker_task is not None:
            worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_task

        await telegram_client.stop()


app = FastAPI(title="Telegram Photo Vault", lifespan=lifespan)
app.include_router(api_router)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
