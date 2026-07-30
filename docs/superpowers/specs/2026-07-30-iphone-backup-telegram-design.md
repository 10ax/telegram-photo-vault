# iPhone Backup → Telegram Archive (one-off migration)

Date: 2026-07-30. Standalone companion to the main pipeline; see `README.md` /
`docs/REFERENCE.md` for the production MEGA→Telegram→Odroid flow this reuses code from.

## Goal

Move `/home/tenax/Pictures/iPhone backup/` (106 GB, 6916 files, standard DCIM layout —
`100APPLE` … `108APPLE`, HEIC/JPG/MOV) off atlas's local disk and into a new, dedicated
Telegram channel, so the space can be reclaimed on atlas's `/home` partition (84% full,
128 GB free at time of writing). This is a one-time migration, not an ongoing sync.

## Non-goals

- No automatic deletion of local files. The script only uploads and verifies; deleting
  the source folder afterward is a manual, separate action by the user.
- No MEGA ingestion, no WebP compression, no Odroid SFTP mirroring — those are the
  production pipeline's concerns and are untouched.
- No EXIF-based date organization/hashtags. Captions carry the original relative path
  instead, since this is archival, not a browsable timeline.
- No changes to the running `telegram-photo-vault` container or its `docker-compose.yml`.

## Architecture

A standalone script, `scripts/backup_local_folder.py`, run once (and re-run to resume) via:

```bash
docker compose run --rm \
  --entrypoint python \
  -v "/home/tenax/Pictures/iPhone backup:/backup-source:ro" \
  -v "./scripts:/app/scripts:ro" \
  telegram-photo-vault scripts/backup_local_folder.py
```

This reuses the existing image/dependencies/env injection (`env_file: .env`) with zero
changes to the compose file or the Dockerfile (the Dockerfile only `COPY`s `app/`, not
`scripts/`, so the script is bind-mounted in rather than baked into the image).
`--entrypoint python` deliberately bypasses `docker/entrypoint.sh`, which starts
`mega-cmd-server` and logs into MEGA before `exec`-ing the container's command — entirely
unrelated to this script and a needless extra failure mode (MEGA login issues would abort
a run that never touches MEGA). The source folder is mounted read-only — the script
cannot delete or modify it even if a future edit introduced a bug.

Reused from the existing app (imported, not duplicated):

- `app/services/chunking.py`: `plan_chunks`, `chunk_name`, `manifest_name`,
  `compute_hashes`, `ChunkWindow`, `build_manifest`, `build_chunk_caption`,
  `build_manifest_caption`.
- `app/services/telegram.py`: `TelegramService.upload_document` /
  `upload_file_object` (the `MediaType`-dependent methods — `upload_media`,
  `publish_browse` — are not used).

New in this script only: the local SQLite state table, the local-folder walk, the
channel-creation step, and the verify-by-redownload step.

## State & resumability

SQLite at `/data/iphone_backup_state.db` (already persisted via the existing
`./data:/data` volume — no new volume needed).

```sql
CREATE TABLE files (
  rel_path TEXT PRIMARY KEY,      -- e.g. "106APPLE/IMG_6849.MOV"
  size INTEGER NOT NULL,
  sha256 TEXT,                    -- whole-file hash, filled at HASHED
  status TEXT NOT NULL,           -- PENDING|HASHED|UPLOADED|VERIFIED|FAILED
  is_chunked INTEGER NOT NULL DEFAULT 0,
  chunk_count INTEGER,
  tg_message_id INTEGER,          -- single-doc case
  manifest_tg_message_id INTEGER, -- chunked case
  error TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- meta["channel_id"] holds the created channel's -100… id
```

On startup: walk the mounted source folder, `INSERT OR IGNORE` every file as `PENDING`
(so re-running after adding/removing nothing is a no-op scan). Any row not already
`VERIFIED` is retried. A file that disappears from disk between runs is left as-is in
the DB (its history is kept; nothing auto-purges).

## Channel creation

On first run, if `meta["channel_id"]` is absent: call `Client.create_channel("iPhone
Backup Archive")` using the same session already configured in `.env`
(`TELEGRAM_API_ID`/`HASH`/`SESSION_STRING` — the same account as the production
channel, just a new destination channel). Store the returned id in `meta` and print it
once to stdout. All subsequent runs read it from `meta` and skip creation.

## Per-file flow

```
PENDING ─(compute_hashes)─▶ HASHED ─(upload)─▶ UPLOADED ─(redownload+compare)─▶ VERIFIED
                                                    │
                                                    └─(any step raises)─▶ FAILED (error saved)
```

1. **HASHED**: `compute_hashes(path, chunk_size)` — one streaming pass, whole-file
   SHA-256 (+ per-chunk hashes only relevant when chunked).
2. **UPLOADED**:
   - Files ≤ `CHUNK_THRESHOLD` (env, default `1950000000`): single
     `TelegramService.upload_document`, caption = `f"{rel_path}\nsize={size}
     sha256={sha256[:16]}"`. Store `tg_message_id`.
   - Files > threshold (4 files in this folder, up to 3.1 GB): `plan_chunks` +
     `ChunkWindow` per chunk, uploaded via `upload_file_object` with
     `build_chunk_caption(rel_path, ...)`, then `build_manifest` uploaded last as the
     commit marker via `build_manifest_caption(rel_path, ...)`. Store
     `manifest_tg_message_id`. Same `.partNNN-of-MMM` naming as production, so
     `scripts/vault_merge.py` can restore these unchanged.
3. **VERIFIED** (chosen verification depth — see below): re-download the just-uploaded
   document (or, for chunked files, all parts + manifest) into a temp path inside
   `/data/tmp-verify/`, recompute SHA-256, compare to the stored hash. Match → `VERIFIED`
   and delete the temp copy. Mismatch → `FAILED` with `error = "hash mismatch on
   verify"`, original `tg_message_id` kept so a human can inspect it on Telegram.

**Verification depth: full re-download compare (approved).** Rejected alternative —
trust Telegram's reported document size against the local size, skip the re-download —
was considered but rejected: it doesn't catch bit-level corruption, and residential
download bandwidth is typically well above upload bandwidth, so the extra round-trip
does not dominate total run time.

## Rate limiting

`upload_delay_seconds` from `TELEGRAM_UPLOAD_DELAY` (env, default `5`), same as
production. kurigram's built-in `sleep_threshold` (`TELEGRAM_SLEEP_THRESHOLD`, default
`60`) auto-sleeps through FloodWaits below that threshold; longer FloodWaits propagate
and abort the run (rerun resumes cleanly — nothing already `VERIFIED` is redone). A
`--delay` CLI flag overrides the env default for this run only, for a faster pass if the
user accepts more FloodWait risk.

Running this script concurrently with the production worker (same account session) is
expected to work — Telegram permits multiple concurrent connections per account — but
both share one FloodWait budget, so heavy overlap will pace both down. Not a blocker,
just a note.

## Output / report

On exit (Ctrl-C, completion, or fatal error), print a summary to stdout:

```
PENDING: 0  HASHED: 0  UPLOADED: 0  VERIFIED: 6910  FAILED: 6
Channel: -100XXXXXXXXXX ("iPhone Backup Archive")
Failed files:
  103APPLE/IMG_3199.HEIC — hash mismatch on verify
  ...
```

The same summary is available on demand by re-running the script (it always re-scans
and reports current state before doing any work) — no separate "report mode" flag is
needed.

Deleting the original folder is a manual step the user takes after reviewing a report
with `FAILED: 0`. Not automated by this script or any other tool in this change.

## Error handling

- Per-file try/except around the HASHED→UPLOADED→VERIFIED chain; any exception marks
  that row `FAILED` with the exception message and moves to the next file. One bad file
  never stops the run.
- FloodWait above `sleep_threshold` is allowed to propagate and end the run (rerun
  resumes from the DB state).
- The temp verify directory (`/data/tmp-verify/`) is cleaned per-file after each
  hash compare (success or mismatch) so disk use inside the container stays bounded to
  roughly one file/chunk-set at a time, not the whole 106 GB.

## Testing

Given this is a one-off script (not a long-lived service), testing is scoped
accordingly:

- Unit tests for the new pure logic only: relative-path walking/DB upsert behavior, and
  the caption-building calls (reusing already-tested `chunking.py` functions — no new
  tests needed for those). Faked Telegram client (the existing test suite already fakes
  `pyrogram.Client` for the main app; reuse that fake).
- No integration test against real Telegram — validated manually against the real
  account during the actual migration run, same as how `vault_merge.py` was validated.

## Open risk, explicitly accepted

`compute_hashes` + full redownload-verify means every file's bytes are read from disk at
least twice (hash) and effectively transferred twice (up + down) for verification. For
106 GB this is expected to take considerably longer than a bare upload. Accepted
per the approved design (see "Verification depth" above) given the source photos are
irreplaceable.
