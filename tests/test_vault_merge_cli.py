"""Characterisation tests for the restore tool the channel's manifests promise.

`scripts/vault_merge.py` is deliberately stdlib-only so it can be copied next
to the downloaded parts on any machine — so it is run here as a subprocess with
the *system* interpreter, not imported. The happy path and two corruption cases
are covered in tests/test_chunking.py; this file pins the refusals, the
manifest contract, and the two path flags.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

MERGE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vault_merge.py"
KIND = "telegram-photo-vault/chunked-file"


def _build(tmp_path: Path, *, parts=(b"first-", b"second", b"-third"), name="orig.bin"):
    """Write a valid chunk set + manifest and return (whole bytes, manifest path)."""
    whole = b"".join(parts)
    count = len(parts)
    chunks = []
    offset = 0
    for index, blob in enumerate(parts, start=1):
        filename = f"{name}.part{index:03d}-of-{count:03d}"
        (tmp_path / filename).write_bytes(blob)
        chunks.append(
            {
                "index": index,
                "filename": filename,
                "offset": offset,
                "size": len(blob),
                "sha256": hashlib.sha256(blob).hexdigest(),
            }
        )
        offset += len(blob)

    manifest = {
        "kind": KIND,
        "manifest_version": 1,
        "original_filename": name,
        "total_size": len(whole),
        "sha256": hashlib.sha256(whole).hexdigest(),
        "chunk_count": count,
        "chunks": chunks,
    }
    manifest_path = tmp_path / f"{name}.manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return whole, manifest_path


def _rewrite(manifest_path: Path, **changes):
    manifest = json.loads(manifest_path.read_text())
    for key, value in changes.items():
        if value is None:
            manifest.pop(key, None)
        else:
            manifest[key] = value
    manifest_path.write_text(json.dumps(manifest))


def _merge(manifest_path: Path, *args):
    return subprocess.run(
        [sys.executable, str(MERGE_SCRIPT), str(manifest_path), *args],
        capture_output=True,
        text=True,
    )


def test_a_valid_set_merges_back_byte_for_byte(tmp_path):
    whole, manifest_path = _build(tmp_path)

    result = _merge(manifest_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "orig.bin").read_bytes() == whole


def test_cat_of_the_parts_is_equivalent_to_the_tool(tmp_path):
    # The documented escape hatch: `LC_ALL=C cat name.part* > name`. Zero-padded
    # names exist precisely so lexicographic order equals numeric order.
    whole, manifest_path = _build(tmp_path)
    parts = sorted(tmp_path.glob("orig.bin.part*"))

    assert b"".join(part.read_bytes() for part in parts) == whole


def test_a_foreign_manifest_is_refused(tmp_path):
    _, manifest_path = _build(tmp_path)
    _rewrite(manifest_path, kind="some-other-tool/archive")

    result = _merge(manifest_path)

    assert result.returncode == 1
    assert "Not a vault chunk manifest" in result.stderr
    assert not (tmp_path / "orig.bin").exists()


@pytest.mark.parametrize("field", ["original_filename", "total_size", "sha256", "chunks"])
def test_every_required_manifest_field_is_checked(tmp_path, field):
    _, manifest_path = _build(tmp_path)
    _rewrite(manifest_path, **{field: None})

    result = _merge(manifest_path)

    assert result.returncode == 1
    assert f"missing required field: {field}" in result.stderr


def test_a_newer_manifest_version_warns_but_still_tries(tmp_path):
    whole, manifest_path = _build(tmp_path)
    _rewrite(manifest_path, manifest_version=2)

    result = _merge(manifest_path)

    assert result.returncode == 0, result.stderr
    assert "manifest_version=2" in result.stderr
    assert (tmp_path / "orig.bin").read_bytes() == whole


def test_a_gap_in_the_chunk_indexes_is_refused(tmp_path):
    _, manifest_path = _build(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["chunks"][2]["index"] = 4  # 1, 2, 4 — part three is missing
    manifest_path.write_text(json.dumps(manifest))

    result = _merge(manifest_path)

    assert result.returncode == 1
    assert "not contiguous" in result.stderr


def test_a_wrong_size_is_caught_without_hashing_the_part(tmp_path):
    _, manifest_path = _build(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["chunks"][1]["size"] += 1
    manifest_path.write_text(json.dumps(manifest))

    result = _merge(manifest_path)

    assert result.returncode == 1
    assert "!= manifest" in result.stderr
    assert not (tmp_path / "orig.bin").exists()


def _corrupt(path: Path) -> None:
    blob = bytearray(path.read_bytes())
    blob[0] ^= 0xFF  # same length, different bytes: size check passes, hash fails
    path.write_bytes(bytes(blob))


def test_hash_verification_stops_at_the_first_mismatch_by_default(tmp_path):
    _, manifest_path = _build(tmp_path)
    _corrupt(tmp_path / "orig.bin.part001-of-003")
    _corrupt(tmp_path / "orig.bin.part002-of-003")

    result = _merge(manifest_path)

    assert result.returncode == 1
    assert result.stderr.count("BAD ") == 1
    assert not (tmp_path / "orig.bin").exists()


def test_keep_going_reports_every_hash_mismatch_at_once(tmp_path):
    _, manifest_path = _build(tmp_path)
    _corrupt(tmp_path / "orig.bin.part001-of-003")
    _corrupt(tmp_path / "orig.bin.part002-of-003")

    result = _merge(manifest_path, "--keep-going")

    assert result.returncode == 1
    assert result.stderr.count("BAD ") == 2
    assert "2 part(s) failed verification; nothing was written." in result.stderr


def test_missing_parts_are_all_listed_even_without_keep_going(tmp_path):
    # Known quirk (see docs/TROUBLESHOOTING.md): the missing-part and
    # size-mismatch branches `continue` past the early-break check, so
    # --keep-going only changes behaviour for SHA-256 mismatches. The full list
    # is the more useful output here, so this is pinned rather than fixed.
    _, manifest_path = _build(tmp_path)
    (tmp_path / "orig.bin.part001-of-003").unlink()
    (tmp_path / "orig.bin.part002-of-003").unlink()

    result = _merge(manifest_path)

    assert result.returncode == 1
    assert result.stderr.count("BAD missing part") == 2
    assert "2 part(s) failed verification; nothing was written." in result.stderr


def test_a_damaged_manifest_file_is_reported_not_traced(tmp_path):
    broken = tmp_path / "orig.bin.manifest.json"
    broken.write_text("{ this is not json")

    result = _merge(broken)

    assert result.returncode == 1
    assert "Cannot read manifest" in result.stderr
    assert "Traceback" not in result.stderr


def test_parts_and_output_can_live_anywhere(tmp_path):
    parts_dir = tmp_path / "downloads"
    parts_dir.mkdir()
    whole, manifest_path = _build(parts_dir)
    moved_manifest = tmp_path / "orig.bin.manifest.json"
    moved_manifest.write_text(manifest_path.read_text())
    manifest_path.unlink()
    output = tmp_path / "restored" / "movie.bin"
    output.parent.mkdir()

    result = _merge(
        moved_manifest, "--parts-dir", str(parts_dir), "--output", str(output)
    )

    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == whole


def test_an_existing_output_is_never_overwritten(tmp_path):
    _, manifest_path = _build(tmp_path)
    (tmp_path / "orig.bin").write_bytes(b"something precious")

    result = _merge(manifest_path)

    assert result.returncode == 1
    assert "refusing to overwrite" in result.stderr
    assert (tmp_path / "orig.bin").read_bytes() == b"something precious"


def test_the_merge_tool_needs_nothing_but_the_standard_library(tmp_path):
    # It must keep working when copied next to the parts on a machine that has
    # never heard of this project.
    result = subprocess.run(
        [sys.executable, "-I", "-c", f"exec(open({str(MERGE_SCRIPT)!r}).read())", "--help"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    assert "--parts-dir" in result.stdout
