from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.backup_local_folder import (
    failed_rows,
    get_meta,
    get_or_create_channel_id,
    get_row,
    open_state_db,
    pending_rel_paths,
    scan_folder,
    set_meta,
    set_status,
    status_counts,
)


def _make_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "100APPLE").mkdir(parents=True)
    (source / "100APPLE" / "IMG_0001.HEIC").write_bytes(b"a" * 10)
    (source / "100APPLE" / "IMG_0002.MOV").write_bytes(b"b" * 20)
    (source / "101APPLE").mkdir()
    (source / "101APPLE" / "IMG_1000.JPG").write_bytes(b"c" * 30)
    return source


def test_scan_folder_inserts_pending_rows(tmp_path):
    source = _make_source(tmp_path)
    conn = open_state_db(tmp_path / "state.db")

    inserted = scan_folder(conn, source)

    assert inserted == 3
    assert pending_rel_paths(conn) == [
        "100APPLE/IMG_0001.HEIC",
        "100APPLE/IMG_0002.MOV",
        "101APPLE/IMG_1000.JPG",
    ]
    row = get_row(conn, "100APPLE/IMG_0002.MOV")
    assert row["status"] == "PENDING"
    assert row["size"] == 20
    assert row["sha256"] is None


def test_scan_folder_is_idempotent(tmp_path):
    source = _make_source(tmp_path)
    conn = open_state_db(tmp_path / "state.db")
    scan_folder(conn, source)

    second_pass = scan_folder(conn, source)

    assert second_pass == 0
    assert len(pending_rel_paths(conn)) == 3


def test_set_status_updates_fields_and_excludes_verified_from_pending(tmp_path):
    source = _make_source(tmp_path)
    conn = open_state_db(tmp_path / "state.db")
    scan_folder(conn, source)

    set_status(conn, "100APPLE/IMG_0001.HEIC", "VERIFIED", sha256="deadbeef", tg_message_id=42)

    row = get_row(conn, "100APPLE/IMG_0001.HEIC")
    assert row["status"] == "VERIFIED"
    assert row["sha256"] == "deadbeef"
    assert row["tg_message_id"] == 42
    remaining = pending_rel_paths(conn)
    assert "100APPLE/IMG_0001.HEIC" not in remaining
    assert len(remaining) == 2


def test_status_counts_and_failed_rows(tmp_path):
    source = _make_source(tmp_path)
    conn = open_state_db(tmp_path / "state.db")
    scan_folder(conn, source)

    set_status(conn, "100APPLE/IMG_0001.HEIC", "VERIFIED")
    set_status(conn, "100APPLE/IMG_0002.MOV", "FAILED", error="hash mismatch on verify")

    counts = status_counts(conn)
    assert counts["VERIFIED"] == 1
    assert counts["FAILED"] == 1
    assert counts["PENDING"] == 1
    assert failed_rows(conn) == [("100APPLE/IMG_0002.MOV", "hash mismatch on verify")]


def test_meta_roundtrip_and_overwrite(tmp_path):
    conn = open_state_db(tmp_path / "state.db")

    assert get_meta(conn, "channel_id") is None
    set_meta(conn, "channel_id", "-1001234567890")
    assert get_meta(conn, "channel_id") == "-1001234567890"
    set_meta(conn, "channel_id", "-1009999999999")
    assert get_meta(conn, "channel_id") == "-1009999999999"


class FakeChannelClient:
    def __init__(self):
        self.create_channel_calls = []

    async def create_channel(self, title):
        self.create_channel_calls.append(title)
        return SimpleNamespace(id=-1009999999999)


@pytest.mark.asyncio
async def test_get_or_create_channel_id_creates_once(tmp_path):
    conn = open_state_db(tmp_path / "state.db")
    client = FakeChannelClient()

    channel_id = await get_or_create_channel_id(client, conn)

    assert channel_id == -1009999999999
    assert client.create_channel_calls == ["iPhone Backup Archive"]
    assert get_meta(conn, "channel_id") == "-1009999999999"


@pytest.mark.asyncio
async def test_get_or_create_channel_id_reuses_stored_id(tmp_path):
    conn = open_state_db(tmp_path / "state.db")
    set_meta(conn, "channel_id", "-1001111111111")
    client = FakeChannelClient()

    channel_id = await get_or_create_channel_id(client, conn)

    assert channel_id == -1001111111111
    assert client.create_channel_calls == []


from scripts.backup_local_folder import build_caption, upload_single


class FakeUploadService:
    def __init__(self):
        self.calls = []

    async def upload_document(self, file_path, *, caption=None, file_name=None):
        self.calls.append({"file_path": file_path, "caption": caption, "file_name": file_name})
        return SimpleNamespace(id=555)


def test_build_caption_contains_path_size_and_hash_prefix():
    caption = build_caption("106APPLE/IMG_6849.MOV", 2476877121, "a" * 64)

    assert caption == "106APPLE/IMG_6849.MOV\nsize=2476877121 sha256=" + "a" * 16


@pytest.mark.asyncio
async def test_upload_single_calls_service_and_returns_message_id(tmp_path):
    abs_path = tmp_path / "IMG_0001.HEIC"
    abs_path.write_bytes(b"x" * 10)
    service = FakeUploadService()

    message_id = await upload_single(service, abs_path, "100APPLE/IMG_0001.HEIC", 10, "b" * 64)

    assert message_id == 555
    assert len(service.calls) == 1
    call = service.calls[0]
    assert call["file_path"] == abs_path
    assert call["file_name"] == "IMG_0001.HEIC"
    assert call["caption"] == "100APPLE/IMG_0001.HEIC\nsize=10 sha256=" + "b" * 16


import hashlib
import json

from app.services.chunking import compute_hashes
from scripts.backup_local_folder import upload_chunked


class FakeChunkService:
    def __init__(self):
        self.next_id = 700
        self.chunk_payloads = {}
        self.manifest_payload = None
        self.manifest_caption = None
        self.chunk_captions = {}

    async def upload_file_object(self, file_object, caption):
        self.chunk_payloads[file_object.name] = file_object.read()
        self.chunk_captions[file_object.name] = caption
        self.next_id += 1
        return SimpleNamespace(id=self.next_id)

    async def upload_bytes(self, data, *, file_name, caption):
        self.manifest_payload = json.loads(data)
        self.manifest_caption = caption
        self.next_id += 1
        return SimpleNamespace(id=self.next_id)


@pytest.mark.asyncio
async def test_upload_chunked_splits_uploads_and_builds_manifest(tmp_path):
    data = bytes(range(256)) * 40  # 10_240 bytes
    abs_path = tmp_path / "IMG_7023.MOV"
    abs_path.write_bytes(data)
    sha256 = hashlib.sha256(data).hexdigest()
    chunk_size = 4_000
    _, chunk_hashes = compute_hashes(abs_path, chunk_size)
    service = FakeChunkService()

    manifest_message, count, chunk_messages = await upload_chunked(
        service, abs_path, "107APPLE/IMG_7023.MOV", len(data), sha256, chunk_size, chunk_hashes
    )

    assert count == 3
    assert len(chunk_messages) == 3
    assert manifest_message.id == service.next_id
    assert set(service.chunk_payloads) == {
        "IMG_7023.MOV.part001-of-003",
        "IMG_7023.MOV.part002-of-003",
        "IMG_7023.MOV.part003-of-003",
    }
    joined = b"".join(
        service.chunk_payloads[f"IMG_7023.MOV.part{i:03d}-of-003"] for i in (1, 2, 3)
    )
    assert joined == data
    assert service.manifest_payload["sha256"] == sha256
    assert service.manifest_payload["chunk_count"] == 3
    # Per-chunk hashes in the manifest must be real (this is what scripts/vault_merge.py
    # verifies against before it will merge parts back into the original file).
    for spec in service.manifest_payload["chunks"]:
        expected = chunk_hashes[spec["index"] - 1]
        assert spec["sha256"] == expected
        assert spec["sha256"] != ""
    assert "107APPLE/IMG_7023.MOV" in service.chunk_captions["IMG_7023.MOV.part001-of-003"]
    assert "107APPLE/IMG_7023.MOV" in service.manifest_caption
