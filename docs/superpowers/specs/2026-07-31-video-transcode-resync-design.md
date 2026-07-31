# Video Transcode (Tdarr) + Telegram Archive Resync

Date: 2026-07-31. Follow-up to `2026-07-30-iphone-backup-telegram-design.md` — assumes that
migration is fully `VERIFIED` (6916/6916) before any part of this work executes.

## Goal

The iPhone backup folder is 99.74 GB of video (1691 `.MOV` files) out of ~106 GB total,
already HEVC-encoded. Re-encode the worthwhile subset to AV1 via the already-installed,
already-working Tdarr instance on atlas (hardware VAAPI on the Radeon 780M), replacing
the local files in place to reclaim disk space, and bring the already-uploaded Telegram
archive back in sync with the new (smaller, renamed) files.

## Context: why this is a real re-compression, not a free upgrade

Sampled 5 files across the size distribution (the largest at 3.1 GB / 24 min, and four
more from the size-sorted list): **100% already HEVC**, 1080p/1440p, 11-17 Mbps. AV1
re-encoding a file that is already efficiently compressed is generational lossy
re-compression, not a format correction — accepted trade-off, decided with the user.

## Non-goals

- No change to the photo (HEIC/JPG) files — separate, smaller follow-up (see the
  "photo preview" ledger entry from the prior conversation), out of scope here.
- No new upload/verify code. This feature's script only detects drift and cleans up
  stale Telegram content; re-upload of the reset rows happens by re-running the
  existing, already-shipped `scripts/backup_local_folder.py` unchanged.
- No automatic `--apply` — dry-run is the default; irreversible action (deleting real
  Telegram messages) requires an explicit flag, same safety posture as the main app's
  existing recovery-tidy feature (`RECOVERY_DELETE_OLD` / dry-run-by-default pattern).

## Part 1 — Tdarr configuration (manual, not code)

Tdarr is already running on atlas (`/home/tenax/docker/tdarr/docker-compose.yml`),
currently with no library configured, mounting only `odroid-media:/media`.

1. **Bind mount**: add `- /home/tenax/Pictures/iPhone backup:/iphone-backup` (rw) to
   the `tdarr` service's `volumes:` list, then `docker compose up -d` to recreate the
   container with the new mount (existing `./server`/`./configs`/`./logs` bind mounts
   and the DB inside them are untouched by this — only a new volume is added).
2. **Library**: create a new library in the Tdarr web UI (http://192.168.1.9:8265)
   pointed at `/iphone-backup`.
3. **Flow** (built via the UI's node editor, guided step by step when this phase
   starts):
   - Skip (route to success, no transcode) any file under ~15 MB — the Live Photo
     companion clips (2-3 s, already 3-5 MB) aren't worth the processing time for a
     negligible size delta.
   - Skip (idempotency) any file whose video stream is already AV1 — makes re-running
     the flow safe if it's ever paused/resumed.
   - Transcode everything else: `av1_vaapi` (hardware, on the 780M — chosen over
     `libsvtav1` because software AV1 encoding of ~100 GB of video on this CPU would
     plausibly take days rather than hours), audio stream copied (not re-encoded).
   - **Output container: `.mp4`, not `.mov`.** AV1-in-MOV has poor playback
     compatibility; AV1-in-MP4 is the more interoperable pairing. This means the
     transcode **renames** the file (`IMG_7023.MOV` → `IMG_7023.mp4`), which Part 2
     must detect and handle explicitly (see below) — it is not simply "replace this
     file's bytes."
   - Replace the original in place (delete the `.MOV` once the `.mp4` is confirmed
     written) — Tdarr's default in-place behavior; no extra config needed for this
     part, but confirm it in the UI before starting a real run.

**Sequencing constraint (repeated from the design conversation, load-bearing):** do not
start this phase until the current migration reports
`SAFE TO DELETE: 6916/6916 files VERIFIED.` — Tdarr replacing files while
`backup_local_folder.py`'s container still holds the source folder open (even read-only)
for an in-flight file would race the two processes over the same bytes.

## Part 2 — `scripts/resync_transcoded_videos.py`

New script, reusing (importing, not duplicating) helpers already shipped and reviewed
in `scripts/backup_local_folder.py`: `open_state_db`, `get_row`, `set_status`,
`scan_folder`, `get_meta`, `build_client`, and `app/services/telegram.py`'s
`TelegramService` (specifically `find_document_by_name`, not previously used by the
migration script but already implemented and tested in production).

### Two new small helpers added to `backup_local_folder.py` (not duplicated here)

- `verified_rows(conn) -> list[dict]` — same shape/pattern as the existing
  `failed_rows`/`status_counts`, but for `status = 'VERIFIED'`, returning every column
  (`rel_path, size, sha256, is_chunked, chunk_count, tg_message_id,
  manifest_tg_message_id`).
- `delete_row(conn, rel_path: str) -> None` — `DELETE FROM files WHERE rel_path = ?`.
  Needed because a renamed file cannot simply have its status reset (the primary key
  itself must change); the clean way to do that in SQLite is delete-old +
  let `scan_folder` insert-new.

### Change detection

```
VIDEO_EXTENSIONS = {".mov", ".mp4", ".mkv", ".m4v", ".webm"}  # case-insensitive
```

For every `VERIFIED` row (video or not — the check is generic; photo rows simply never
match because Tdarr never touches them):

1. If `source_root/rel_path` still exists **and** its current size equals the stored
   `size` → unchanged, skip.
2. If `source_root/rel_path` still exists but the size differs → **`size_changed`**
   (Tdarr replaced it in place, same name — would only happen if a future flow
   revision keeps the original extension).
3. If `source_root/rel_path` is missing → look for exactly one sibling in the same
   directory with the same filename stem and a *different* extension, where **both**
   the old and the candidate new extension are in `VIDEO_EXTENSIONS`. If found →
   **`renamed`** (old_rel_path → new_rel_path). This extension restriction is the fix
   for a real hazard found during design: iPhone Live Photos pair `IMG_7023.HEIC` +
   `IMG_7023.MOV` under the **same stem** — without restricting to video extensions,
   a missing `.MOV` would wrongly match the unrelated sibling `.HEIC` photo as if it
   were "the same file renamed."
4. If `source_root/rel_path` is missing and no matching video sibling exists →
   **`orphaned`** — report only, never acted on automatically (this would mean
   something deleted the file outright, not Tdarr's expected rename-or-replace
   behavior; surfacing it beats guessing).

### Dry run (default) — `python -m scripts.resync_transcoded_videos`

Prints one line per detected `size_changed` or `renamed` row (old name, old size, new
name if renamed, new size) plus a summary count, and a separate list of any `orphaned`
rows as warnings. Touches the state DB and the filesystem read-only. Exits 0.

### Apply — `python -m scripts.resync_transcoded_videos --apply`

For every `size_changed` or `renamed` row from the same detection pass (`orphaned` rows
are never acted on, in either mode):

1. **Delete old Telegram content:**
   - `is_chunked = 0`: `await client.delete_messages(channel_id, [tg_message_id])`.
   - `is_chunked = 1`: reconstruct the expected old chunk filenames from the stored
     `chunk_count` and the **old** `rel_path`'s basename (`chunk_name(old_base_name, i,
     chunk_count)` for `i` in `1..chunk_count`, reusing `app/services/chunking.py`'s
     existing `chunk_name` — same function the original upload used, so the names are
     guaranteed to match), then `await service.find_document_by_name(name)` per part
     to recover each message (this is *why* `find_document_by_name` — already written,
     already tested — is reused here instead of writing a new lookup: individual chunk
     message ids were deliberately not persisted in Task 4/6, and this is the one
     place that decision needs to be revisited-by-lookup rather than by schema
     change). Delete all found chunk messages plus `manifest_tg_message_id` in one
     `delete_messages` call.
2. **Reset local state:** `delete_row(conn, old_rel_path)`.
3. After processing every detected change, call `scan_folder(conn, source_root)` once
   — inserts the new-named/resized files as fresh `PENDING` rows.

No upload happens in this script. The user re-runs the same
`docker compose run ... -m scripts.backup_local_folder --channel-id <id>` command used
for the original migration; it picks up the new `PENDING` rows through the unchanged,
already-reviewed pipeline (hash → upload/chunk → verify), sized against the *new*
(likely smaller) files — so a file that needed chunking before may now upload as a
single document, and that decision is made by the existing threshold check with zero
new code.

### Safety notes carried over from the original design's review history

- A `find_document_by_name` miss (chunk message not found — e.g. channel history search
  limits, or a part that failed to upload originally and was never actually present)
  must not silently proceed to delete an incomplete set and orphan the rest — report it
  as a failure for that row and leave its DB row untouched (don't `delete_row`) rather
  than deleting a partial old set and losing traceability of what's still live in the
  channel. Skip the row and continue with the others; nothing about later rows depends
  on this one.
- Deleting Telegram messages is irreversible from this side (no undo). The dry-run
  default is the safeguard; there is no additional confirmation prompt inside `--apply`
  itself — the explicit flag *is* the confirmation, matching how `--scan-only` /
  `--channel-id` were designed in the parent script.

## Testing

Unit tests with fakes, no real Telegram calls, following the existing suite's
conventions (`tests/test_backup_local_folder.py`'s `FakeClient`/`FakeService` style):

- `verified_rows` / `delete_row` additions to `backup_local_folder.py`: same pattern as
  existing `failed_rows`/`status_counts` tests.
- Change detection: unchanged / `size_changed` / `renamed` / `orphaned`, **including the
  Live-Photo HEIC+MOV same-stem case as an explicit negative test** (must NOT be
  classified as `renamed`).
- Dry run: produces a report and performs zero mutations (no `delete_row` calls, no
  Telegram calls) — assert the fake client records zero `delete_messages` calls.
- Apply, non-chunked: deletes the right single message, resets state, rescans.
- Apply, chunked: `find_document_by_name` called once per expected chunk filename with
  the exact reconstructed names, all found messages + manifest deleted together, state
  reset, rescans.
- Apply, chunked with a missing chunk (search miss): row is skipped (no deletion, no
  `delete_row`), reported as a failure, other rows in the same run are unaffected.

## Open risk, explicitly accepted

`find_document_by_name`'s channel search (per its existing docstring in
`app/services/telegram.py`) is best-effort and has a result-count limit; for a channel
with thousands of messages, a very old chunk part could in principle not be found even
though it exists. Accepted per the safety note above (skip + report rather than
partial-delete) — this is a pre-existing characteristic of reused, already-shipped
production code, not something this feature introduces.
