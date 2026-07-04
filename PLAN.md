# Telegram Photo Vault — Implementation Plan

Date: 2026-07-04. Companion doc: `docs/video-chunking-design.md` (chunked >2 GB uploads).

## Goals

1. Compile successfully *(verified: all modules import on Python 3.11 with pinned deps)*
2. Expose a home page dashboard
3. Run on schedule and on demand
4. Recover photos already uploaded to the channel and tidy them (re-upload with caption + filtering)
5. Split >2 GB videos into Telegram-uploadable chunks, trivially mergeable after download

## Teldrive assessment (github.com/tgdrive/teldrive)

Teldrive is an actively maintained (v1.8.3, Feb 2026, MIT) Go service that turns Telegram
into a generic personal cloud: files are stored as chunked document messages in a private
channel, a **PostgreSQL** database holds the virtual-filesystem metadata (file → parts
mapping), and it exposes a REST API + web UI + a (modified-)rclone backend.

**Verdict: prior art, not a dependency.**

- *Not a replacement*: teldrive is a filesystem abstraction. It does not do hashtag
  captions, EXIF-derived organization, WebP mirroring to the Odroid, MEGA ingestion, or
  channel tidying — the actual goals of this project.
- *Not the chunk engine either*: its part messages are machine-oriented (optionally
  random-named, no meaningful captions) and the channel is **not self-describing** —
  reconstruction depends on its Postgres DB. That directly conflicts with this project's
  core requirement that the channel alone be recoverable (`cat name.part* > name.mp4`).
  Integrating it would add a Postgres + sidecar-service + second-session footprint to
  avoid writing ~300 lines of chunking code we want to control anyway.
- *What it does give us*: strong validation that byte-split parts-as-documents works at
  scale on Telegram; engineering patterns worth borrowing (upload concurrency/pacing,
  strict rate-limit discipline — teldrive's docs warn accounts get banned for API misuse,
  which reinforces our conservative FloodWait handling); and, unrelated to the vault, it
  remains a fine standalone tool if a general "Telegram as disk" is ever wanted.

## Phase order

Rationale: the Telegram client library underpins phases 3 and 4, so it moves first;
recovery (3) and chunking (4) share caption/manifest conventions, so recovery validates
them cheaply before chunking builds on them.

### Phase 1 — kurigram migration + media-type routing
- Replace abandoned Pyrogram with the maintained, API-compatible **kurigram** fork
  (also unlocks 4 GB premium uploads for the chunker).
- Add `media_type` (IMAGE/VIDEO/OTHER) to `Photo`, detected at discovery.
  Routing: images → full pipeline; videos → skip WebP/SFTP (MEGA→TG→cleanup);
  other → recorded as `SKIPPED`, left on MEGA.
- HEIC support via `pillow-heif` (iPhone camera uploads would otherwise fail compression).
- Caption fallback chain: EXIF → date-in-filename → file mtime.
- Configure logging (worker logs are currently invisible: root logger never configured).
- Additive SQLite migration helper (`ALTER TABLE ... ADD COLUMN`) so existing DBs upgrade.

### Phase 2 — scheduled + on-demand runs, dashboard
- Replace the hot 5 s poll (full recursive `mega-ls -R` each pass) with: scheduled runs
  every `WORKER_RUN_INTERVAL` seconds (default 900), `WORKER_MODE=interval|manual`,
  and an `asyncio.Event` wake for on-demand triggers. A run drains the pipeline until
  no photo makes progress.
- `POST /api/run` (trigger now), `GET /api/photos` (filterable), `POST /api/photos/{id}/retry`
  (requeue FAILED from the step that failed — new `failed_status` column).
- `GET /api/status` gains worker state (mode, running, last/next run).
- HTML dashboard at `/`: status tiles, worker card + Run now, disk gauge, failed table
  with retry, recovery controls. Static page; API key entered once, kept in localStorage.

### Phase 3 — channel recovery + tidy
- New `recovery_items` table keyed by `tg_message_id`; resumable state machine
  SCANNED → DOWNLOADED → (PLANNED on dry-run) → REUPLOADED → COMPLETED,
  plus SKIPPED / DUPLICATE / FAILED.
- Scan: iterate full channel history; ingest photo/video/document/animation messages;
  skip our own chunk/manifest messages and already-tidy documents (caption already
  carries the hashtag scheme).
- Tidy: download original → SHA-256 dedupe → rebuild caption (EXIF → filename date →
  message date; Telegram-native photos are EXIF-stripped, documents preserve bytes) →
  re-upload as document → delete old message **only after** the replacement is confirmed.
- Safety: dry-run is the default (`POST /api/recovery/run` with `dry_run=false` to act),
  duplicates are flagged not deleted, explicit FloodWait handling + per-message delay.

### Phase 4 — chunked >2 GB uploads (per docs/video-chunking-design.md)
- Byte-range split, 1.9 GB chunks; per-chunk + whole-file SHA-256 computed in one
  streaming pass; `name.partNNN-of-MMM` naming; `#chunked` captions; JSON manifest
  uploaded **last** as commit marker; MEGA deletion gated on manifest upload.
- `upload_chunks` child table + `is_chunked`/`sha256`/`total_size`/`manifest_tg_message_id`
  on `photos`; new `CHUNK_UPLOADING` state between DOWNLOADED and TG_UPLOADED; routing
  by size threshold in the DOWNLOADED step; streaming window reader (peak disk = 1× file).
- `scripts/vault_merge.py`: verify + merge downloaded parts from the manifest
  (plain `cat` + `sha256sum -c` also works, by design).

### Phase 5 — tests, CI, docs
- pytest unit tests for the pure logic (mega-ls parsing, captions, chunk math, manifest
  round-trip, window reader, media detection, path builders).
- GitHub Actions: compileall + pytest on 3.11.
- AGENTS.md updated (env vars, endpoints, state machine).

## Deferred / out of scope for now
- par2 parity chunks (off by default per design doc), transcoded `#preview` uploads,
  Telegram-download mode in vault-merge, cron-expression schedules (interval + on-demand
  covers the goal), teldrive/rclone side-integration.
