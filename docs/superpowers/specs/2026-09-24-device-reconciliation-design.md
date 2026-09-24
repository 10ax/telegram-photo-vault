# Device reconciliation: which local files are safe to delete

Date: 2026-09-24. Second spec in the catalog effort.

**Depends on Part 1 of `2026-09-21-catalog-and-immich-bridge-design.md`** — the
`catalog_items` model, the multi-channel scan and the provenance match — and on nothing
else in it. EXIF enrichment, the gallery export and Immich are not on this critical path.

## Goal

Answer, for a file sitting on a phone, the only question that matters before deleting it:
*are these bytes already in the archive?* And answer it with a verdict the owner can act
on in bulk without inspecting files one by one.

The archive already holds the bytes. What is missing is the reverse lookup — from a local
file to the message that preserves it — and a protocol that lets any client ask for it.

## Context: what the measurements showed

Measured against `local-data/telegram_photo_vault.db`, not inferred.

**The app's own database cannot answer the question.**

```
photos rows                               3,194
  COMPLETED                               3,101
  SKIPPED (never archived at all)            93
rows carrying sha256 or total_size            1     the single chunked file
```

`Photo` has `sha256` and `total_size` columns, but the worker only populates them on the
chunked path (`_prepare_chunks`, `app/worker.py:363`). For every normal upload the
database records `mega_path` and a message id and nothing about the content. A local file
cannot be compared against that.

**The channel can.** The existing recovery scan already holds what is needed:

```
recovery_items                           18,294 messages   217 GB
  file_size present                      18,294  (100%)
  media_kind = document                  12,793  — file_name present on all of them
  media_kind = photo                      3,201  — no file_name: native mirrors, not archives
```

**Name plus exact size is a sound content key on this corpus.** Grouping the scan by file
name:

```
names appearing more than once            1,246 groups
  same name, same size (harmless dup)     1,236
  same name, different size               10        of 12,793 documents = 0.08%
```

Pixel file names embed a millisecond timestamp (`PXL_20260713_115033830.jpg`) and MEGA's
camera upload preserves them — verified, every `mega_path` in the database is such a name.
A wrong match needs a reused name *and* a byte-exact size collision.

**Hashes are not available where they would be needed most.** Of the 3,101 files the
worker archived, the number with a SHA-256 anywhere in the vault is **0**. The 6,198
hashed `PXL_*` rows all come from the earlier migration era and do not overlap. A
hash-only policy would declare nothing on the Pixel deletable until a verification pass
had run.

**A stale index reads exactly like a missing file.** 3,039 of the 3,101 archived files are
findable by name in the scan; the 62 that are not are all dated after 2026-07-07, which is
the date of the last scan. Nothing was lost — the index was simply older than the files.
This is the failure mode the design has to make structurally impossible.

## Non-goals

- **No deletion by this repo, anywhere.** The server emits verdicts. Execution belongs to
  the client, dry-run by default, as with every destructive path in this codebase.
- **No client.** Which surface enumerates the phone — adb, a Termux agent, a native
  Android app — is deliberately deferred. The manifest protocol is the contract that makes
  the choice reversible, and it is specified here so any of them can be written later.
- **No ingest path.** The `NOT_ARCHIVED` bucket tells the owner that MEGA's camera upload
  has a gap. Closing that gap by uploading from the device is a separate feature.
- **No second index.** `catalog_items` is the lookup surface. This spec adds no table that
  describes the channel.
- **No fuzzy matching.** No name normalisation, no date heuristics, no nearest-size
  matching. Determinism is a requirement: the same inventory against the same catalog
  yields the same verdicts.
- **No change to the on-channel formats.**

## Part 1 — an amendment to the catalog scan

`catalog_items` carries `is_chunked` and `manifest_tg_message_id`, but the catalog spec
does not say where such a row comes from, and the traversal it reuses
(`RecoveryService._is_vault_artifact`, `app/services/recovery.py:247`) discards
`.partNNN-of-MMM` **and** `.manifest.json`. Left as is, the catalog would be blind to every
file above 2 GB — the files whose loss would cost most.

The amendment: the scan keeps discarding chunk parts, and stops discarding manifests. On a
`.manifest.json` it fetches the object, which is a few KB, and writes **one row for the
original file**:

| catalog_items column | from the manifest |
|---|---|
| `file_name` | `original_filename` |
| `file_size` | `total_size` |
| `sha256` | `sha256` (whole file) |
| `is_chunked` | `true` |
| `manifest_tg_message_id` | the manifest's own message id |

Chunked files then look exactly like ordinary documents to the lookup, and they arrive
with the strongest identity available — tier A, for free.

Completeness needs no extra bookkeeping. The manifest is, by the documented contract, the
last object uploaded for a set: its presence already means every part landed. No manifest,
no row, and the original is correctly invisible to the lookup.

## Part 2 — the verdict engine

`app/services/reconcile.py`. It touches no network and knows nothing about Telegram: it
receives a list of dictionaries and queries the database. That boundary is what makes it
testable with pure fixtures.

### Evidence policy

A match counts as proof only against a channel whose `role` is `archive`. The browse
channel holds native, Telegram-recompressed copies; treating one as evidence that the
original is safe would authorise deleting an original in exchange for a degraded mirror.
For the same reason a `media_kind = 'photo'` row is never proof, in any channel.

### Order of evaluation

**Pipeline overlay first**, by basename against `photos`:

| `photos.status` | verdict | reason |
|---|---|---|
| `PENDING`, `DOWNLOADED`, `CHUNK_UPLOADING`, `TG_UPLOADED`, `COMPRESSED`, `ODROID_UPLOADED` | `IN_FLIGHT` | the bytes are not in the channel yet |
| `FAILED` | `NOT_ARCHIVED` | `pipeline_failed` — needs intervention, not patience |
| `SKIPPED` | `NOT_ARCHIVED` | `unsupported_type` — the 93 files that exist in `photos` and were never archived |
| `COMPLETED` | does not decide | corroboration, not proof |
| no row | does not decide | most of a phone has never been through this pipeline |

**Then the catalog**, in descending strength:

| tier | condition | verdict |
|---|---|---|
| A | local `sha256` equals `catalog_items.sha256` | `ARCHIVED` |
| A (negative) | local `sha256` **differs** from a known candidate `sha256` | that candidate is disqualified from every tier below |
| B | `file_name` and `file_size` both exact | `ARCHIVED` |
| C | name matches, size differs | `AMBIGUOUS` (`size_mismatch`) |
| C | name matches only case-insensitively | `AMBIGUOUS` (`case_only_match`) |
| A- | partial fingerprint and size both equal (see below) | `ARCHIVED` |
| C | name and size match, but the candidate's known hash differs | `AMBIGUOUS` (`hash_mismatch`) |
| — | no name match | `NOT_ARCHIVED` |

A hash is not only a promotion. When the catalog knows a candidate's content hash and it
disagrees with the local file's, that is **disproof**, and disproof outranks the metadata
inference below it: the name and the size may still match exactly while the bytes differ, and
archiving on that evidence would delete a file the archive does not hold. The candidate is
disqualified rather than merely not-promoted, and the entry is reported as `hash_mismatch` with
the message to look at — same name, same size, different content is exactly what a human should
see.

`/sdcard` is case-insensitive and Telegram is not, which is why a case-only match is
reported rather than trusted.

When `photos` says `COMPLETED` and the catalog has nothing, the verdict is `AMBIGUOUS`
with reason `completed_but_absent_from_catalog`. That is the honest reading of the 62
files in the measurements: it points at a rescan instead of lying in either direction.

### The freshness rule

> An entry whose `mtime` is newer than the newest `message_date` scanned for the
> archive channels **cannot** be `ARCHIVED`. It becomes `IN_FLIGHT`, reason
> `catalog_older_than_file`.

This is the most important rule in the design. A photo taken after the last scan cannot be
declared archived even by coincidence, so the stale-index failure costs a rescan rather
than a photo. Every response carries the catalog's age and newest scanned date so the
client never has to guess.

### Resolving an AMBIGUOUS entry

On demand and never in bulk: `TelegramService.partial_fingerprint(message_id)` streams the
first and last 256 KB of the archived copy via ranged download and hashes them; the client
sends the same partial hash for its local file. Equal fingerprints plus equal size promote
the entry to `ARCHIVED` at tier A-, the one tier reachable only by an explicit request. On this corpus the whole ambiguous set is small, and
even the 21 files above 1 GB cost 10 MB in total to settle.

## Part 3 — the manifest protocol

The public contract, documented in `docs/REFERENCE.md` as a stable format because every
future client implements it. One entry:

```json
{"relpath": "DCIM/Camera/PXL_20260713_115033830.jpg",
 "name": "PXL_20260713_115033830.jpg",
 "size": 3412887,
 "mtime": "2026-07-13T11:50:33Z",
 "sha256": null}
```

`sha256` is optional: supplying it buys tier A, omitting it falls back to tier B. One
verdict comes back per entry, carrying `verdict`, `tier`, `reason`, and the
`tg_message_id` that holds the bytes when there is one.

Five new endpoints plus `POST /api/catalog/scan`, which the catalog spec already owns and
is listed only to show the whole surface. All behind `require_api_key`, all following the
posture of `RecoveryService` — one background task per call, `409` while busy:

```
POST /api/catalog/scan             {full: bool}      (catalog spec; incremental resume)
GET  /api/catalog/freshness                          newest scanned date, age, coverage
GET  /api/vault/lookup?name=&size=                   single verdict, for debugging
POST /api/devices/{id}/reconcile   {entries:[...]}   → {snapshot_id, summary, entries}
GET  /api/devices/{id}/snapshot                      last summary + actionable findings
POST /api/devices/{id}/deletions   {deleted:[...]}   client declares what it removed
```

About 600 KB for 4,800 entries, comfortably a single POST. To make library size a
non-issue, `reconcile` accepts a continuation `snapshot_id`: a client may split its
inventory across several calls against one snapshot, and `RECONCILE_MAX_ENTRIES` bounds
each call with a clear `413` rather than a timeout.

## Part 4 — what the server remembers

Three new tables. Nothing here describes the channel; that is the catalog's job.

```
device_snapshot    id, device_id, taken_at, completed_at,
                   total_files, total_bytes, per-verdict counts and bytes

device_finding     snapshot_id, relpath, name, size, verdict, tier, reason
                   only non-ARCHIVED rows — the few hundred that need a decision

deletion_audit     device_id, relpath, name, size, verdict_tier,
                   channel_id, tg_message_id, deleted_at
```

The `ARCHIVED` list is the bulk and is never stored: it goes back to the client in the
response, and it is the part nobody would ever query again — except, once deleted, through
`deletion_audit`. That table has no foreign key to the snapshot so it outlives snapshot
pruning, and it makes every deletion reconstructible: which message holds the bytes of the
file that is gone.

New columns are additive, nullable or defaulted, per the repo's no-migration-tool rule.

## New configuration

Both follow the five-edit rule covered by the `add-config-knob` skill.

| variable | default | purpose |
|---|---|---|
| `RECONCILE_MAX_ENTRIES` | `10000` | entries accepted per `reconcile` call |
| `RECONCILE_FINGERPRINT_BYTES` | `262144` | bytes read per side (head and tail) of an ambiguous item |

## Testing

House style: hand-written fakes duck-typing only what is under test, no `unittest.mock`,
`tmp_path` wherever the filesystem is touched, the `clean_db` fixture, no network.

- `test_catalog_manifest_rows.py` — a `.manifest.json` in a fake history yields one row for
  the original with its total size and whole-file hash; chunk parts yield none; a set whose
  manifest is absent is invisible to the lookup.
- `test_reconcile_rules.py` — the whole verdict table as cases: each tier, the case-only
  match, `SKIPPED` and `FAILED` overlays, `completed_but_absent_from_catalog`, a mirror-role
  match refused as evidence, a native `photo` row refused as evidence, and the freshness
  rule. Pure, no Telegram fake needed at all.
- `test_reconcile_api.py` — 401, 503, 409, 413, continuation across several calls against
  one `snapshot_id`, and the deletion audit.
- `test_partial_fingerprint.py` — head and tail ranges requested and hashed, with a fake
  client returning known bytes.
- `test_db_migrations.py` (extended) — the three tables created on a legacy database.

Verified with the four contract commands: `pytest -q`, `ruff check .`,
`python -m compileall -q app scripts`.

## Known limitations, stated rather than hidden

- **Images present only as native `photo` messages are invisible to the lookup** and come
  back `NOT_ARCHIVED`. There are 3,201 such messages. The verdict is wrong in the safe
  direction: the owner keeps a file that may be redundant.
- **A real scan of 18,294 messages, and floodwait pacing, cannot be covered by the suite.**
  It belongs in `docs/TROUBLESHOOTING.md` under *Known issues*, as the repo already does.
- **Tier B is a metadata match, not a proof of identity.** Measured residual risk on this
  corpus: 10 name groups out of 12,793 hold differing sizes, and none of them would by
  itself cause a wrong deletion. The hash tier and the fingerprint path exist for anyone
  who wants certainty on a given file.
