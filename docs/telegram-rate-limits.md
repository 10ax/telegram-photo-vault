# Telegram rate limits and the vault's pace

Date: 2026-10-05. Evidence note, not a design change. Every number below is
either measured from this account or read from the client library in this repo's
`.venv`; nothing is inferred from the Bot API.

## There is no published number for an MTProto account

Telegram publishes limits for the **Bot API** (≈30 messages/s, 1/s per chat),
and those are what search results return. This repo uses an **MTProto user
account** through `kurigram` (`pyrogram` namespace), where the limit is
adaptive and enforced by raising `FLOOD_WAIT_X` / `FLOOD_PREMIUM_WAIT_X`. The
only meaningful statement of "the rate limit" is what this account has been
observed to tolerate. The observed envelope is below.

## What this account actually tolerated

Source: `local-data/backup-run.log`, the completed iPhone migration of
6,916 files / 105.1 GB on 2026-07-30 → 07-31, on a **non-Premium** account.

| Metric | Value |
|---|---|
| Files / bytes | 6,916 files, 105.1 GB |
| Wall time | 21 h 33 m (19:45:13 → 17:18:21) |
| Sustained throughput | **5.4 files/min · 4.9 GB/h (≈1.35 MB/s)** |
| Clean burst (small files, 5 s delay) | **≈10 files/min** with zero floods |
| `FLOOD_PREMIUM_WAIT_X` (11–15 s) | **18,012** — on `upload.SaveBigFilePart` and `upload.GetFile` |
| `FLOOD_WAIT_X` (923 / 1190 / 1488 / 1759 s) | **4** — all on `messages.SendMedia` (15–29 min) |
| `upload.GetFile` timeouts | 2 |

Two distinct tiers are visible:

1. **Premium throttle.** 11–15 s waits that recur continuously under sustained
   media transfer on a non-Premium account. The error text says it outright:
   *"…or purchase a Telegram Premium subscription to remove this rate limit."*
   This is the day-to-day ceiling for this vault.
2. **Hard account flood.** Rare, large `FLOOD_WAIT` on `messages.SendMedia`
   (15–29 min). This is what forces a run to stop and resume later; it does not
   indicate anything wrong with the file being sent.

## How the client handles it today

**Client-level auto-sleep.** `Client(sleep_threshold=…)` (repo:
`TELEGRAM_SLEEP_THRESHOLD`, default 60) makes `Session.invoke` sleep inline on
`FloodWait` **and** `FloodPremiumWait` whose value is at or below the
threshold, and re-raise anything larger
(`pyrogram/session/session.py:552-565`). The 11–15 s premium tier is therefore
absorbed invisibly; only the rare >60 s floods propagate.

**Two weaknesses worth knowing about:**

- **`FloodPremiumWait` is a sibling of `FloodWait`, not a subclass**
  (`pyrogram/errors/exceptions/flood_420.py`: both extend `Flood`). So
  `except FloodWait` does not catch it. `app/services/recovery.py` catches
  `FloodWait` only; a >60 s premium wait would fall through to the generic
  handler and cost a retry.
- **`app/worker.py` has no flood handling at all.** A >60 s wait raised from
  `send_document` hits the generic `except Exception`, increments
  `retry_count`, and can mark the photo `FAILED` — blaming the file for an
  account-wide throttle. Not data loss, but a false failure and a manual retry.
- **kurigram's part worker uses `sleep_threshold=10`, not the configured 60.**
  `save_file`'s internal worker calls `session.invoke(data)` with the default
  (`methods/advanced/save_file.py`), which is why 11–15 s part-level floods
  surfaced as ~18k `ERROR` tracebacks while ≤10 s ones slept quietly. This is
  library-internal and a latent truncation risk for multi-part >2 GB uploads;
  the whole-file SHA-256 check is what catches it.

There is also **no shared cooldown**: the worker, recovery and catalog share one
account and one `Client`, but pace themselves independently, so concurrent
subsystems stack toward the same per-account limit.

## Recommended pace

- Never exceed **~1 media transfer per 6 s sustained** (~10/min) — the observed
  clean ceiling with a 5 s delay and small files.
- Anything that reaches `upload.SaveBigFilePart` (files >10 MB) should space
  files by **≥10 s**; 5 s produced the entire 18k premium-wait count.
- On any flood, sleep `value + jitter`, and prefer pausing *all* subsystems,
  because the limit is per account rather than per method.
- History scans (`messages.GetHistory`) are cheap — the full 18k-message archive
  walked in minutes — but not flood-free: a live scan on 2026-10-05 hit 3–9 s
  `GetHistory` waits, auto-slept by the client below the 60 s threshold. Keep
  `CATALOG_SCAN_DELAY` at 2.

Current configuration after the 2026-10-05 change:

| knob | value | why |
|---|---|---|
| `TELEGRAM_UPLOAD_DELAY` | 10 | <10 s floods on `SaveBigFilePart` at 5 s |
| `RECOVERY_DELAY` | 8 | media items in a recovery/backfill batch |
| `CATALOG_SCAN_DELAY` | 2 | history pages are cheap; keep defensive |
| `TELEGRAM_SLEEP_THRESHOLD` | 60 | ≤60 s floods sleep inline; larger ones surface so a run can stop |

Raising `TELEGRAM_SLEEP_THRESHOLD` would let background tasks sleep through the
rare 15–29 min floods instead of failing, but `POST /api/vault/verify` calls the
fingerprint synchronously inside an HTTP request, so a high threshold would let
one request hang for half an hour. Keep it moderate until verify is moved off the
request path.

## Live probe

`scripts/probe_rate_limit.py` measures the current envelope with **read-only**
partial `upload.GetFile` reads (the same operation enrichment and backfill use).
It never sends, edits, copies or deletes. See `--help`; results from the
2026-10-05 run are in the table below.

<!-- probe:results:begin -->
Run 2026-10-05 against the archive channel, read-only partial `upload.GetFile`
reads (`sleep_threshold=0` so every wait surfaces rather than being slept):

| run | args | requests | floods | max wait | throughput | p95 latency |
|---|---|---|---|---|---|---|
| A | `--delay 0` | 60 | 0 | — | 336/min | 0.22 s |
| B | `--delay 0` | 300 | 0 | — | 379/min | 0.22 s |

**Conclusion:** partial 1 MiB reads — enrichment, fingerprinting, `vault/verify`,
`resolve-manifests` — are **not** throttled at hundreds per minute. The waits in
the migration log came from *sustained full-file transfer*:
`upload.SaveBigFilePart` (uploads) and full-file `upload.GetFile` (the verify
re-downloads), plus the rare `messages.SendMedia` hard flood. So the pace that
has to be conservative is the full-file one; metadata and partial reads can run
fast.

Not measured here, and why: **uploads** (the probe is read-only by design, and a
throwaway upload would write to the channel) — the migration log is the evidence
for those, and that is what `TELEGRAM_UPLOAD_DELAY=10` addresses; and
**sustained full-file download**, which only reveals its throttle over minutes,
so the backfill should start `--limit`ed and watched exactly as the catalog spec
says.

The probe exercises `upload.GetFile`. A full catalog scan of the same day
exercised `messages.GetHistory` on 18,419 archive messages and did see small
waits (9 s, 7 s, 4 s, 3 s), all auto-slept by the client. So: metadata reads
(`GetFile` partial) are effectively free; history pagination is cheap but not
free.
<!-- probe:results:end -->
