from pathlib import Path

from scripts.backup_local_folder import (
    failed_rows,
    get_meta,
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
