import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services.chunking import (
    ChunkWindow,
    build_chunk_caption,
    build_manifest,
    chunk_name,
    compute_hashes,
    manifest_name,
    plan_chunks,
)

MERGE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vault_merge.py"


def test_plan_chunks_exact_and_remainder():
    plan = plan_chunks(10, 3)
    assert [(c["index"], c["offset"], c["size"]) for c in plan] == [
        (1, 0, 3),
        (2, 3, 3),
        (3, 6, 3),
        (4, 9, 1),
    ]
    assert [c["size"] for c in plan_chunks(6, 3)] == [3, 3]
    assert plan_chunks(2, 3) == [{"index": 1, "offset": 0, "size": 2}]


def test_plan_chunks_rejects_bad_input():
    with pytest.raises(ValueError):
        plan_chunks(0, 3)
    with pytest.raises(ValueError):
        plan_chunks(3, 0)


def test_chunk_name_padding():
    assert chunk_name("a.mp4", 3, 12) == "a.mp4.part003-of-012"
    assert chunk_name("a.mp4", 3, 1000) == "a.mp4.part0003-of-1000"
    assert manifest_name("a.mp4") == "a.mp4.manifest.json"


def test_compute_hashes_matches_manual(tmp_path):
    data = os.urandom(10_000)
    path = tmp_path / "f.bin"
    path.write_bytes(data)

    whole, chunks = compute_hashes(path, 3_000)
    assert whole == hashlib.sha256(data).hexdigest()
    assert chunks == [
        hashlib.sha256(data[i : i + 3_000]).hexdigest() for i in range(0, 10_000, 3_000)
    ]


def test_chunk_window_reads_and_seeks(tmp_path):
    data = os.urandom(9_000)
    path = tmp_path / "f.bin"
    path.write_bytes(data)

    window = ChunkWindow(path, 3_000, 3_000, "f.bin.part002-of-003")
    assert window.name == "f.bin.part002-of-003"

    # pyrogram's size probe
    assert window.seek(0, os.SEEK_END) == 3_000
    assert window.tell() == 3_000
    window.seek(0)

    assert window.read() == data[3_000:6_000]
    assert window.read() == b""
    window.seek(100)
    assert window.read(10) == data[3_100:3_110]
    window.close()


def test_captions_and_manifest():
    caption = build_chunk_caption(
        "#2024 #06_2024 #2024_06_01",
        index=3,
        count=12,
        original_filename="IMG.mp4",
        total_size=22,
        sha256="abcdef0123456789deadbeef",
    )
    assert "#chunked #part003_of_012" in caption
    assert "sha256=abcdef0123456789" in caption
    assert caption.startswith("#2024 ")

    manifest = build_manifest(
        original_filename="IMG.mp4",
        total_size=22,
        sha256="ff",
        chunk_size=10,
        chunks=[{"index": 1}],
        mega_path="/Camera/IMG.mp4",
        mtime_utc=datetime(2024, 1, 1, tzinfo=timezone.utc),
        capture_datetime=datetime(2024, 1, 1, 12, 0),
        capture_datetime_source="filename",
    )
    assert manifest["kind"] == "telegram-photo-vault/chunked-file"
    assert manifest["manifest_version"] == 1
    assert manifest["chunk_count"] == 1
    assert manifest["source"]["capture_datetime_source"] == "filename"


def _make_chunk_set(tmp_path: Path, size=10_000, chunk_size=3_000):
    data = os.urandom(size)
    plan = plan_chunks(size, chunk_size)
    chunks = []
    for spec in plan:
        name = chunk_name("orig.bin", spec["index"], len(plan))
        blob = data[spec["offset"] : spec["offset"] + spec["size"]]
        (tmp_path / name).write_bytes(blob)
        chunks.append(
            {
                "index": spec["index"],
                "filename": name,
                "offset": spec["offset"],
                "size": spec["size"],
                "sha256": hashlib.sha256(blob).hexdigest(),
            }
        )
    manifest = build_manifest(
        original_filename="orig.bin",
        total_size=size,
        sha256=hashlib.sha256(data).hexdigest(),
        chunk_size=chunk_size,
        chunks=chunks,
        mega_path=None,
        mtime_utc=None,
        capture_datetime=datetime(2020, 1, 2, 3, 4, 5),
        capture_datetime_source="mtime",
    )
    manifest_path = tmp_path / manifest_name("orig.bin")
    manifest_path.write_text(json.dumps(manifest))
    return data, manifest_path


def _run_merge(manifest_path: Path):
    return subprocess.run(
        [sys.executable, str(MERGE_SCRIPT), str(manifest_path)],
        capture_output=True,
        text=True,
    )


def test_vault_merge_roundtrip(tmp_path):
    data, manifest_path = _make_chunk_set(tmp_path)

    result = _run_merge(manifest_path)
    assert result.returncode == 0, result.stderr
    merged = tmp_path / "orig.bin"
    assert merged.read_bytes() == data
    assert "verified" in result.stdout
    # mtime restored from capture_datetime
    assert abs(merged.stat().st_mtime - datetime(2020, 1, 2, 3, 4, 5).timestamp()) < 2

    # refuses to overwrite an existing output
    result = _run_merge(manifest_path)
    assert result.returncode == 1
    assert "refusing to overwrite" in result.stderr


def test_vault_merge_detects_corruption(tmp_path):
    _, manifest_path = _make_chunk_set(tmp_path)
    part = tmp_path / "orig.bin.part002-of-004"
    blob = bytearray(part.read_bytes())
    blob[0] ^= 0xFF
    part.write_bytes(bytes(blob))

    result = _run_merge(manifest_path)
    assert result.returncode == 1
    assert "SHA-256 mismatch" in result.stderr
    assert not (tmp_path / "orig.bin").exists()


def test_vault_merge_detects_missing_part(tmp_path):
    _, manifest_path = _make_chunk_set(tmp_path)
    (tmp_path / "orig.bin.part003-of-004").unlink()

    result = _run_merge(manifest_path)
    assert result.returncode == 1
    assert "missing part" in result.stderr
