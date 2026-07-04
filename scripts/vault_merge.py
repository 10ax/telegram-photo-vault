#!/usr/bin/env python3
"""Merge and verify a chunked telegram-photo-vault file from its manifest.

Standalone: Python 3 standard library only, no project imports. Download the
`.manifest.json` and all `.partNNN-of-MMM` documents from the channel into one
directory, then:

    python vault_merge.py IMG_2024.mp4.manifest.json

Without this script the merge is still possible (by design):

    LC_ALL=C cat IMG_2024.mp4.part* > IMG_2024.mp4
    sha256sum IMG_2024.mp4   # compare against "sha256" in the manifest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

EXPECTED_KIND = "telegram-photo-vault/chunked-file"
READ_BLOCK = 4 * 1024 * 1024


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(READ_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"Cannot read manifest {path}: {exc}")

    if manifest.get("kind") != EXPECTED_KIND:
        fail(f"Not a vault chunk manifest (kind={manifest.get('kind')!r})")
    if manifest.get("manifest_version") != 1:
        print(
            f"WARNING: manifest_version={manifest.get('manifest_version')} "
            "(this tool implements v1); proceeding.",
            file=sys.stderr,
        )
    for field in ("original_filename", "total_size", "sha256", "chunks"):
        if field not in manifest:
            fail(f"Manifest is missing required field: {field}")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("manifest", type=Path, help="Path to the .manifest.json file")
    parser.add_argument(
        "--parts-dir",
        type=Path,
        default=None,
        help="Directory containing the .part files (default: the manifest's directory)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output file (default: original_filename next to the parts)",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="Report all verification problems instead of stopping at the first",
    )
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    parts_dir = args.parts_dir or args.manifest.parent
    output = args.output or parts_dir / manifest["original_filename"]

    chunks = sorted(manifest["chunks"], key=lambda chunk: chunk["index"])
    expected_indexes = list(range(1, len(chunks) + 1))
    if [chunk["index"] for chunk in chunks] != expected_indexes:
        fail(f"Manifest chunk indexes are not contiguous 1..{len(chunks)}")

    # Verify every part before writing anything.
    problems = []
    for chunk in chunks:
        part_path = parts_dir / chunk["filename"]
        if not part_path.is_file():
            problems.append(f"missing part: {part_path}")
            continue
        actual_size = part_path.stat().st_size
        if actual_size != chunk["size"]:
            problems.append(
                f"{chunk['filename']}: size {actual_size} != manifest {chunk['size']}"
            )
            continue
        actual_sha = sha256_file(part_path)
        if actual_sha != chunk["sha256"]:
            problems.append(f"{chunk['filename']}: SHA-256 mismatch")
        else:
            print(f"ok  {chunk['filename']}")
        if problems and not args.keep_going:
            break

    if problems:
        for problem in problems:
            print(f"BAD {problem}", file=sys.stderr)
        fail(f"{len(problems)} part(s) failed verification; nothing was written.")

    if output.exists():
        fail(f"Output already exists, refusing to overwrite: {output}")

    total_written = 0
    with open(output, "wb") as target:
        for chunk in chunks:
            with open(parts_dir / chunk["filename"], "rb") as source:
                for block in iter(lambda: source.read(READ_BLOCK), b""):
                    target.write(block)
                    total_written += len(block)

    if total_written != manifest["total_size"]:
        output.unlink(missing_ok=True)
        fail(f"Wrote {total_written} bytes, manifest says {manifest['total_size']}")

    final_sha = sha256_file(output)
    if final_sha != manifest["sha256"]:
        output.unlink(missing_ok=True)
        fail("Whole-file SHA-256 mismatch after merge.")

    source_info = manifest.get("source") or {}
    restored_mtime = source_info.get("capture_datetime") or source_info.get("mtime_utc")
    if restored_mtime:
        try:
            timestamp = datetime.fromisoformat(restored_mtime).timestamp()
            os.utime(output, (timestamp, timestamp))
        except (ValueError, OSError) as exc:
            print(f"WARNING: could not restore mtime: {exc}", file=sys.stderr)

    print(f"\nmerged  {output} ({total_written} bytes)")
    print(f"sha256  {final_sha} (verified)")


if __name__ == "__main__":
    main()
