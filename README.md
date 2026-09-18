# Telegram Photo Vault

Self-hosted pipeline that archives your camera uploads: it pulls photos and
videos from a MEGA folder, stores the originals in a private Telegram channel
(with date hashtag captions for browsing), mirrors compressed WebP copies to a
local server over SFTP, and then cleans the MEGA source. Files larger than
Telegram's 2 GB cap are split into verifiable chunks that merge back with a
single `cat`.

```
MEGA /Camera ──▶ download ──▶ Telegram channel (original, captioned)
                    │              └─ >2GB: .partNNN-of-MMM chunks + manifest
                    └─▶ WebP ──▶ Odroid via SFTP
                                        └─ then delete MEGA source + temp files
```

It also includes a **channel recovery** tool that scans everything already in
the channel and re-uploads it "tidy": as documents, deduplicated, with the
same caption scheme.

- Guide: this file
- Reference (API, env vars, schemas, formats): [`docs/REFERENCE.md`](docs/REFERENCE.md)
- Chunked-upload design rationale: [`docs/video-chunking-design.md`](docs/video-chunking-design.md)
- Implementation plan/history: [`PLAN.md`](PLAN.md) · Agent notes: [`AGENTS.md`](AGENTS.md)

## Quickstart (Docker)

1. **Telegram API credentials** — create an app at
   [my.telegram.org](https://my.telegram.org) to get `TELEGRAM_API_ID` and
   `TELEGRAM_API_HASH`. The uploader is a *user* session (not a bot), so it can
   post files up to 2 GB (4 GB with Premium).

2. **Generate a session string** (one-off, on any machine with Python):

   ```bash
   pip install kurigram TgCrypto
   python -c "
   from pyrogram import Client
   with Client('tmp', api_id=API_ID, api_hash='API_HASH', in_memory=True) as app:
       print(app.export_session_string())
   "
   ```

   Log in when prompted, then put the printed value in `TELEGRAM_SESSION_STRING`.
   (Alternative: run once locally without it and complete the interactive login;
   a session file is created next to the app.)

3. **Create `.env`** in the repo root:

   ```env
   TELEGRAM_API_ID=12345
   TELEGRAM_API_HASH=abcdef...
   TELEGRAM_CHANNEL_ID=-1001234567890
   TELEGRAM_SESSION_STRING=...
   MEGA_EMAIL=you@example.com
   MEGA_PASSWORD=...
   MEGA_TARGET_FOLDER=/Camera
   ODROID_HOST=192.168.1.50
   ODROID_USERNAME=vault
   ODROID_KEY_PATH=/data/ssh/id_ed25519        # or ODROID_PASSWORD
   ODROID_KNOWN_HOSTS=/data/ssh/known_hosts
   API_KEY=pick-a-long-random-string
   ```

   The channel ID is the `-100…` form; the account in the session must be able
   to post to it. See [`docs/REFERENCE.md`](docs/REFERENCE.md) for every
   optional variable and its default.

4. **Run it:**

   ```bash
   docker compose up --build -d
   ```

5. **Open the dashboard** at `http://<host>:8000/`, paste your `API_KEY` into
   the key field (stored in your browser), and you'll see the pipeline tiles,
   worker state, disk usage, and failed items.

## Day-to-day operation

- **Scheduled runs**: by default the worker runs every 15 minutes
  (`WORKER_RUN_INTERVAL=900`). Each run discovers new MEGA files and drains the
  pipeline. Set `WORKER_MODE=manual` to run only on demand.
- **Run now**: the dashboard button, or `curl -X POST -H "X-Api-Key: $KEY" http://host:8000/api/run`.
- **Failures**: items that fail 3 times land in the dashboard's *Failed items*
  table with their traceback; the Retry button requeues them at the exact step
  that failed.
- **File types**: images go through the full pipeline (including HEIC); videos
  are archived to Telegram only (no WebP/SFTP); anything else is recorded as
  `SKIPPED` and left on MEGA untouched.

## Tidying the existing channel (recovery)

For media that was uploaded to the channel before this project (or by earlier
versions), the recovery tool re-uploads it in the vault's canonical form.

1. **Scan** (dashboard button or `POST /api/recovery/scan`) — walks the entire
   channel history and indexes every media message. Already-tidy documents and
   chunk artifacts are skipped automatically.
2. **Dry run** (`POST /api/recovery/run`, default) — downloads each item,
   flags duplicates by SHA-256, and records the caption it *would* apply.
   Review via the dashboard or `GET /api/recovery/items?status=PLANNED`.
3. **Tidy** (`POST /api/recovery/run` with `{"dry_run": false}`) — re-uploads
   each item as a captioned document and deletes the original message **only
   after** the replacement is confirmed. Duplicates are flagged, never deleted.

Captions are derived from EXIF, then a date in the filename, then the message
date. Note that Telegram-native photos were recompressed by Telegram at upload
time — recovery preserves what exists; it cannot restore stripped EXIF.

## Large videos (> 2 GB)

Automatic: anything over `CHUNK_THRESHOLD` is uploaded as byte-range chunks
(`movie.mp4.part001-of-012`, …) followed by a small `movie.mp4.manifest.json`
that carries every hash. The manifest is the completeness marker — if it
exists, the set is whole.

**To restore a chunked file:** download all its `.part*` files and the
manifest into one directory (Telegram Desktop: search the channel for the
filename), then either

```bash
python scripts/vault_merge.py movie.mp4.manifest.json   # verifies every hash
```

or, with no tooling at all:

```bash
LC_ALL=C cat movie.mp4.part* > movie.mp4
sha256sum movie.mp4        # compare with "sha256" in the manifest
```

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q                      # no network needed; Telegram/MEGA are faked
uvicorn app.main:app --reload  # local run (needs MEGAcmd + env vars)
```

CI (GitHub Actions) runs `compileall` + the test suite on Python 3.11 for
every push and pull request.
<!-- autodoc:begin -->
<!-- autodoc: d73b7ae 2026-09-17 -->
---

## The state this repo is actually in

Added by a quality pass on 2026-09-17. The sections above are the owner's own
prose and were left untouched; this block records what is true today, including
where the two disagree.

### Correction: the recovery section above describes a design that was replaced

"Tidying the existing channel (recovery)" still describes the original
download-everything / dedupe-by-SHA-256 / re-upload / delete-the-original flow.
Commit `67cb660` replaced it with a **hybrid in-place tidy**, and that is what
runs now:

- The caption is derived from a date in the filename, else the message post
  date — **with no download at all**.
- Only image *documents* whose filename has no date are downloaded, purely to
  read their EXIF, and that download is gated by a free-space floor
  (`RECOVERY_MIN_FREE_GB`) and a per-batch byte cap
  (`RECOVERY_BATCH_MAX_DOWNLOAD_GB`). An item that would breach the floor is
  deferred to a later batch, not failed.
- The caption is then **edited in place**; any existing free text is preserved
  and the hashtags are appended below it.
- **Nothing is ever deleted or re-uploaded**, and nothing is deduplicated.
  `RECOVERY_DELETE_OLD` is inert.
- Each `POST /api/recovery/run` processes **one batch** (`RECOVERY_BATCH_SIZE`,
  default 300). Tidying a large channel means calling it repeatedly.

`AGENTS.md`, `CLAUDE.md` and `docs/REFERENCE.md` describe the current behaviour
correctly.

### What runs

- **The worker pipeline** — MEGA discovery → Telegram document upload (chunked
  above `CHUNK_THRESHOLD`) → WebP → SFTP → MEGA delete. Exercised end to end by
  the test suite against fakes.
- **The channel tidy and the browse-channel backfill** — see above and
  `.claude/skills/tidy-channel-batch/SKILL.md`.
- **`scripts/backup_local_folder.py`** — resumable one-off migration of a local
  folder into its own channel, with its own `sqlite3` state DB. It never deletes
  a source, and says `SAFE TO DELETE` only at 100% verified.
- **`scripts/vault_merge.py`** — stdlib-only verify-and-merge for chunked files.
  See `.claude/skills/restore-chunked-file/SKILL.md`.

### What is dead or half-finished

Three `RecoveryStatus` values, two `recovery_items` columns and the
`RECOVERY_DELETE_OLD` knob are left over from the superseded recovery design and
do nothing. `--keep-going` in `vault_merge.py` only affects one of its three
failure modes, and `--channel-id` is silently ignored alongside `--scan-only`.
All of them are named, with their evidence, under **Known issues** in
[`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — nothing was changed to
hide them.

### Running it on this machine

The Docker quickstart above is accurate, but day to day this repo runs from a
local venv against `local-data/`, with `.env` pointing `DATABASE_URL`,
`WORKER_DOWNLOAD_ROOT`, `WORKER_COMPRESSED_ROOT`, `RECOVERY_DOWNLOAD_ROOT` and
`DATA_VOLUME_PATH` there. The compose file's `/data` paths are not the live
paths.

```bash
uv venv --python 3.14 .venv                                   # a uv venv has no pip inside
VIRTUAL_ENV=.venv uv pip install -r requirements.txt -r requirements-dev.txt
source .venv/bin/activate

pytest -q                          # full suite, ~3 s, no network and no credentials
ruff check .                       # pyflakes-level errors only; see ruff.toml
python -m compileall -q app scripts

uvicorn app.main:app --reload --port 8000   # needs MEGAcmd logged in (mega-whoami) + env vars
```

The local venv is Python 3.14; CI runs 3.11, so the code must stay
3.11-compatible.

### Layout

| path | what it is |
|---|---|
| `app/` | the service: `main.py` (composition root), `worker.py`, `api/`, `models/`, `services/`, `static/` |
| `scripts/` | the two standalone tools, run by hand |
| `tests/` | pytest suite — pure rules plus functional flows against hand-written fakes |
| `docs/` | `REFERENCE.md` (API, schema, formats), `TROUBLESHOOTING.md`, design specs |
| `.claude/skills/` | the repeatable workflows of this repo |
| `deploy/teldrive/` | an unrelated compose kit; shares no code with the app |

### Where to look next

- Something is broken → [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)
- Exact API, schema and chunk formats → [`docs/REFERENCE.md`](docs/REFERENCE.md)
- State machines, every env var → [`AGENTS.md`](AGENTS.md)
- Working on the code → [`CLAUDE.md`](CLAUDE.md)
<!-- autodoc:end -->
