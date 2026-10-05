"""One-shot, read-only catalog scan — no worker, no MEGA, no HTTP.

The server's ``POST /api/catalog/scan`` does exactly what this does
(``scan_all`` + ``match_all``), but starting the whole app also starts the
worker, whose first ``run_forever`` pass begins draining the MEGA backlog. This
tool exists so a scan can be run without that side effect.

It creates the schema if needed (``init_db``) and writes ``catalog_items``; it
never sends, edits, copies or deletes a Telegram message and never touches MEGA.

    set -a; . ./.env; set +a
    .venv/bin/python -m scripts.catalog_scan
    .venv/bin/python -m scripts.catalog_scan --resolve-manifests 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from pyrogram import Client

from app.models.database import ChannelRole, init_db
from app.services.catalog import CatalogService, ChannelSpec, channel_spec_or_none
from app.services.reconcile import ReconcileService

logger = logging.getLogger("catalog_scan")


def _int_or_str(value: str) -> int | str:
    text = value.strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return text


def _channels() -> list[ChannelSpec]:
    """The same channel set `lifespan` builds, by role."""
    specs: list[ChannelSpec] = []
    archive = os.getenv("TELEGRAM_CHANNEL_ID")
    if archive:
        spec = channel_spec_or_none("TELEGRAM_CHANNEL_ID", _int_or_str(archive), ChannelRole.ARCHIVE)
        if spec is not None:
            specs.append(spec)
    browse = os.getenv("BROWSE_CHANNEL_ID")
    if browse:
        spec = channel_spec_or_none("BROWSE_CHANNEL_ID", _int_or_str(browse), ChannelRole.MIRROR)
        if spec is not None:
            specs.append(spec)
    iphone = os.getenv("IPHONE_CHANNEL_ID")
    if iphone:
        spec = channel_spec_or_none("IPHONE_CHANNEL_ID", _int_or_str(iphone), ChannelRole.ARCHIVE)
        if spec is not None:
            specs.append(spec)
    return specs


def _role_by_channel(specs: list[ChannelSpec]) -> dict[str, str]:
    return {str(spec.channel_id): spec.role.value.lower() for spec in specs}


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(f"{name} is not set; load the app's environment first.")
    return value


async def run(args: argparse.Namespace) -> dict[str, object]:
    channels = _channels()
    if not channels:
        sys.exit("No channel is configured (TELEGRAM_CHANNEL_ID/BROWSE_CHANNEL_ID/IPHONE_CHANNEL_ID).")

    await init_db()

    client = Client(
        os.getenv("TELEGRAM_SESSION_NAME", "telegram_photo_vault"),
        api_id=int(_require("TELEGRAM_API_ID")),
        api_hash=_require("TELEGRAM_API_HASH"),
        session_string=os.getenv("TELEGRAM_SESSION_STRING") or None,
        sleep_threshold=int(os.getenv("TELEGRAM_SLEEP_THRESHOLD", "60")),
    )

    archive_spec = next((s for s in channels if s.role is ChannelRole.ARCHIVE), None)
    service = CatalogService(
        client,
        channels,
        scan_delay_seconds=float(os.getenv("CATALOG_SCAN_DELAY", "2")),
        worker_channel_id=archive_spec.channel_id if archive_spec else None,
        backup_state_db=os.getenv("BACKUP_STATE_DB") or None,
    )

    await client.start()
    try:
        scanned = await service.scan_all()
        matched = await service.match_all()
        manifests = (
            await service.resolve_manifests(limit=args.resolve_manifests)
            if args.resolve_manifests
            else None
        )
    finally:
        await client.stop()

    reconcile = ReconcileService(
        archive_channel_ids=[s.channel_id for s in channels if s.role is ChannelRole.ARCHIVE]
    )
    freshness = await reconcile.catalog_freshness()

    roles = _role_by_channel(channels)
    return {
        "scanned": {roles.get(cid, cid): counts for cid, counts in scanned.items()},
        "matched": matched,
        "resolved_manifests": manifests,
        "archive_rows": freshness["archive_rows"],
        "frontier": freshness["frontier"],
        "channels": [
            {
                "role": roles.get(str(entry["channel_id"]), "unknown"),
                "rows": entry["rows"],
                "last_scanned_at": entry["last_scanned_at"],
                "newest_message_date": entry["newest_message_date"],
            }
            for entry in freshness["channels"]
        ],
    }


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--resolve-manifests",
        type=int,
        default=0,
        metavar="N",
        help="also read up to N chunked-file manifests (read-only, small fetches)",
    )
    args = parser.parse_args(argv)

    result = asyncio.run(run(args))
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
