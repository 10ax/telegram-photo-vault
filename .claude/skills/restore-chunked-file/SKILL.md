---
name: restore-chunked-file
description: Use when pulling a file larger than 2 GB back out of the Telegram channel — finding its parts and manifest, verifying every hash, and merging them into the original bytes
---

# Restore a chunked file from the channel

Files over `CHUNK_THRESHOLD` (default 1,950,000,000 bytes) are not in the
channel as one message. They are `name.partNNN-of-MMM` documents plus a small
`name.manifest.json`. The manifest is the **commit marker**: if it is there,
the set is complete.

The channel is deliberately self-describing — you never need this repo's
database, or this repo at all, to get the bytes back.

## 1. Collect the pieces

In Telegram Desktop, search the archive channel for the original filename. You
want, in one directory:

- every `name.partNNN-of-MMM` document
- `name.manifest.json`

The part captions repeat `#chunked #partNNN_of_MMM`, the original filename, the
total size and the first 16 hex characters of the whole-file SHA-256, so a
single part is enough to identify the set you are missing.

## 2. Verify and merge

```bash
python scripts/vault_merge.py name.manifest.json
```

Stdlib only — copy that one file next to the parts on any machine with Python 3
and it works. It checks every part's size and SHA-256 **before** writing
anything, then the whole-file SHA-256 afterwards, and restores the capture
mtime from the manifest.

Useful flags:

```bash
python scripts/vault_merge.py name.manifest.json --keep-going          # list every bad part
python scripts/vault_merge.py name.manifest.json --parts-dir ~/dl      # parts live elsewhere
python scripts/vault_merge.py name.manifest.json --output ~/movie.mp4  # write elsewhere
```

It refuses to overwrite an existing output, and on any verification failure it
writes nothing at all.

## 3. Or merge with no tooling

This must always work, and a test asserts it does:

```bash
LC_ALL=C cat name.part* > name
sha256sum name       # compare against "sha256" in the manifest
```

`LC_ALL=C` matters: the part numbers are zero-padded so that lexicographic
order equals numeric order, and a locale-aware sort can break that.

## Reading the failure output

| output | meaning |
|---|---|
| `BAD missing part: …` | that part was never downloaded, or has a different name |
| `BAD …: size N != manifest M` | a truncated download — fetch it again |
| `BAD …: SHA-256 mismatch` | the bytes are wrong; re-download that one part |
| `Manifest chunk indexes are not contiguous 1..N` | the manifest itself is damaged |
| `Not a vault chunk manifest (kind=…)` | wrong JSON file |
| `Output already exists, refusing to overwrite` | move the old file aside first |

Note: `--keep-going` only changes the SHA-256 path. Missing parts and size
mismatches are always all reported (see `docs/TROUBLESHOOTING.md`, Known
issues).

## If you change the formats

Don't, without a migration — files already in the channel carry the old format
forever, and `vault_merge.py` is the only thing that reads it. The naming,
caption and manifest shape live in `app/services/chunking.py`
(`chunk_name`, `manifest_name`, `build_manifest`, `build_chunk_caption`) and
are the contract described in `docs/video-chunking-design.md`.

```bash
source .venv/bin/activate
pytest tests/test_chunking.py tests/test_vault_merge_cli.py tests/test_chunked_flow.py -q
```

`tests/test_chunked_flow.py` asserts the cat-merge equivalence end to end;
`tests/test_vault_merge_cli.py` runs the real CLI as a subprocess.
