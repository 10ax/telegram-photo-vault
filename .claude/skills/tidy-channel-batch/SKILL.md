---
name: tidy-channel-batch
description: Use when adding date-hashtag captions to media already in the Telegram channel — running a recovery scan, a dry run, or one tidy batch, and reading the batch counters afterwards
---

# Run one channel tidy batch

The tidy adds `#YYYY #MM_YYYY #YYYY_MM_DD` captions to channel messages that
predate this project. It works **in place**: it edits captions and never
deletes or re-uploads media. One API call processes **one batch**, so tidying a
large channel means running this several times.

Only one recovery task runs at a time; a second call while one is in flight
returns `409`.

## Before you start

```bash
export KEY="$(grep '^API_KEY=' .env | cut -d= -f2-)"   # never paste the key into a file
export VAULT=http://localhost:8000
curl -s -H "X-Api-Key: $KEY" $VAULT/api/status | python3 -m json.tool
```

Read two things in that output:

- `recovery.disk.below_floor` — if `true`, EXIF downloads will be deferred
  until there is more room than `RECOVERY_MIN_FREE_GB` (default 10 GiB).
- `recovery.running` — must be `false` before you start anything.

## 1. Scan the channel history (once, then whenever new old media appears)

```bash
curl -s -X POST -H "X-Api-Key: $KEY" $VAULT/api/recovery/scan
```

Walks the whole history into `recovery_items`. It skips this project's own
`.partNNN-of-MMM` / `.manifest.json` messages and anything whose caption
already carries the full hashtag scheme (those land as `SKIPPED`). Re-scanning
is idempotent — existing `tg_message_id`s are not re-inserted.

Watch it finish:

```bash
curl -s -H "X-Api-Key: $KEY" $VAULT/api/status \
  | python3 -c 'import json,sys; s=json.load(sys.stdin)["recovery"]; print(s["running"], s["items"])'
```

## 2. Dry run — see the captions before anything is edited

```bash
curl -s -X POST -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"dry_run": true, "limit": 50}' $VAULT/api/recovery/run
curl -s -H "X-Api-Key: $KEY" "$VAULT/api/recovery/items?status=PLANNED&limit=20" \
  | python3 -m json.tool
```

Each item's `planned_caption` is the date chain's answer: a date in the
filename, else the message post date, else — for image *documents* with no date
in the name — the EXIF read from a temporary download.

## 3. Tidy for real

```bash
curl -s -X POST -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"dry_run": false}' $VAULT/api/recovery/run
```

Then read the batch counters:

```bash
curl -s -H "X-Api-Key: $KEY" $VAULT/api/status \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["recovery"]["batch"])'
```

| counter | meaning |
|---|---|
| `captioned` | caption edited in place (existing free text kept, tags appended) |
| `already` | the message already carried the tags; nothing sent |
| `planned` | dry run only — caption recorded, not applied |
| `skipped` | no date derivable, or the source message is gone |
| `deferred` | an EXIF download would have breached the free-space floor |
| `failed` | errored `RECOVERY_MAX_RETRIES` times |
| `downloaded_bytes` | stops the batch early once past `RECOVERY_BATCH_MAX_DOWNLOAD_GB` |

Repeat step 3 until `total` comes back `0`. A non-zero `deferred` means free
disk space (or lower `RECOVERY_MIN_FREE_GB`) and run again — those items stay
`SCANNED` and are picked up next time.

## Backfilling the browse gallery

Only when `BROWSE_CHANNEL_ID` is set. This copies existing native
photo/video/animation messages into the gallery channel **server-side** — no
download, no re-upload:

```bash
curl -s -X POST -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"limit": 100, "max_video_bytes": 52428800}' $VAULT/api/recovery/backfill
```

It resumes from `recovery_items.browse_tg_message_id`, so re-running never
duplicates. Documents are deliberately excluded — they render as files, not a
photo grid.

## Checking your change locally

Anything you alter in `app/services/recovery.py` is covered by:

```bash
source .venv/bin/activate
pytest tests/test_recovery_flow.py tests/test_recovery_rules.py tests/test_browse_backfill.py -q
```

`tests/test_recovery_rules.py` pins the rules (what counts as a vault artifact,
what needs EXIF, how captions merge); `tests/test_recovery_flow.py` drives the
whole flow against `FakeClient`. Neither touches the network.
