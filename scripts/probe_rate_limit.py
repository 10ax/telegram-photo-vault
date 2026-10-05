"""Measure this account's current Telegram media-transfer throttle, read-only.

Telegram does not publish an MTProto account rate limit; it is enforced as
``FLOOD_WAIT`` / ``FLOOD_PREMIUM_WAIT`` and it changes. This probe issues
partial ``upload.GetFile`` reads — the same operation enrichment and backfill
use — and reports where waits appear, so a batch size and delay can be chosen
from evidence rather than guesswork. See ``docs/telegram-rate-limits.md``.

**It never sends, edits, copies or deletes anything.** It is bounded three
ways: a maximum number of requests, a wall-clock cap, and an abort on the first
wait at or above ``--abort-wait``.

Run from the repo root with the app's environment loaded, e.g.::

    set -a; . ./.env; set +a
    .venv/bin/python -m scripts.probe_rate_limit --requests 60 --delay 0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time

from pyrogram import Client
from pyrogram.errors import FloodPremiumWait, FloodWait

# Client(sleep_threshold=0) makes session.invoke re-raise every flood instead
# of sleeping it, so the probe can see the wait value it is being asked for.
NO_AUTO_SLEEP = 0


def _percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile; None for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(pct / 100 * len(ordered)) - 1))
    return ordered[rank]


def summarize(
    records: list[dict[str, object]], *, elapsed_seconds: float
) -> dict[str, object]:
    """Reduce per-request records to the handful of numbers a pace decision needs.

    A record is ``{"latency_s": float, "flood_seconds": int | None}``. Latency
    percentiles are over the reads that succeeded: a flood's "latency" is the
    wait Telegram demanded, not a transfer time, and mixing the two would hide
    the very number being measured.
    """
    floods = [int(r["flood_seconds"]) for r in records if r.get("flood_seconds")]
    ok = [float(r["latency_s"]) for r in records if not r.get("flood_seconds")]
    return {
        "requests": len(records),
        "floods": len(floods),
        "flood_waits": sorted(floods),
        "max_flood_seconds": max(floods) if floods else 0,
        "latency_p50_s": _percentile(ok, 50),
        "latency_p95_s": _percentile(ok, 95),
        "per_minute": (len(records) / elapsed_seconds * 60) if elapsed_seconds > 0 else 0.0,
    }


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(f"{name} is not set; load the app's environment first.")
    return value


def parse_channel_id(text: str) -> int | str:
    """A numeric ``-100…`` id becomes an int; anything else stays a string.

    Passed as a string, pyrogram resolves a bare number through
    ``contacts.ResolvePhone`` and dies with ``PHONE_NOT_OCCUPIED`` before the
    first read. Same rule as ``_parse_int_or_str`` in ``app/main.py``.
    """
    stripped = text.strip()
    if stripped.lstrip("-").isdigit():
        return int(stripped)
    return stripped


async def _collect_message_ids(client: Client, channel, sample: int) -> list[int]:
    """Ids of the newest document/video messages, the kinds worth reading."""
    ids: list[int] = []
    async for message in client.get_chat_history(channel):
        media = getattr(message, "document", None) or getattr(message, "video", None)
        if media is not None:
            ids.append(message.id)
            if len(ids) >= sample:
                break
    return ids


async def probe(args: argparse.Namespace) -> dict[str, object]:
    client = Client(
        os.getenv("TELEGRAM_SESSION_NAME", "telegram_photo_vault"),
        api_id=int(_require("TELEGRAM_API_ID")),
        api_hash=_require("TELEGRAM_API_HASH"),
        session_string=os.getenv("TELEGRAM_SESSION_STRING") or None,
        sleep_threshold=NO_AUTO_SLEEP,
    )

    await client.start()
    try:
        channel = parse_channel_id(args.channel or _require("TELEGRAM_CHANNEL_ID"))
        ids = await _collect_message_ids(client, channel, args.sample)
        if not ids:
            sys.exit("No document/video messages found to read.")

        records: list[dict[str, object]] = []
        errors: list[str] = []
        aborted: str | None = None
        start = time.monotonic()

        for index in range(args.requests):
            if time.monotonic() - start >= args.max_seconds:
                aborted = f"time cap {args.max_seconds:.0f}s reached"
                break

            message_id = ids[index % len(ids)]
            began = time.monotonic()
            flood: int | None = None
            try:
                message = await client.get_messages(channel, message_id)
                async for _part in client.stream_media(message, offset=0, limit=1):
                    break
            except (FloodWait, FloodPremiumWait) as exc:
                flood = int(getattr(exc, "value", 0) or 0)
            except Exception as exc:  # noqa: BLE001 - one bad message must not stop the run
                errors.append(type(exc).__name__)

            latency = time.monotonic() - began
            records.append({"latency_s": round(latency, 3), "flood_seconds": flood})
            print(
                f"[{index + 1}/{args.requests}] latency={latency:6.2f}s "
                f"flood={flood if flood else '-'}",
                flush=True,
            )

            if flood:
                if flood >= args.abort_wait:
                    aborted = f"flood of {flood}s reached abort-wait {args.abort_wait}s"
                    break
                await asyncio.sleep(flood + 1)
            elif args.delay > 0:
                await asyncio.sleep(args.delay)

        elapsed = time.monotonic() - start
    finally:
        await client.stop()

    result = summarize(records, elapsed_seconds=elapsed)
    result.update(
        {
            "elapsed_s": round(elapsed, 1),
            "delay_s": args.delay,
            "errors": errors,
            "aborted": aborted,
        }
    )
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--channel", help="channel id; defaults to TELEGRAM_CHANNEL_ID")
    parser.add_argument("--sample", type=int, default=40, help="messages to read from")
    parser.add_argument("--requests", type=int, default=60, help="hard cap on reads")
    parser.add_argument(
        "--delay", type=float, default=0.0, help="seconds between reads (0 = as fast as possible)"
    )
    parser.add_argument(
        "--max-seconds", type=float, default=480.0, help="wall-clock cap on the whole probe"
    )
    parser.add_argument(
        "--abort-wait",
        type=float,
        default=60.0,
        help="stop on the first flood this long or longer",
    )
    args = parser.parse_args(argv)

    if args.requests > 500:
        sys.exit("Refusing more than 500 reads: this is a probe, not a load test.")

    result = asyncio.run(probe(args))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
