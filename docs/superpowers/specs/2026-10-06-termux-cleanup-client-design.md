# Termux cleanup client: free phone storage from the archive

Date: 2026-10-06. The client half of the device-reconciliation effort. It depends
on the server side specified in `2026-09-24-device-reconciliation-design.md` and
in `docs/REFERENCE.md`; it changes no server behaviour.

## Goal

Let the owner free space on the phone by deleting local photos and videos that
the archive already holds, and **only** those. The server already answers "are
these bytes in the archive?" with a per-file verdict; this client asks that
question on the device, shows what is safe, and — after a single confirmation —
deletes it and records the deletion.

## Context: what exists, what is missing

The server emits verdicts and never deletes. `POST
/api/devices/{device_id}/reconcile` takes an inventory manifest and returns, per
entry, `ARCHIVED` / `IN_FLIGHT` / `AMBIGUOUS` / `NOT_ARCHIVED` with a match tier
and the `channel_id` + `tg_message_id` that hold the bytes. `POST
/api/vault/verify` settles an ambiguous entry cheaply, and `POST
/api/devices/{device_id}/deletions` records what a client removed. There is no
client — deliberately, so the transport stayed replaceable. This is that client.

## Environment facts

- **Android 17** (API 37), Pixel, **Termux from the GitHub build**, with
  **"All files access" (`MANAGE_EXTERNAL_STORAGE`) granted**. `termux-setup-storage`
  alone grants read; write/delete on shared storage needs All-files access, which
  the GitHub build declares and the Play policy (which Termux is not subject to)
  does not block.
- **Connectivity: Tailscale preferred, LAN as fallback.** Android 17 gates LAN
  access behind the `ACCESS_LOCAL_NETWORK` runtime permission for apps targeting
  API 37+; Termux may be affected (termux/termux-app#5165). The Tailscale address
  is a VPN interface, not the local network, and is exempt — so the client
  defaults to the Tailscale name.
- **No Telegram traffic from the phone.** The client speaks only to the vault
  HTTP API, over the WireGuard-encrypted tailnet, with the `X-Api-Key` header.

## Non-goals

- **No delete-by-default.** The client is review-then-delete, one bulk
  confirmation; `--dry-run` never deletes.
- **No upload or ingest.** It never sends media anywhere; it only reports and
  deletes.
- **No RAW/DNG support.** Those were never archived, so they can only ever be
  `NOT_ARCHIVED`; scanning them is pure cost. Skipped.
- **No server change, no new endpoint.** The contract is already published.
- **No gallery/UI.** Immich remains the browser; this is a CLI.
- **No iOS, no non-media files.**

## Architecture

A stdlib-only Python package at the repo root, `vault_client/`, run as
`python -m vault_client` after `git clone`/`git pull` in Termux. Stdlib only, so
the phone needs nothing beyond `pkg install python`.

| module | responsibility |
|---|---|
| `config.py` | resolve settings from flags > `~/.config/vault-client/env` > defaults: server URL, `device_id`, API key, roots, options |
| `enumerate.py` | walk the roots and emit manifest entries (pure helpers: extension filter, relpath, mtime formatting) |
| `api.py` | HTTP client for `freshness`, `reconcile`, `verify`, `deletions` |
| `pipeline.py` | orchestrate one run end to end |
| `report.py` | human summary/list and the JSON report file |
| `__main__.py` | argparse CLI |

## The run

1. **`GET /api/catalog/freshness`** — read `frontier` and
   `fingerprint_window_bytes`. If `archive_rows` is `0`, stop (the catalog was
   never scanned; `reconcile` would `409`). If `frontier` is in the past, warn
   that recent files will be held `IN_FLIGHT`.
2. **Enumerate** `/sdcard/DCIM` and `/sdcard/Pictures` for images and videos
   (hidden files and non-media skipped), collecting `{relpath, name, size,
   mtime, sha256}`. `relpath` is relative to `/sdcard` (e.g.
   `DCIM/Camera/PXL_….jpg`); `mtime` is ISO-8601 **with a UTC offset**. No
   hashing unless `--hash`, which adds a whole-file SHA-256.
3. **`POST /api/devices/{id}/reconcile`** in chunks (default 2 000 entries),
   paginating with the returned `snapshot_id`, `final: true` on the last call.
   The request sends `taken_at` as the run's start time (the client's clock,
   recorded not trusted). The response's `entries` covers only the current call;
   the client accumulates them and cross-checks the final cumulative
   `summary.total_files` against what it sent.
4. For each `AMBIGUOUS` entry, **`POST /api/vault/verify`** with
   `{channel_id, tg_message_id, file_size, head_sha256, tail_sha256}`, hashing the
   first and last `fingerprint_window_bytes` of the local file (whole file when
   shorter). `match: true` promotes it to `ARCHIVED`/`FINGERPRINT`.
5. Build the deletable set = `ARCHIVED` only. Print a summary grouped by folder
   with counts and reclaimable bytes, plus the file list (or `--json`).
6. **Confirm** — one `y/N` prompt (`--yes` skips; `--dry-run` stops here).
7. **Delete**, then refresh MediaStore.
8. **`POST /api/devices/{id}/deletions`** with
   `{relpath, name, size, tier, channel_id, tg_message_id, deleted_at}` for the
   files that actually went.
9. Write the report file and print the result. The report defaults to
   `~/storage/shared/vault-client-reports/<UTC timestamp>.json` (so it is
   visible outside Termux) and is configurable.

## Deletion safety

- Only `ARCHIVED` authorises a deletion. `IN_FLIGHT`, `NOT_ARCHIVED` and
  unsettled `AMBIGUOUS` are shown and kept.
- **Re-stat guard:** immediately before `unlink`, re-read size and mtime; if
  either changed since enumeration, skip the file and report it. This closes the
  window where a file is edited between inventory and deletion.
- **MediaStore refresh:** after deleting, invoke `termux-media-scan -r <dir>`
  (Termux:API) if present, else an `am broadcast` media-scanner for the
  directory; best-effort, so the gallery does not show ghost entries.
- **No blind reconcile retries.** The contract states a retried `reconcile`
  double-counts and is fatal to the snapshot: on an ambiguous network failure the
  client starts a fresh snapshot rather than retrying.
- The audit is written after deletion; an interrupt mid-delete still writes the
  audit for what was removed before it stopped.

## Ambiguous files, honestly

`POST /api/vault/verify` compares the head and tail of the archived copy with the
local file's **and requires the sizes to be equal**. So it settles a byte-identical
copy whose metadata differed — notably a `case_only_match` — but a genuine
`size_mismatch` cannot match and correctly stays `AMBIGUOUS`, so the file is kept.
The client does not pretend otherwise; it reports the unsettleable ones.

## Configuration and auth

`~/.config/vault-client/env` (not in the repo, `chmod 600`):

```
VAULT_SERVER=http://atlas.tail57bbb.ts.net:8000
VAULT_DEVICE_ID=pixel
VAULT_API_KEY=…
VAULT_ROOTS=/sdcard/DCIM,/sdcard/Pictures
```

(The server speaks plain HTTP; Tailscale's WireGuard layer encrypts the
transport. The API key travels only inside the tailnet.)

Flags override the file; the file overrides defaults. The API key never lives in
the repo or in a report.

## CLI

```
python -m vault_client            # review, then prompt
  --dry-run            # print the plan and stop; never delete
  --yes                # skip the confirmation prompt
  --hash               # whole-file SHA-256 for tier A evidence
  --roots DIR,DIR      # override the configured roots
  --device-id ID       # override the configured device id
  --server URL         # override the configured server
  --json               # machine-readable report on stdout
```

## Errors and pacing

| condition | behaviour |
|---|---|
| `401` | bad/missing API key — stop with a clear message |
| `409` | catalog never scanned, or snapshot conflict — stop with guidance |
| `413` | chunk too large — halve it and continue against the same snapshot |
| `503` | a service is not configured/up — stop |
| verify `404` | archived message gone — keep the file, report it |
| server unreachable | clear message; the configured URL is used as-is (Tailscale default, LAN by flag) |

The phone makes only vault-API calls, sequentially, in bounded chunks with a
small pause between verify calls. No Telegram rate limits are involved; the load
is one inventory plus a handful of requests.

## Testing

Repo tests under `tests/`, no network and no phone, house style:

- `test_vault_client_enumerate.py` — extension filtering, relpath, mtime with
  offset, hidden/non-media skipped; a test asserts the client's extension set
  matches the server's media detection.
- `test_vault_client_api.py` — request shape and pagination against an injected
  fake transport: `snapshot_id` continuation, `final` on the last chunk, `413`
  halving, error mapping.
- `test_vault_client_pipeline.py` — verdict partitioning (only `ARCHIVED`
  deletable), ambiguous promotion via verify, the re-stat guard skipping a
  changed file, audit payload, `--dry-run` deleting nothing, all with `tmp_path`
  files and a fake API.
- `test_vault_client_report.py` — summary math and JSON rendering.

## Known limitations and open risks

- **Android 17 LAN gate.** If Tailscale is unavailable and only the LAN is
  reachable, the client may be blocked by `ACCESS_LOCAL_NETWORK`; the fix is
  upstream in Termux or an on-device permission grant, both outside this design.
- **Storage permission is fragile.** A Termux reinstall or an OS reset drops
  All-files access; the client should detect a delete that fails with `EACCES`
  and say exactly which Settings toggle to re-enable.
- **Large libraries require pagination.** `RECONCILE_MAX_ENTRIES` is 10 000; the
  client must paginate, and a run is not resumable across process restarts
  (`snapshot_id` is in-memory only) — a rerun is a fresh inventory.
- **Catalog staleness.** Verdicts are only as fresh as the last scan; the
  6-hourly rescan timer bounds this, and stale entries fail closed to
  `IN_FLIGHT`.
- **Full hashing is expensive.** `--hash` reads every byte; the default avoids it.
