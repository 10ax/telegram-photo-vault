<!-- autodoc:begin -->
<!-- autodoc: d73b7ae 2026-09-17 -->
---

# Troubleshooting

The file to open when the vault is broken. Every command below exists in this
repo or is a standard system tool; nothing here is invented.

Two conventions used throughout:

```bash
source .venv/bin/activate                        # everything runs from the repo venv
export KEY="$(grep '^API_KEY=' .env | cut -d= -f2-)"   # read the key, never paste it anywhere
export VAULT=http://localhost:8000
```

`.env` is gitignored and holds the only secrets in this project. **Never copy a
channel id, session string, API key, MEGA password or private hostname out of
it into a document, a commit message, an issue or a log you share.** Refer to
them as `$TELEGRAM_CHANNEL_ID`, `$KEY` and so on, as this file does.

Where a command needs the database path, read it from `.env` — on this machine
`DATABASE_URL` points at `local-data/`, not the compose file's `/data`:

```bash
grep '^DATABASE_URL=' .env | sed 's|^DATABASE_URL=sqlite+aiosqlite:///||'
```

---

## Uploads and the archive

### Symptom — a restored file's SHA-256 no longer matches its manifest, or short videos turn up in Telegram's saved-GIFs list

**Check:**

```bash
grep -n 'force_document' app/services/telegram.py
pytest tests/test_telegram_uploads.py -q
```

**Cause:** Telegram inspects what it is sent. Without `force_document=True` it
recognises GIFs and short soundless MP4s (Pixel and Samsung motion photos) as
*animations*, **transcodes them**, and adds each one to the account's saved-GIFs
library. Transcoding rewrites the bytes, so the archived copy no longer matches
the SHA-256 recorded for it — the archive silently stops being an archive. This
happened before commit `9aaa70a`, which set the flag on both send paths.

**Fix:** every send must go through `TelegramService.upload_document`,
`upload_file_object` or `upload_bytes`, all of which pass `force_document=True`.
Never call `client.send_document` directly, and never route an archival upload
through `upload_media` or `publish_browse` — those two send native photos and
videos *on purpose*, and are only used for the human-browsable mirror in
`BROWSE_CHANNEL_ID`, never for the archive. `tests/test_telegram_uploads.py`
fails if the flag is dropped. Bytes already transcoded by Telegram cannot be
recovered; re-upload from the original if you still have it.

### Symptom — `vault_merge.py` says `SHA-256 mismatch` for every part of a file

**Check:**

```bash
python3 -c "import json,sys;m=json.load(open(sys.argv[1]));print([c['sha256'] for c in m['chunks']])" name.manifest.json
```

**Cause:** if that prints a list of empty strings, the file was uploaded by
`scripts/backup_local_folder.py` before commit `6d09fb8`, which hardcoded
per-chunk hashes as `""`. The whole-file `sha256` in the manifest is still
correct; only the per-chunk fields are empty, and `vault_merge.py` checks those
first.

**Fix:** merge without the tool and verify against the whole-file hash instead:

```bash
LC_ALL=C cat name.part* > name
sha256sum name      # compare with "sha256" in the manifest
```

`LC_ALL=C` matters — part numbers are zero-padded so lexicographic order equals
numeric order, and a locale-aware sort can reorder them.

### Symptom — a file over 2 GB appears in the channel as parts with no manifest

**Check:** search the channel for `<name>.manifest.json`, or:

```bash
pytest tests/test_chunked_flow.py -q
```

**Cause:** the manifest is uploaded **last**, after every chunk, and is the
commit marker for the set. Parts without a manifest mean the run was
interrupted mid-upload. The photo is still `CHUNK_UPLOADING` and `_finalize`
cannot run, so the MEGA source has *not* been deleted — nothing is lost.

**Fix:** let the worker run again (`curl -X POST -H "X-Api-Key: $KEY"
$VAULT/api/run`). It uploads exactly one chunk per visit, and before re-sending
a chunk whose message id was never committed it calls `find_document_by_name` to
reuse the copy already in the channel rather than duplicating it.

### Symptom — you want to change the chunk naming, caption or manifest format

**Check:**

```bash
pytest tests/test_chunking.py tests/test_vault_merge_cli.py -q
```

**Cause:** there is no such thing as a format change here. Files already in the
channel carry the old format forever, and the channel must stay
self-describing: the parts, their captions and the manifest are the only thing
needed to find, order, verify and merge a file — no database, no this repo. See
`docs/video-chunking-design.md`.

**Fix:** don't, unless you also ship a migration and bump `manifest_version`.
`vault_merge.py` warns and proceeds on an unknown `manifest_version`, so an
incompatible v2 written under version 1 would fail confusingly rather than
loudly. Keep `vault_merge.py` stdlib-only for the same reason — it has to run on
a machine that has never seen this project.

---

## MEGA

### Symptom — discovery runs cleanly and finds nothing, run after run

**Check:**

```bash
mega-whoami
mega-ls -R "$MEGA_TARGET_FOLDER"
```

**Cause:** MEGAcmd is a client for a background server. If `mega-cmd-server` is
not running, or the session is not logged in, `mega-ls` returns nothing useful
and `MegaService._parse_mega_ls_output` parses that into an empty list. The
worker treats an empty listing as "nothing new" and returns 0 — there is no
error anywhere.

**Fix:** log in (`mega-login`) and confirm with `mega-whoami` before starting
the app. In Docker the entrypoint starts the server and logs in from
`MEGA_EMAIL`/`MEGA_PASSWORD`; check `docker compose logs telegram-photo-vault`.
Also confirm `MEGA_TARGET_FOLDER` is the folder you think it is — on this
machine it is `/phone_bkp`, not the README's `/Camera`.

### Symptom — a photo is stuck at `PENDING` with `MegaCmdError: Command failed`

**Check:**

```bash
curl -s -H "X-Api-Key: $KEY" "$VAULT/api/photos?status=FAILED" | python3 -m json.tool
```

**Cause:** the `error_log` field holds the full traceback, including MEGAcmd's
own stderr. `Command failed (…)` with `Not logged in` is the case above; a
transfer error is usually transient.

**Fix:** fix the underlying cause, then `POST /api/photos/{id}/retry`. The retry
resumes at `failed_status` and walks back to `PENDING` if the local file is
gone, so it is always safe to press.

### Symptom — a completed photo is still on MEGA

**Check:**

```bash
mega-ls -R "$MEGA_TARGET_FOLDER" | grep -F '<filename>'
```

**Cause:** `_finalize` is the only step that deletes the source, and it runs
last — after the Telegram upload and (for images) the WebP/SFTP mirror. A photo
that is not `COMPLETED` has not reached it.

**Fix:** none needed; that ordering is the safety property. If `_finalize` did
run and MEGA reported the file missing, the worker logs a warning and continues
— `PhotoWorker._is_remote_file_missing` treats "not found", "no such file",
"doesn't exist", "does not exist", "path not found" and "could not find" as
already-deleted. Any other MEGA error re-raises and the photo retries.

---

## Disk

### Symptom — the disk fills up during a tidy run, or items stay `SCANNED` forever

**Check:**

```bash
curl -s -H "X-Api-Key: $KEY" $VAULT/api/status \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["recovery"]["disk"])'
df -h .
```

**Cause:** the tidy only downloads image *documents* whose filename has no date,
so their EXIF can be read, and every such download is gated twice: by
`RECOVERY_MIN_FREE_GB` (default 10 GiB free must remain — an item that would
breach it is **deferred**, left `SCANNED`, and retried in a later batch) and by
`RECOVERY_BATCH_MAX_DOWNLOAD_GB` (default 5 GiB, after which the batch stops
early). `/home` on this machine is small; these floors are what keep a run from
filling it.

**Fix:** if `below_floor` is `true`, free space and run the batch again — the
deferred items are picked up automatically. Do not "fix" a stalled batch by
lowering `RECOVERY_MIN_FREE_GB` towards zero; that is the guard, not the
problem. `batch["deferred"]` in `/api/status` tells you how many items are
waiting on space.

### Symptom — `/api/system` reports the wrong filesystem

**Check:**

```bash
curl -s -H "X-Api-Key: $KEY" $VAULT/api/system | python3 -m json.tool
```

**Cause:** the route reads `DATA_VOLUME_PATH` and falls back to `/` when that
path does not exist. Outside Docker, `DATA_VOLUME_PATH` must point at the real
data directory or you are watching the wrong disk.

**Fix:** set `DATA_VOLUME_PATH` in `.env` to the same place `WORKER_DOWNLOAD_ROOT`
and `RECOVERY_DOWNLOAD_ROOT` live.

---

## The local-folder backup script

### Symptom — a rerun of `scripts/backup_local_folder.py` uploads everything again

**Check:**

```bash
python -m scripts.backup_local_folder --source DIR --state-db FILE --scan-only
```

**Cause:** the state DB is a **cache, not the source of truth**. The real index
is the channel itself: every upload's caption carries the file's relative path,
its size and the first 16 characters of its SHA-256, and chunked files carry a
manifest. Lose or move the state DB and the script has no memory — `--scan-only`
will show every file back at `PENDING`, and unlike the worker's chunk path there
is no channel-side lookup to notice that a copy is already up there.

**Fix:** keep the state DB with the run (on this machine
`local-data/iphone_backup_state.db`) and back it up alongside the source. If it
is already lost, adopt the existing channel with `--channel-id` so at least the
re-upload lands in the right place, and reconcile from the channel's captions —
they are a complete index on their own. Never point the script at the app's own
database; it uses plain `sqlite3` and its own schema.

### Symptom — the script prints `NOT SAFE TO DELETE` and you were about to delete anyway

**Check:** the last line of the report, printed on every exit (clean finish,
FloodWait abort or Ctrl-C).

**Cause:** the script says `SAFE TO DELETE` only when **every** tracked row is
`VERIFIED` — that is, uploaded *and* re-downloaded *and* hash-matched. Anything
less prints the count still outstanding. `0 files tracked` is also not safe: an
empty source directory (an unmounted volume, say) would otherwise look like
success, which is why `main()` aborts in that case.

**Fix:** rerun the script; every row that is not `VERIFIED` is retried, and
`FAILED` rows restart from scratch. Only delete the source once you have seen
`SAFE TO DELETE: N/N files VERIFIED.` Note that `--skip-verify` marks rows
`VERIFIED` **without** the re-download check — a report built from a
`--skip-verify` run proves the upload was accepted, not that the bytes are
intact.

---

## Development environment

### Symptom — `.venv/bin/python -m pip` fails with `No module named pip`

**Check:**

```bash
ls .venv/bin
```

**Cause:** the venv was created by `uv`, which does not install pip inside it.

**Fix:** install through uv instead, which is what the repo's own verification
commands do:

```bash
uv venv --python 3.14 .venv
VIRTUAL_ENV=.venv uv pip install -r requirements.txt -r requirements-dev.txt
```

(`python -m venv .venv` also works and does give you pip; just be consistent.)

### Symptom — tests pass locally but CI fails with a `SyntaxError` or `AttributeError`

**Check:**

```bash
uv venv --python 3.11 /tmp/vault311
VIRTUAL_ENV=/tmp/vault311 uv pip install -r requirements.txt -r requirements-dev.txt
/tmp/vault311/bin/python -m pytest -q
```

**Cause:** the local venv is Python 3.14; `.github/workflows/ci.yml` runs 3.11.
Anything newer than 3.11 — syntax, a stdlib function, a changed default —
passes locally and fails there.

**Fix:** keep the code 3.11-compatible. `ruff.toml` sets
`target-version = "py311"`, and once the 3.11 venv above exists the quickest
syntax check is `/tmp/vault311/bin/python -m compileall -q app scripts tests`.
Run the whole suite on 3.11 before changing anything version-sensitive.

### Symptom — `ruff check .` reports errors you did not introduce

**Check:**

```bash
ruff check . --statistics
cat ruff.toml
```

**Cause:** `ruff.toml` deliberately enables only `E9` (syntax/IO errors) and `F`
(pyflakes: undefined names, unused imports and variables, f-string and format
bugs). It is an error-finder, not a formatter — there is no style, import-order
or line-length rule, so it passes on the codebase unchanged. A new ruff release
occasionally adds a rule to those groups.

**Fix:** fix the finding if it is real. Widening the rule set is a deliberate
decision for the repo owner — do not turn on a style group to make one warning
go away, and do not reformat the codebase.

### Symptom — a test hangs, or fails with `attached to a different loop`

**Check:**

```bash
grep -n 'engine.dispose' tests/conftest.py tests/test_api.py
```

**Cause:** `pytest.ini` sets `asyncio_mode = auto` with **function-scoped** event
loops, and `app/models/database.py` builds the engine at import time as a module
global. A pooled aiosqlite connection opened in one test's loop must not be
reused in the next one's.

**Fix:** use the `clean_db` fixture, which drops and recreates the schema and
disposes the engine afterwards. Any fixture that creates its own loop (the
`client` fixture in `tests/test_api.py`, which calls `asyncio.run`) must dispose
the engine itself before handing over. Tests in `tests/test_db_migrations.py`
dispose in a `finally` for the same reason.

### Symptom — a new model column exists in the code but not in the running database

**Check:**

```bash
pytest tests/test_db_migrations.py -q
```

**Cause:** there is no migration tool. `init_db()` is `create_all` (which only
creates *missing tables*, never missing columns) plus a hand-rolled
`PRAGMA table_info` / `ALTER TABLE ADD COLUMN` pass driven by
`_COLUMN_MIGRATIONS` in `app/models/database.py`.

**Fix:** add the column to the model **and** to `_COLUMN_MIGRATIONS`, and make
it nullable or defaulted — SQLite cannot add a bare `NOT NULL` column to a table
that already has rows. Never rename or drop a column. `tests/test_db_migrations.py`
asserts all three of those properties.

---

## API and dashboard

### Symptom — every `/api/*` call returns 503 `API key is not configured.`

**Check:**

```bash
curl -s -o /dev/null -w '%{http_code}\n' $VAULT/health
grep -c '^API_KEY=' .env
```

**Cause:** `API_KEY` is unset or blank in the environment the app actually
started with. The dependency fails closed — it refuses every request rather
than serving the API unauthenticated. `GET /health` and `GET /` stay open, so a
200 from `/health` with 503s everywhere else confirms it.

**Fix:** set `API_KEY` and restart the app.

### Symptom — every `/api/*` call returns 401 but the key looks right

**Cause:** the header is `X-Api-Key`, and it is compared with
`secrets.compare_digest` against the exact value — no trimming.

**Fix:** `curl -H "X-Api-Key: $KEY" …`. Check for a trailing space or newline in
the `.env` value. In the dashboard, re-paste the key into the key field.

### Symptom — `POST /api/recovery/{scan,run,backfill}` returns 409

**Cause:** `RecoveryService` runs one background task at a time, by design — a
scan and a tidy must not interleave.

**Fix:** wait. `/api/status` → `recovery.running` and `recovery.activity` tell
you what is in flight, and `recovery.batch` its live counters.

### Symptom — `POST /api/recovery/backfill` returns 200 and then nothing happens

**Check:**

```bash
curl -s -H "X-Api-Key: $KEY" $VAULT/api/status \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["recovery"]["last_error"])'
```

**Cause:** every recovery endpoint only *starts* a background task, so the HTTP
response tells you the task was accepted, not that it worked. With no gallery
mirror configured, backfill raises
`No browse channel configured (set BROWSE_CHANNEL_ID).` inside that task; the
guard catches it into `recovery.last_error` and the POST still returns the
snapshot. The same is true of any failure in a scan or a tidy run — always read
`last_error` after starting one.

**Fix:** set `BROWSE_CHANNEL_ID` and restart, or don't call backfill.

### Symptom — a photo shows a stale `updated_at` right after a retry

**Cause:** `updated_at` uses a server-side `onupdate`, so committing expires the
attribute; serializing it without a refresh would trigger a sync lazy load.

**Fix:** `await session.refresh(row)` before serializing a committed row — as
`retry_photo` in `app/api/routes.py` already does. Any new route that commits
and returns the row needs the same.

---

## Known issues

Things that are broken, half-finished or surprising, found while writing the
tests in this pass and **left unfixed on purpose** — this run pins behaviour, it
does not change it. Each is pinned by a test so it cannot drift silently.

### `--keep-going` in `vault_merge.py` only affects hash mismatches

`scripts/vault_merge.py` verifies each part in a loop whose missing-part and
size-mismatch branches `continue`, skipping the
`if problems and not args.keep_going: break` check at the bottom. So those two
problem classes are *always* fully reported, and only a SHA-256 mismatch stops
at the first one. The flag's help text ("Report all verification problems
instead of stopping at the first") is therefore only true for one of three
cases. Nothing is written either way, so this is a reporting quirk, not a data
risk. Pinned by `tests/test_vault_merge_cli.py::test_missing_parts_are_all_listed_even_without_keep_going`.

### `--channel-id` is silently ignored with `--scan-only`

In `scripts/backup_local_folder.py`, `main()` returns from the `--scan-only`
branch *before* the `set_meta(conn, "channel_id", …)` call, so
`--scan-only --channel-id <id>` prints a report and adopts nothing. Adopting an
existing channel requires a real run. Pinned by
`tests/test_backup_reporting.py::test_channel_id_is_ignored_in_scan_only_mode`.

### `get_row()` raises `TypeError` for a path that was never scanned

`get_row` in `scripts/backup_local_folder.py` does
`dict(zip(columns, row))` without checking that `row` is not `None`, so an
unknown `rel_path` raises `TypeError: zip argument #2 must support iteration`
instead of returning `None` or raising something readable. Every caller goes
through `pending_rel_paths()` so this is unreachable in the script's own flow;
it bites anyone calling `process_file()` directly. Pinned by
`tests/test_backup_reporting.py::test_reading_a_row_that_was_never_scanned_raises`.

### Three `RecoveryStatus` values and two columns are dead

`RecoveryStatus.DOWNLOADED`, `.REUPLOADED` and `.DUPLICATE` are still in the
enum and still counted by `GET /api/status`, but nothing in the in-place tidy
produces them — they are left over from the download-and-re-upload design that
`67cb660` replaced. Likewise `recovery_items.sha256` and
`recovery_items.new_tg_message_id` are never written any more, so
`GET /api/recovery/items` always reports them as `null`. They are kept because
old rows may still carry values and columns are never dropped here. Pinned by
`tests/test_recovery_rules.py::test_legacy_statuses_still_exist_but_are_no_longer_produced`.

### `RECOVERY_DELETE_OLD` does nothing

`RecoveryService.delete_old` is stored and never read: the in-place tidy edits
captions and never deletes a message. The variable is still accepted in `.env`
and `docker-compose.yml` for config compatibility, and is documented as
informational in `AGENTS.md`. Setting it to `false` changes nothing, and so does
setting it to `true`.

### `README.md`'s recovery section describes the old design

The "Tidying the existing channel (recovery)" section still describes the
pre-`67cb660` behaviour — downloading every item, flagging duplicates by
SHA-256, re-uploading as a new document and deleting the original. The code does
none of that any more. The section was left byte-for-byte intact (this pass does
not rewrite human prose); the correction is in the generated block at the end of
`README.md`. `AGENTS.md`, `CLAUDE.md` and `docs/REFERENCE.md` describe the
current in-place behaviour correctly.

### What is not covered by tests, and why

- **Real Telegram, MEGA, SFTP and MEGAcmd calls.** Everything is faked at the
  boundary: `FakeClient`/`FakeTelegram` for pyrogram, shell-script stand-ins for
  `mega-ls`/`mega-get`/`mega-rm` in `tests/test_mega_commands.py`, and a
  constructor-level check for `SFTPService` (no `asyncssh.connect` is ever
  reached). FloodWait pacing, `sleep_threshold` behaviour and real host-key
  verification are therefore untested here — they can only be exercised against
  the live services.
- **`app/main.py`'s `lifespan`.** Building it requires a real pyrogram client
  and a successful `telegram_client.start()`. The API tests construct a bare
  `FastAPI()` and set `app.state` by hand instead, so a wiring mistake in
  `lifespan` — a knob parsed but not passed through — would not be caught by the
  suite. That is what the `add-config-knob` skill's checklist is for.
- **`app/static/dashboard.html`.** No browser tests; the dashboard is exercised
  by hand.
- **`deploy/teldrive/`.** A separate compose kit that shares no code with this
  app and is not touched by CI.
<!-- autodoc:end -->
