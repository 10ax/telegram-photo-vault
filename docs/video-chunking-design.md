# Design: Chunked Upload of Large Videos to Telegram

Status: **Draft / brainstorm** — not implemented.
Scope: files larger than the Telegram MTProto per-file cap (2 GiB standard account,
4 GiB Premium) that the vault pipeline must archive to the private channel.

---

## 0. Problem statement and goals

The pipeline (`app/worker.py`) uploads each MEGA file to a private Telegram channel
as a **document** via `TelegramService.upload_document`. MTProto rejects documents
above 2,147,483,648 bytes (2 GiB) for a standard account and 4 GiB for Premium.
Camera videos routinely exceed this.

Goals, in priority order:

1. **Byte-exact recoverability.** The vault's whole point is that the channel copy
   *is* the original. After merge, `sha256(reassembled) == sha256(original)` must
   hold, provably.
2. **Self-describing channel.** If the local SQLite DB is destroyed, everything
   needed to find, order, verify, and merge chunks must be recoverable from the
   channel alone.
3. **Trivially recoverable without this project's code.** A future human with only
   `bash`, `cat`, and `sha256sum` must be able to reassemble a file.
4. **Crash-safe / resumable.** A crash mid-upload must never produce duplicate or
   orphaned chunks that the worker cannot reconcile; only missing chunks are
   re-uploaded.
5. **Bounded disk usage.** The Docker host should not need 2× the file size free.

Non-goals: making chunks playable in the Telegram client (this is a vault, not a
streaming service); deduplication; encryption (channel is already private; can be
layered later, orthogonally).

---

## 1. Chunking strategies compared

### 1a. Raw byte-range split (dd/split-style, fixed chunk size)

Cut the file into fixed-size byte windows: chunk *i* = bytes
`[i * C, min((i+1) * C, total))` for chunk size `C` (~1.9 GB).

| Aspect | Assessment |
|---|---|
| Merge | Trivial and **byte-exact**: `cat f.part* > f` reproduces the original bit-for-bit. No tooling, no codec knowledge, no re-mux. |
| File-type coverage | Universal — works identically for MP4, MOV, MKV, HEVC, raw camera formats, even non-video blobs. Zero format parsing, zero format bugs. |
| Chunk playability | **None.** Individual chunks are opaque byte ranges (chunk 0 of an MP4 with a leading `moov` atom may half-play; do not rely on it). |
| CPU / memory | Near zero: sequential read + write (or no write at all with a streaming window reader, see §4.3). Hashing is the only CPU cost and is needed anyway. |
| Determinism | Fully deterministic given `(file bytes, C)`. Re-splitting after a crash yields identical chunks with identical hashes — the foundation of idempotent resume (§4.2). |
| Failure modes | Practically none on the split side. Risk concentrates in bookkeeping (ordering, completeness), which the manifest (§2) solves. |

### 1b. Container-aware segmentation (ffmpeg `-f segment` / HLS-style)

`ffmpeg -i in.mp4 -c copy -f segment -segment_time N out%03d.mp4` (or HLS `.ts`
playlists).

| Aspect | Assessment |
|---|---|
| Merge | **Not byte-exact.** Re-muxing rewrites container headers, timestamps, and atom layout; `sha256` of the concat output ≠ original. The "original" is silently replaced by a near-copy — unacceptable for goal 1. Some metadata (edit lists, custom atoms, GPS/maker notes in `udta`) can be dropped outright. |
| Chunk playability | Yes — each segment plays in the Telegram client. This is the *only* advantage. |
| Coverage | Fragile across codecs/containers: keyframe-alignment constraints mean segment sizes are approximate (a segment can overshoot the 2 GiB cap if keyframes are sparse); HEVC-in-MOV, variable-frame-rate phone footage, and exotic camera formats are recurring ffmpeg edge cases. |
| CPU | Low with `-c copy`, but a full ffmpeg dependency enters the image and every ffmpeg bug becomes a data-integrity bug. |
| Verification | You cannot verify the archive against the source hash at all. You'd have to define "equivalence" (stream hashes? frame hashes?) — complex and weaker. |

### 1c. Split archives (zip/7z/rar multi-volume)

`7z a -v1900m out.7z in.mp4` and upload the volumes.

| Aspect | Assessment |
|---|---|
| Merge | Standard tooling (`7z x out.7z.001`), byte-exact after extraction, and the archive format embeds its own CRCs. |
| CPU / disk | Must write all volumes before uploading (2× disk) and, even with `-mx=0` (store, no compression), a full extra read/write pass. No streaming: 7z volumes are produced by the archiver, not derivable independently per-index, so crash-resume means re-running the whole archiving step. |
| Recoverability | Requires 7z/unzip on the recovery machine — heavier than `cat`, though still common. Volume naming (`.001`) is a de-facto standard. |
| Opacity | Adds a layer of indirection: the channel stores archives-of-videos rather than videos, hurting the "self-describing channel" goal (captions/filenames point at `.7z.001`, not the media). |

### Recommendation

**Primary: raw byte-range split (1a).** For a vault, the merge path is the product.
Byte-split is the only option where the merge is (i) byte-exact, (ii) verifiable
against the original's SHA-256, (iii) executable with nothing but `cat`, and
(iv) deterministic enough to make resume/idempotency trivial. Its one weakness —
unplayable chunks — is irrelevant here: nobody watches archive chunks in-client,
and the compressed WebP/preview path already serves the "browse" use case.

**Secondary (optional, additive):** for videos the user actually wants to *watch*
in Telegram, a future enhancement may upload **one extra transcoded preview**
(e.g. 720p H.264 under 2 GiB, sent as media, clearly labeled `#preview`) *in
addition to* the byte-exact chunk set. This gets playability without ever
compromising the archival copy. Container segmentation (1b) as the archival
format is rejected outright; split archives (1c) add cost without adding any
integrity guarantee that the manifest doesn't already provide.

Chunk size: `CHUNK_SIZE = 1_900_000_000` bytes (config: `VAULT_CHUNK_SIZE`).
Rationale: comfortably under the 2,147,483,648-byte hard cap (≈11.5% headroom for
any protocol overhead and for accidental off-by-one bugs), a round decimal number
humans can do math with, and small enough that a mid-chunk FloodWait retry loses
at most ~1.9 GB of transfer. Premium accounts may raise it to `3_900_000_000`,
but the default should assume the standard cap so a lapsed Premium subscription
never bricks the pipeline. With 3-digit part numbering (§3.1) this supports files
up to ~1.9 TB.

---

## 2. Integrity and the manifest

### 2.1 Hashing

- **Per-chunk SHA-256**, computed while the chunk bytes are read for upload
  (single pass, no extra I/O).
- **Whole-file SHA-256**, computed once over the original before splitting
  (or incrementally folded during the same read pass that produces chunks —
  chunk boundaries don't affect a running whole-file digest).

SHA-256 (not MD5/CRC): collision-resistant, universally available
(`sha256sum` ships in coreutils), and cheap relative to a 2 GB network upload.

### 2.2 Manifest schema (v1)

One JSON document per chunked file, name: `<original_filename>.manifest.json`
(e.g. `IMG_2024.mp4.manifest.json`).

```json
{
  "manifest_version": 1,
  "kind": "telegram-photo-vault/chunked-file",
  "original_filename": "IMG_2024.mp4",
  "total_size": 22548578304,
  "sha256": "9f2b6c…whole-file-hex…",
  "chunk_size": 1900000000,
  "chunk_count": 12,
  "chunks": [
    { "index": 1,  "filename": "IMG_2024.mp4.part001-of-012", "offset": 0,            "size": 1900000000, "sha256": "aa11…" },
    { "index": 2,  "filename": "IMG_2024.mp4.part002-of-012", "offset": 1900000000,   "size": 1900000000, "sha256": "bb22…" },
    { "index": 12, "filename": "IMG_2024.mp4.part012-of-012", "offset": 20900000000,  "size": 1648578304, "sha256": "ll12…" }
  ],
  "source": {
    "mega_path": "/Camera/IMG_2024.mp4",
    "mtime_utc": "2024-06-01T14:23:05Z",
    "capture_datetime": "2024-06-01T16:23:05",
    "capture_datetime_source": "exif|mtime"
  },
  "created_utc": "2026-07-04T10:00:00Z",
  "tool": "telegram-photo-vault"
}
```

Notes:
- `index` is 1-based to match the human-facing `partNNN` naming.
- Per-chunk `offset` is redundant (derivable from `chunk_size`) but makes the
  manifest self-checking and lets a reader validate without arithmetic.
- `manifest_version` + `kind` allow schema evolution and let a recovery scan
  distinguish vault manifests from random JSON documents.
- `capture_datetime` reuses the pipeline's existing EXIF/mtime extraction
  (`app/services/telegram.py: _extract_datetime_sync`) so a merge tool can
  restore the file's mtime.

### 2.3 Where the manifest lives (all three, deliberately redundant)

1. **Uploaded to the channel** as a small `.manifest.json` document, **after all
   chunks succeed**. This ordering makes the manifest a **commit marker**: a
   chunk set without a trailing manifest is by definition incomplete/aborted,
   which gives crash-recovery an unambiguous signal. Its caption carries the
   same hashtags as the chunks plus `#manifest`.
2. **Compact metadata in every chunk's caption** (§3.2): part index, part count,
   total size, and the first 16 hex chars of the whole-file SHA-256. Captions are
   capped at 1024 chars so the *full* per-chunk hash list cannot live there for
   large sets — captions carry enough to regroup and sanity-check, the manifest
   carries the authoritative detail.
3. **Mirrored in SQLite** (`upload_chunks` rows + parent columns, §6.1) for the
   worker's own resume logic and for the `/api/status` view.

**DB-loss scenario:** the channel alone suffices. A recovery job (§3.4) scans
channel history for `*.manifest.json` documents, downloads them (they're a few
KB each), and rebuilds the entire chunk index — message IDs are recovered by
matching document `file_name`s against manifest `chunks[].filename`. Even if a
manifest message were deleted, chunk filenames + captions still encode
order/count/size and the whole-file hash prefix, so a merge is still possible
(with weaker per-chunk verification).

---

## 3. Telegram-side conventions

### 3.1 Chunk naming

```
<original_filename>.part<NNN>-of-<MMM>
IMG_2024.mp4.part003-of-012
```

- Zero-padded 3 digits: lexicographic order == numeric order, so shell globs
  concatenate correctly (`LC_ALL=C cat f.part* `), and 999 × 1.9 GB ≈ 1.9 TB
  ceiling is ample for camera footage.
- Embedding `-of-<MMM>` in *every* chunk name makes each file self-describing:
  a lone chunk found in the channel announces how many siblings it has.
- Keeping the full original filename (extension included) as the prefix means a
  plain filename search in the Telegram client finds the whole set, and `cat`'s
  output name is derivable by stripping the suffix.
- The name is set explicitly via the document's `file_name` attribute so it
  survives regardless of the temp file's on-disk name.

### 3.2 Captions

Reuse the existing hashtag scheme from `_format_caption`
(`#YYYY #MM_YYYY #YYYY_MM_DD`, derived from EXIF/mtime of the *original*), then
append chunk metadata:

```
#2024 #06_2024 #2024_06_01
#chunked #part003_of_012
file=IMG_2024.mp4 size=22548578304 sha256=9f2b6c01deadbeef
```

- Same date hashtags on every chunk and on the manifest → the existing
  year/month browsing convention keeps working for chunked files.
- `#chunked` lets a recovery scan cheaply pre-filter.
- The `sha256=` prefix (16 hex chars) lets any human eyeball that two chunks
  belong to the same original even without the manifest.
- Total stays far below the 1024-char caption cap.

### 3.3 Tying chunks together: naming + manifest (not reply chains, not albums)

- **Media groups (albums):** capped at 10 items — a 22 GB file needs 12 chunks;
  disqualified outright, and album items share ordering semantics that add
  nothing over filenames.
- **Reply-to chains** (each chunk replies to the previous or to a header
  message): superficially attractive, but fragile — deleting any message in the
  chain orphans the tail, replies complicate resumable re-uploads (a re-uploaded
  chunk 7 would have to re-thread), and recovery code would need to walk chains
  instead of just reading names. Rejected as the *source of truth*; harmless to
  add later as cosmetic sugar (e.g. manifest replies to chunk 1).
- **Chosen: deterministic naming + trailing manifest.** Order and grouping live
  in the filename; completeness and hashes live in the manifest; the DB is a
  cache. No Telegram feature with deletion- or ordering-fragility is load-bearing.

### 3.4 Recovery / re-discovery job

A `vault-rescan` maintenance command (future work, cheap to build):

1. `client.get_chat_history(channel_id)` (or `search_messages(query=".manifest.json", filter=DOCUMENT)`)
   and collect every document whose `file_name` ends in `.manifest.json`.
2. Download each (KB-sized), validate `kind`/`manifest_version`.
3. For each manifest, locate chunk messages by `file_name` match (a second
   history pass builds a `file_name → message_id` map in one sweep).
4. Rebuild `upload_chunks` / parent rows in SQLite; report chunk sets that are
   missing chunks or missing manifests (aborted uploads → candidates for
   cleanup or re-upload).

Because the manifest is uploaded last, "manifest present" ⇒ "set was complete at
upload time", so the scan can also serve as an integrity audit.

---

## 4. Reliability mechanics

### 4.1 Resumable uploads

Each chunk is an independent DB row with its own status (§6.1). The upload loop:

```
for chunk in chunks where status != UPLOADED (ordered by index):
    read window [offset, offset+size) from the original file
    compute sha256 while reading; verify against manifest row (defense in depth)
    send_document(...); record tg_message_id; status = UPLOADED; commit
upload manifest; record manifest_tg_message_id; parent → TG_UPLOADED
```

A crash between chunks loses nothing; a crash *during* a chunk loses at most
that chunk's transfer. Because byte-split is deterministic, the retry re-reads
the same window and produces byte-identical content — no "version skew" between
attempts is possible while the source file is intact (guard: re-stat size +
mtime of the original before resuming; if changed, fail loudly).

### 4.2 Idempotency after a crash

Crash windows and their resolution:

| Crash point | State on restart | Resolution |
|---|---|---|
| After `send_document` returns, before DB commit | Chunk uploaded to TG, DB row still PENDING | The one true duplicate window. On resume, before re-uploading chunk *i*, query the channel (`search_messages` by exact `file_name`, or `get_messages` around the last known message id) for an existing document named `…partNNN-of-MMM` with matching `document.file_size`; if found, adopt its `message_id` instead of re-sending. Size match + deterministic naming is sufficient in practice; paranoid mode can download-and-hash. Cheap belt-and-braces: commit a `status=UPLOADING` row *before* calling `send_document` so resume knows exactly which index to double-check. |
| Mid-transfer | Nothing in channel (message only exists once fully sent) | Just re-upload. MTProto uploads are part-wise server-side, but Pyrogram does not persist upload sessions across process restarts — treat chunk upload as all-or-nothing. |
| After all chunks, before manifest | Complete chunk set, no manifest | Resume detects all chunks UPLOADED, uploads manifest, done. Channel-side, the missing manifest correctly marks the set incomplete until then. |

The `(photo_id, index)` unique constraint makes DB-side duplication impossible.

### 4.3 Disk-space math and stream-splitting

Let `F` = file size (e.g. 22 GB), `C` = chunk size (1.9 GB).

- **Naive split-everything-first** (`split -b`): peak disk = `2F`. For a 22 GB
  video that's 44 GB of scratch — unacceptable on a small Docker host.
- **One-chunk-at-a-time temp file:** produce chunk *i* to a temp file, upload,
  delete, next. Peak = `F + C` ≈ `F + 1.9 GB`.
- **Streaming window reader (recommended):** Pyrogram's `send_document` accepts
  a seekable `BinaryIO` with a `.name` attribute. A small
  `ChunkWindow(fileobj, offset, length)` wrapper that implements
  `read/seek/tell` relative to the window (and exposes
  `.name = "IMG_2024.mp4.part003-of-012"`) lets Pyrogram stream the byte range
  straight off the original file. Peak disk = `F`. Seekability matters:
  Pyrogram needs `seek(0)` on internal retries, which the wrapper supports
  trivially by re-seeking to `offset`. SHA-256 is folded inside `read()`
  (reset on seek-to-0) so hashing costs no extra pass.

**Recommendation:** implement the streaming window reader as the primary path
(peak disk = `F`, no extra write I/O, one code path for hash+upload). Keep the
temp-file variant as a documented fallback behind a flag
(`VAULT_CHUNK_TEMPFILE=1`) in case a Pyrogram fork/version misbehaves with
non-file inputs — the fallback is ~20 lines and costs only `+C` disk.

### 4.4 FloodWait and rate limits

- Catch `pyrogram.errors.FloodWait` around every `send_document` /
  `search_messages` call: `await asyncio.sleep(e.value + jitter(1–5 s))`, then
  retry the *same* chunk. FloodWait retries must **not** consume
  `retry_count` — they are throttling, not failure.
- Keep the existing `TELEGRAM_UPLOAD_DELAY` (default 5 s) between chunks;
  large sets are exactly the workload that triggers flood control. Consider a
  larger inter-chunk delay (`VAULT_CHUNK_DELAY`, default 10–15 s) since each
  chunk is already a multi-minute upload and the marginal delay is noise.
- A 22 GB file at residential upstream is hours of transfer; the worker's
  per-photo processing loop must not starve other files — see §6.2 (chunk
  uploads advance one chunk per worker visit, interleaving with other work).

### 4.5 par2 parity chunks — worth it?

Arguments for: protects against a *single lost/corrupted chunk* (accidental
message deletion, hypothetical bit-rot) with ~10% overhead; `par2` is standard
tooling.

Arguments against:
- Telegram stores files replicated server-side; documented real-world bit-rot
  on Telegram documents is essentially unheard of — the realistic failure modes
  are *account loss, channel deletion, or ToS enforcement*, and par2 stored in
  the **same channel** protects against none of them.
- par2 creation requires the whole file locally and a full extra read + heavy
  CPU (Reed–Solomon over 22 GB), plus 2+ GB of parity volumes to upload —
  meaningfully slower pipeline for marginal risk reduction.
- Accidental single-message deletion is better mitigated procedurally (private
  channel, single admin) and detectably (rescan audit, §3.4).

**Verdict: not worth it as a default. Skip.** Leave a config hook
(`VAULT_PAR2_REDUNDANCY=0` default) so a paranoid operator can enable
`par2 c -rN` volumes uploaded alongside the chunk set; the manifest gains an
optional `"parity": [...]` array. The far cheaper insurance is: (a) verify
whole-file hash *before* deleting the MEGA source, and (b) rescan audits.

---

## 5. Merge / download UX

### 5.1 Zero-tooling recovery (must be documented in the channel itself)

The manifest's `kind` doc-string and this repo's README should both state:

```bash
# 1. Download all parts + the manifest from the channel (Telegram Desktop is fine)
# 2. Reassemble (zero-padded names sort correctly):
LC_ALL=C cat IMG_2024.mp4.part*-of-012 > IMG_2024.mp4

# 3. Verify against the manifest:
jq -r '"\(.sha256)  \(.original_filename)"' IMG_2024.mp4.manifest.json | sha256sum -c
# → IMG_2024.mp4: OK
```

No project code, no Python, no Telegram API — goal 3 satisfied. (Per-chunk
verification, for narrowing down a bad download, is the same one-liner over
`.chunks[]`.)

### 5.2 `vault-merge` CLI sketch (convenience path)

A small standalone script (`tools/vault_merge.py`), usable with just a Pyrogram
session + channel id:

```
vault-merge --channel -100123456 IMG_2024.mp4
  1. find document "IMG_2024.mp4.manifest.json" in channel history; download; parse
  2. for each chunks[i]: locate message by file_name, download to workdir,
     stream-hash while downloading, compare to chunks[i].sha256
     (already-present local chunks with matching hash are skipped → resumable)
  3. concatenate in index order into IMG_2024.mp4 (or stream-append during
     download to avoid 2x local disk)
  4. verify whole-file sha256 against manifest.sha256
  5. os.utime() the result from source.mtime_utc; print OK + hash
```

Failure UX: name exactly which chunk failed verification and re-download only
that one. The CLI is sugar; §5.1 is the guarantee.

---

## 6. Integration sketch with the existing worker

### 6.1 Schema extension

Parent stays in `photos` (avoid a disruptive rename); add columns + one child table:

```python
class Photo(Base):
    # existing columns...
    total_size:  Mapped[int | None]         # bytes, set at DOWNLOADED
    sha256:      Mapped[str | None]         # whole-file hash (chunked files; optionally all files)
    is_chunked:  Mapped[bool] = mapped_column(default=False)
    manifest_tg_message_id: Mapped[int | None]

class ChunkStatus(str, Enum):
    PENDING = "PENDING"; UPLOADING = "UPLOADING"; UPLOADED = "UPLOADED"; FAILED = "FAILED"

class UploadChunk(Base):
    __tablename__ = "upload_chunks"
    id:            Mapped[int]              # PK
    photo_id:      Mapped[int]              # FK → photos.id, indexed
    index:         Mapped[int]              # 1-based
    offset:        Mapped[int]
    size:          Mapped[int]
    sha256:        Mapped[str | None]
    tg_message_id: Mapped[int | None]
    status:        Mapped[ChunkStatus]
    retry_count / error_log / created_at / updated_at  # mirror Photo conventions
    __table_args__ = (UniqueConstraint("photo_id", "index"),)
```

`Photo.tg_message_id` (Integer) stays pointing at the manifest message for
chunked files — the manifest is the file's "anchor" in the channel. (Adjacent
note: Telegram message ids fit in 32 bits per-chat today, but `BigInteger` is
the safer column type when touching this area.)

### 6.2 State machine: parallel per-chunk machine, one new parent state

Keep the parent flow intact and insert a single new state for large files:

```
PENDING → DOWNLOADED → [small file]  → TG_UPLOADED → COMPRESSED → ODROID_UPLOADED → COMPLETED
                     ↘ [large file] CHUNK_UPLOADING ↗
```

- **Routing decision** in `_handle_downloaded`: after confirming the local file
  exists, `stat().st_size`; if `> VAULT_CHUNK_THRESHOLD`
  (default = `VAULT_CHUNK_SIZE`, i.e. anything that can't ship as one document),
  compute the chunk plan, insert `UploadChunk` rows + whole-file hash, set
  `is_chunked=True`, status → `CHUNK_UPLOADING`. Byte-split is
  format-agnostic, so **size alone routes**; a MIME sniff (extension or
  `python-magic`) is used only to pick caption emoji/tags and to decide the
  §1-secondary "playable preview" later — it is not a correctness input.
- **`_handle_chunk_uploading`** (new step handler): pick the lowest-index
  non-UPLOADED chunk, run the §4.1/§4.2 sequence for *that one chunk*, return.
  One chunk per worker visit keeps the existing
  `_process_photo_by_id`-loop fairness: other photos progress between chunks.
  When no chunks remain, upload the manifest, set `manifest_tg_message_id` and
  `tg_message_id`, status → `TG_UPLOADED` — rejoining the normal pipeline.
- Chunk-level retries live on `UploadChunk.retry_count`; the parent only goes
  `FAILED` when a chunk exhausts retries.
- **Adjacent gap to resolve during implementation:** the current
  `TG_UPLOADED → COMPRESSED` step calls `compress_to_webp`, which is
  Pillow/image-only and will throw on any video (chunked or not). Videos need a
  bypass (skip straight to `ODROID_UPLOADED`-eligible) or a thumbnail-extraction
  variant. That predates chunking but the chunking work will trip over it first.

### 6.3 Config additions

```
VAULT_CHUNK_SIZE=1900000000       # bytes per chunk
VAULT_CHUNK_THRESHOLD=1900000000  # route to chunking above this
VAULT_CHUNK_DELAY=15              # inter-chunk sleep (s), on top of FloodWait handling
VAULT_CHUNK_TEMPFILE=0            # 1 = temp-file fallback instead of streaming window
VAULT_PAR2_REDUNDANCY=0           # % parity; 0 = disabled (recommended)
```

---

## 7. Risks and open questions

1. **Library choice — Pyrogram is unmaintained.** Last upstream release is
   v2.0.106 (2023); the repo is effectively frozen. Active forks exist, notably
   **kurigram (KurimuzonAkuma/pyrogram)**, which tracks current TL layers and
   supports 4 GiB Premium uploads. Recommendation: plan a migration to a
   maintained fork (kurigram is drop-in for the APIs used here) *before*
   building chunking, since chunking multiplies our exposure to upload-path
   bugs; at minimum, pin exact versions and test the `BinaryIO`-input path of
   §4.3 against the chosen fork. This is a project-level decision to surface,
   not one this design silently makes.
2. **"Documents preserve bytes" assumption.** Telegram re-encodes *media*
   (photos sent as photos, videos sent as videos get transcoded/streamable
   variants) but **documents are stored and returned byte-identical** — this is
   long-standing, widely relied-upon behavior, and the existing pipeline
   already depends on it for originals. The design states it as an explicit
   assumption; cheap validation: after the first production chunk upload,
   re-download one chunk and compare hashes (could even be a one-time startup
   self-test). If Telegram ever violated this, the manifest hashes would detect
   it immediately at merge time — the design fails loud, not silent.
3. **Account vs bot limits.** Bot API uploads cap at 50 MB (or 2 GB via a local
   Bot API server) and Premium does not apply to bots. This pipeline uses an
   MTProto *user* session (api_id/api_hash + session string), so the 2 GiB /
   4 GiB-Premium caps apply. Chunk size must be driven by the *account's
   current* tier — default to the 2 GiB-safe 1.9 GB and treat Premium as an
   opt-in override, since Premium can lapse.
4. **Channel storage permanence.** Telegram's "unlimited" cloud storage is
   policy, not contract: no SLA, accounts can be deleted for inactivity
   (self-destruct timer, default 6–12 months without login), and ToS
   enforcement or service shutdown are non-zero risks. The vault should be
   treated as *a* replica, not *the only* replica — which is in tension with
   the pipeline deleting the MEGA source. Cheap mitigations: (a) only delete
   the MEGA source after the manifest is uploaded and (optionally) one chunk
   spot-check passes; (b) periodic `vault-rescan` audit; (c) keep the session
   alive (the worker does, by running).
5. **Caption/name limits.** Captions cap at 1024 chars (2048 Premium) — our
   caption format uses <200. Document `file_name` survives well past the
   client's ~60-char display truncation, but extremely long original filenames
   plus the `.partNNN-of-MMM` suffix should be length-checked (~250-char safety
   cap) with a deterministic truncation rule recorded in the manifest.
6. **Open questions.**
   - Adopt kurigram now or after chunking ships? (Recommend: now.)
   - Should *all* files (small ones too) get a `sha256` column + verification?
     Nearly free during the existing download step and strengthens the whole
     vault, not just chunked files.
   - Delete-source policy: is "manifest uploaded" sufficient to delete from
     MEGA, or should a chunk re-download spot-check gate it? (Cost: one chunk's
     download per large file.)
   - Video WebP-compression gap (§6.2) — fix scope and ordering vs this work.
   - Is the optional playable `#preview` upload (§1 secondary) wanted at all?

---

## Appendix A — Decision summary

| Decision | Choice |
|---|---|
| Split strategy | Raw byte-range split, fixed 1.9 GB chunks |
| Playability | Not a goal; optional separate `#preview` upload later |
| Integrity | SHA-256 per chunk + whole file, single-pass during upload read |
| Manifest | `<name>.manifest.json` uploaded **last** (commit marker) + caption metadata + SQLite mirror |
| Naming | `<name>.part<NNN>-of-<MMM>`, zero-padded, 1-based |
| Grouping | Filenames + manifest; no reply chains or albums as source of truth |
| Disk strategy | Streaming `ChunkWindow` reader (peak = file size); temp-file fallback flag |
| par2 | Off by default; config hook only |
| Merge | `LC_ALL=C cat *.part* > file && sha256sum -c` (no project code needed); `vault-merge` CLI as sugar |
| DB | `upload_chunks` child table + `is_chunked`/`sha256`/`total_size`/`manifest_tg_message_id` on `photos` |
| State machine | New `CHUNK_UPLOADING` parent state between `DOWNLOADED` and `TG_UPLOADED`; per-chunk statuses; one chunk per worker visit |
| Routing | Size threshold only (`VAULT_CHUNK_THRESHOLD`); MIME sniff is cosmetic |
| Library | Flag Pyrogram staleness; recommend kurigram migration before implementation |
