# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Self-hosted async pipeline (FastAPI, SQLAlchemy 2 async on SQLite, kurigram = maintained Pyrogram fork under the `pyrogram` namespace) that archives camera uploads: a MEGA folder is drained, each original goes to a private Telegram channel **as a document** with date-hashtag captions (files > 2 GB are split into `.partNNN-of-MMM` chunks + a JSON manifest), images are also mirrored as WebP to an Odroid over SFTP, then the MEGA source is deleted. On top of that: a channel **recovery/tidy** subsystem, an optional **browse channel** gallery mirror, and standalone migration scripts.

Read for depth, in this order: `AGENTS.md` (state machines, every env var, agent notes), `docs/REFERENCE.md` (HTTP API, DB schema, chunk/manifest formats), `docs/video-chunking-design.md` (why the channel must stay self-describing), `docs/superpowers/specs/` + `PLAN.md` (design history and rationale).

## Commands

Everything runs from the repo `.venv`. Local venv is Python 3.14; CI runs 3.11, so keep code 3.11-compatible.

```bash
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q                                   # full suite, ~3 s, no network (Telegram/MEGA are faked)
pytest tests/test_chunking.py -q            # one file
pytest tests/test_chunked_flow.py::test_chunked_upload_to_completion -q   # one test
pytest -k caption -q                        # by keyword
python -m compileall -q app scripts         # CI's compile check
```

There is no linter or formatter configured. CI (`.github/workflows/ci.yml`) is `compileall` + `pytest -q` on Python 3.11 for every push and PR.

Run the service:

```bash
uvicorn app.main:app --reload --port 8000   # needs MEGAcmd installed and logged in (mega-whoami) plus env vars
docker compose up --build -d                # containerized; entrypoint starts mega-cmd-server and logs into MEGA
```

Standalone scripts:

```bash
python -m scripts.backup_local_folder --source DIR --state-db FILE [--channel-id ID] [--scan-only] [--skip-verify]
python scripts/vault_merge.py movie.mp4.manifest.json   # stdlib-only chunk verify + merge
```

Code navigation: this repo has a tokensave index in `.tokensave/`. Use the `tokensave_*` MCP tools instead of reading files; if `tokensave_status` reports 0 nodes, run `tokensave sync`.

## How this machine actually runs it

The README describes Docker, but on this machine the app runs from `.venv` against `local-data/`. `.env` (gitignored) points `DATABASE_URL`, `WORKER_DOWNLOAD_ROOT`, `WORKER_COMPRESSED_ROOT`, `RECOVERY_DOWNLOAD_ROOT` and `DATA_VOLUME_PATH` at `local-data/`; the compose file's `/data` paths are not the live paths. `MEGA_TARGET_FOLDER` is currently `/phone_bkp`, not the README's `/Camera`. `/home` is disk-constrained, so anything that downloads must honour the recovery free-space floor. `local-data/iphone_backup_state.db` and `backup-run.log` belong to `scripts/backup_local_folder.py`, not the app.

## Architecture

### Composition root

`lifespan` in `app/main.py` is the only place services are built. It reads env, starts one pyrogram `Client` (session string if set, else a session file), constructs `MegaService`, `TelegramService`, `SFTPService`, `PhotoWorker` and `RecoveryService`, spawns `worker.run_forever()` as an asyncio task, and stores `worker`, `worker_task`, `recovery` and `telegram_client` on `app.state`. Routes in `app/api/routes.py` only read `request.app.state`. Every `/api/*` route depends on `require_api_key` (`X-Api-Key` header: 401 on a wrong key, 503 when `API_KEY` is unset). `GET /` serves `app/static/dashboard.html`; `GET /health` is open.

Adding a config knob means: parse it in `lifespan`, pass it as a constructor kwarg, add it to `docker-compose.yml`, and document it in both `AGENTS.md` and `docs/REFERENCE.md`.

### Worker (`app/worker.py`)

`PhotoWorker.run_forever` sleeps `run_interval` between runs (in `manual` mode it only waits on the wake event that `POST /api/run` sets). A run is discovery (list the MEGA folder, insert unseen files as `PENDING`, or `SKIPPED` for anything that is not image/video) followed by a drain loop that keeps fetching `batch_size` active photos and calling `_process_photo_by_id` until no photo makes progress. `_run_step` dispatches on `PhotoStatus`; each handler performs exactly one transition and the caller commits. On exception: `retry_count += 1`, traceback into `error_log`, and at `max_retries` the step is saved in `failed_status` and status becomes `FAILED`. `POST /api/photos/{id}/retry` resumes from `failed_status`, walking back to `PENDING` if the local file is gone.

Invariants to preserve:

- Videos skip WebP/SFTP: `TG_UPLOADED` goes straight to `_finalize`.
- `_finalize` (MEGA delete + temp cleanup, then `COMPLETED`) is the only step that deletes the source. For chunked files it cannot run before the manifest is uploaded.
- In `CHUNK_UPLOADING` exactly one chunk is uploaded per worker visit (fairness, small crash window). Before re-sending a chunk whose `tg_message_id` was never committed, `find_document_by_name` looks for an already-uploaded copy to reuse. The manifest is uploaded last and is the commit marker for the set.
- Browse-channel mirroring is best-effort and must never fail the archival step.

### Telegram (`app/services/telegram.py`)

`TelegramService` wraps the client. Uploads are forced to be documents so Telegram never transcodes archived media; every send is followed by `upload_delay_seconds`, and short FloodWaits are slept by the client's `sleep_threshold`. Caption date chain: EXIF `DateTimeOriginal`, then a date parsed from the filename, then a fallback (file mtime for the worker, message date for recovery); `format_date_caption` yields `#YYYY #MM_YYYY #YYYY_MM_DD`. Other load-bearing methods: `edit_caption`, `find_document_by_name` (best-effort `search_messages`, limit 10, any error means not found), and the browse-channel publish of a native photo/video copy when `browse_channel_id` is set.

### Recovery / tidy (`app/services/recovery.py`)

`RecoveryService` runs one background task per API call (`409` while busy). Scan walks the channel history into `recovery_items`, skipping our own chunk/manifest messages and captions that already carry the hashtag scheme. Run processes **one batch** (`batch_size`) of hybrid **in-place** tidy: the caption is derived from filename or post date with no download; only image documents whose filename has no date are downloaded for EXIF, gated by `min_free_bytes` and `batch_max_download_bytes` (a deferred item just stays `SCANNED`); then `edit_caption` appends the hashtags while preserving existing free text. Nothing is ever deleted; `delete_old` is informational only. `DOWNLOADED`, `REUPLOADED` and `DUPLICATE` are legacy statuses still present in the enum and in `/api/status` counts and must not be produced by new code. `backfill` copies native media into the browse channel server-side with `copy_message` (no download) and resumes via `recovery_items.browse_tg_message_id`.

### Chunking (`app/services/chunking.py`)

Pure helpers shared by the worker and the backup script: `plan_chunks`, `chunk_name` (`name.partNNN-of-MMM`, zero-padded so lexicographic order equals numeric order), `compute_hashes` (per-chunk and whole-file SHA-256 in one streaming pass), `ChunkWindow` (file-like byte range, peak disk stays 1x the file), `build_manifest`, `build_chunk_caption`, `build_manifest_caption`. These formats are a contract: `LC_ALL=C cat name.part* > name` must always reassemble byte-exactly, and the channel alone (no DB) must be enough to find, order, verify and merge chunks.

### Database (`app/models/database.py`)

`DATABASE_URL` is read at import time and the engine plus `AsyncSessionLocal` are module globals, which is why `tests/conftest.py` sets the env var before importing anything from `app`. `init_db()` is `create_all` plus additive migrations (`PRAGMA table_info` then `ALTER TABLE ADD COLUMN`); there is no migration tool, so new columns must be nullable or defaulted, and columns are never renamed or dropped. `updated_at` uses a server-side `onupdate`, so `await session.refresh(row)` before serializing a committed row.

### Standalone scripts (`scripts/`)

- `backup_local_folder.py`: one-off, resumable migration of a local folder into its own Telegram channel. Keeps its own stdlib `sqlite3` state DB (never the app DB); any row not `VERIFIED` is retried on rerun and `FAILED` rows restart from scratch. Reuses `chunking.py` and `TelegramService` unmodified. Verifies each upload by re-download + hash unless `--skip-verify`; always prints a report and says `SAFE TO DELETE` only at 100% verified. It never deletes sources. Designed to run either from `.venv` or via `docker compose run --rm --entrypoint python` with the folder bind-mounted read-only (the image only copies `app/`, not `scripts/`).
- `vault_merge.py` must stay stdlib-only so it can be copied anywhere.
- `deploy/teldrive/` is a separate teldrive "family drive" compose kit and shares no code with the app.

## Tests

`pytest.ini` sets `asyncio_mode = auto` with function-scoped loops. Use the `clean_db` fixture: it drops and recreates the schema and **disposes the engine**, because pooled aiosqlite connections must not cross event loops (any fixture that creates its own loop must dispose too, see the `client` fixture in `tests/test_api.py`). No network anywhere: flows are exercised with hand-written fakes (`FakeTelegram`, `FakeMega`, `FakeClient`, `FakeUploadService`, ...) that duck-type only the methods under test; follow that style instead of `unittest.mock` patching. API tests build a bare `FastAPI()` with the router and set `app.state.*` by hand, never running the lifespan.

## Design workflow

Non-trivial features start as a spec in `docs/superpowers/specs/YYYY-MM-DD-*-design.md` with a matching plan in `docs/superpowers/plans/`; `PLAN.md` is the top-level phase history. Anything that deletes Telegram messages is dry-run by default and needs an explicit `--apply` or `dry_run: false`; keep that posture.

# RTK (Rust Token Killer) - Token-Optimized Commands

## Golden Rule

**Always prefix commands with `rtk`**. If RTK has a dedicated filter, it uses it. If not, it passes through unchanged. This means RTK is always safe to use.

**Important**: Even in command chains with `&&`, use `rtk`:
```bash
# ❌ Wrong
git add . && git commit -m "msg" && git push

# ✅ Correct
rtk git add . && rtk git commit -m "msg" && rtk git push
```

## RTK Commands by Workflow

### Build & Compile (80-90% savings)
```bash
rtk cargo build         # Cargo build output
rtk cargo check         # Cargo check output
rtk cargo clippy        # Clippy warnings grouped by file (80%)
rtk tsc                 # TypeScript errors grouped by file/code (83%)
rtk lint                # ESLint/Biome violations grouped (84%)
rtk prettier --check    # Files needing format only (70%)
rtk next build          # Next.js build with route metrics (87%)
```

### Test (60-99% savings)
```bash
rtk cargo test          # Cargo test failures only (90%)
rtk go test             # Go test failures only (90%)
rtk jest                # Jest failures only (99.5%)
rtk vitest              # Vitest failures only (99.5%)
rtk playwright test     # Playwright failures only (94%)
rtk pytest              # Python test failures only (90%)
rtk rake test           # Ruby test failures only (90%)
rtk rspec               # RSpec test failures only (60%)
rtk test <cmd>          # Generic test wrapper - failures only
```

### Git (59-80% savings)
```bash
rtk git status          # Compact status
rtk git log             # Compact log (works with all git flags)
rtk git diff            # Compact diff (80%)
rtk git show            # Compact show (80%)
rtk git add             # Ultra-compact confirmations (59%)
rtk git commit          # Ultra-compact confirmations (59%)
rtk git push            # Ultra-compact confirmations
rtk git pull            # Ultra-compact confirmations
rtk git branch          # Compact branch list
rtk git fetch           # Compact fetch
rtk git stash           # Compact stash
rtk git worktree        # Compact worktree
```

Note: Git passthrough works for ALL subcommands, even those not explicitly listed.

### GitHub (26-87% savings)
```bash
rtk gh pr view <num>    # Compact PR view (87%)
rtk gh pr checks        # Compact PR checks (79%)
rtk gh run list         # Compact workflow runs (82%)
rtk gh issue list       # Compact issue list (80%)
rtk gh api              # Compact API responses (26%)
```

### JavaScript/TypeScript Tooling (70-90% savings)
```bash
rtk pnpm list           # Compact dependency tree (70%)
rtk pnpm outdated       # Compact outdated packages (80%)
rtk pnpm install        # Compact install output (90%)
rtk npm run <script>    # Compact npm script output
rtk npx <cmd>           # Compact npx command output
rtk prisma              # Prisma without ASCII art (88%)
```

### Files & Search (60-75% savings)
```bash
rtk ls <path>           # Tree format, compact (65%)
rtk read <file>         # Code reading with filtering (60%)
rtk grep <pattern>      # Search grouped by file (75%). Format flags (-c, -l, -L, -o, -Z) run raw.
rtk find <pattern>      # Find grouped by directory (70%)
```

### Analysis & Debug (70-90% savings)
```bash
rtk err <cmd>           # Filter errors only from any command
rtk log <file>          # Deduplicated logs with counts
rtk json <file>         # JSON structure without values
rtk deps                # Dependency overview
rtk env                 # Environment variables compact
rtk summary <cmd>       # Smart summary of command output
rtk diff                # Ultra-compact diffs
```

### Infrastructure (85% savings)
```bash
rtk docker ps           # Compact container list
rtk docker images       # Compact image list
rtk docker logs <c>     # Deduplicated logs
rtk kubectl get         # Compact resource list
rtk kubectl logs        # Deduplicated pod logs
```

### Network (65-70% savings)
```bash
rtk curl <url>          # Compact HTTP responses (70%)
rtk wget <url>          # Compact download output (65%)
```

### Meta Commands
```bash
rtk gain                # View token savings statistics
rtk gain --history      # View command history with savings
rtk discover            # Analyze Claude Code sessions for missed RTK usage
rtk proxy <cmd>         # Run command without filtering (for debugging)
rtk init                # Add RTK instructions to CLAUDE.md
rtk init --global       # Add RTK to ~/.claude/CLAUDE.md
```

## Token Savings Overview

| Category | Commands | Typical Savings |
|----------|----------|-----------------|
| Tests | vitest, playwright, cargo test | 90-99% |
| Build | next, tsc, lint, prettier | 70-87% |
| Git | status, log, diff, add, commit | 59-80% |
| GitHub | gh pr, gh run, gh issue | 26-87% |
| Package Managers | pnpm, npm, npx | 70-90% |
| Files | ls, read, grep, find | 60-75% |
| Infrastructure | docker, kubectl | 85% |
| Network | curl, wget | 65-70% |

Overall average: **60-90% token reduction** on common development operations.
<!-- autodoc:begin -->
<!-- autodoc: d73b7ae 2026-09-17 -->
---

## Verification (added 2026-09-17)

These four commands are the contract. Run all of them from the repo root before
claiming anything works; a green suite is the only evidence that counts.

```bash
uv venv --python 3.14 .venv
VIRTUAL_ENV=.venv uv pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
python3 -m compileall -q app scripts
```

`uv venv` does **not** put pip inside the venv — `.venv/bin/python -m pip`
fails. Install through `uv pip install` with `VIRTUAL_ENV` pointed at the venv,
as above, or build the venv with `python -m venv` instead.

The local venv is Python **3.14**; `.github/workflows/ci.yml` runs **3.11**.
Anything newer than 3.11 passes here and fails there. Before changing anything
version-sensitive:

```bash
uv venv --python 3.11 /tmp/vault311
VIRTUAL_ENV=/tmp/vault311 uv pip install -r requirements.txt -r requirements-dev.txt
/tmp/vault311/bin/python -m pytest -q
```

### Linting

`ruff.toml` enables only `E9` (syntax/IO errors) and `F` (pyflakes), with
`target-version = "py311"`. It is an error-finder, not a formatter: no style,
import-order or line-length rules, so it passes on the codebase unchanged — and
note that `tests/test_backup_local_folder.py` deliberately puts imports part-way
down the file, which a wider rule set would flag. Widening the selection is the
owner's call, not something a failing build should force. Ruff also lints
`.agents/skills/` (vendored third-party skills); it passes there today.

### CI

`.github/workflows/ci.yml` on push and pull_request, Python 3.11, with
`concurrency` + `cancel-in-progress`: `compileall` → `ruff check .` → `pytest -q`.
Deterministic steps only — no secrets, no network, no Telegram or MEGA. The
Docker build is not exercised in CI. `.github/dependabot.yml` covers the
`github-actions` ecosystem weekly and deliberately leaves pip alone: kurigram,
Pillow and pillow-heif pin behaviour this archive depends on.

## Writing tests here

Follow the existing style — hand-written fakes that duck-type only the methods
under test, never `unittest.mock` patching, and a `tmp_path` in preference to a
mock whenever the code touches the filesystem. No network, no credentials, and
no real Telegram/MEGA/SFTP call anywhere in the suite.

Two loop rules that will bite you (see `tests/conftest.py`):

- Use the `clean_db` fixture. It drops and recreates the schema and **disposes
  the engine**, because `app/models/database.py` builds the engine as a module
  global and pooled aiosqlite connections must not cross event loops.
- Any fixture that creates its own loop (the `client` fixture in
  `tests/test_api.py` calls `asyncio.run`) must dispose the engine itself.
  `tests/test_db_migrations.py` disposes in a `finally` for the same reason.

### What the suite covers, by file

| file | what it pins |
|---|---|
| `test_telegram_uploads.py` | **`force_document=True` on every archival send**, the caption date chain, upload pacing, best-effort browse mirroring, exact-name chunk lookup |
| `test_worker_state_machine.py` | step dispatch, videos skipping WebP/SFTP, `_finalize` as the only deleter, retry → `failed_status` → `FAILED`, discovery idempotency |
| `test_chunked_flow.py` | the chunked upload end to end, including cat-merge equivalence and chunk reuse after a crash |
| `test_chunking.py` | chunk planning, naming, hashing, `ChunkWindow`, manifest shape |
| `test_vault_merge_cli.py` | the restore CLI as a subprocess: refusals, manifest contract, path flags |
| `test_recovery_flow.py` / `test_recovery_rules.py` | the tidy end to end / its rules: vault-artifact detection, EXIF need, caption merging and truncation, the disk floor |
| `test_browse_publish.py` / `test_browse_backfill.py` | the gallery mirror and its server-side backfill |
| `test_api.py` / `test_api_guards.py` | the happy paths / the refusals: 401, 503, 409, and the retry resume-point rule |
| `test_db_migrations.py` | `init_db()` upgrading a legacy SQLite file in place, idempotently |
| `test_backup_local_folder.py` / `test_backup_reporting.py` | the migration script's per-file pipeline / its report and CLI guards |
| `test_mega_commands.py` | the MEGAcmd subprocess contract, using shell-script stand-ins |
| `test_sftp_service.py` | the host-key guard and remote-path normalisation (no SSH attempted) |
| `test_image_compression.py` | the WebP mirror: long-side cap, mode conversion, EXIF rotation |

`docs/TROUBLESHOOTING.md` ends with a **Known issues** section listing what is
broken and deliberately left unfixed, plus what the suite cannot cover
(`lifespan`, the dashboard, and anything needing the real services).

## What not to touch

- **`force_document=True`.** See above, and commit `9aaa70a`.
- **The on-channel formats** — chunk names, chunk/manifest captions, the
  manifest JSON. Files already uploaded carry them forever, and the channel must
  stay self-describing without this repo or its database.
- **`scripts/vault_merge.py`'s stdlib-only rule.** It has to run where nothing
  else is installed.
- **`AGENTS.md`, `README.md`, `docs/REFERENCE.md` prose and the sections of this
  file above this block** — hand-written and current. Add, don't rewrite.
- **`.agents/skills/` and `skills-lock.json`** — vendored from an external
  source; regenerate them there, not here.
- **Existing database columns.** There is no migration tool; columns are added
  (nullable or defaulted, via `_COLUMN_MIGRATIONS`) and never renamed or
  dropped.

## Repeatable workflows

`.claude/skills/` holds the three this repo actually repeats:

- `tidy-channel-batch` — scan, dry-run and run one batch of the channel tidy,
  and read the batch counters afterwards.
- `restore-chunked-file` — get a >2 GB file back out of the channel and verify
  it.
- `add-config-knob` — the five edits an environment variable needs, from
  `lifespan` through to both reference documents.
<!-- autodoc:end -->
