from datetime import datetime, timezone

from app.services import media as server_media

from vault_client import enumerate as ve


def test_client_extension_sets_match_the_servers_media_detection():
    """The client keeps its own copy (stdlib-only on the phone); this fails the
    moment the two drift."""
    assert ve.IMAGE_EXTENSIONS == frozenset(server_media.IMAGE_EXTENSIONS)
    assert ve.VIDEO_EXTENSIONS == frozenset(server_media.VIDEO_EXTENSIONS)


def test_is_media_is_case_insensitive_and_rejects_non_media():
    assert ve.is_media("PXL_1.JPG")
    assert ve.is_media("clip.mp4")
    assert not ve.is_media("notes.txt")
    assert not ve.is_media("raw.dng")


def test_relpath_is_relative_to_sdcard_with_posix_separators(tmp_path):
    path = tmp_path / "DCIM" / "Camera" / "a.jpg"
    assert ve.relpath_for(path, tmp_path) == "DCIM/Camera/a.jpg"


def test_enumerate_skips_hidden_non_media_and_symlinks(tmp_path):
    camera = tmp_path / "DCIM" / "Camera"
    camera.mkdir(parents=True)
    (camera / "keep.jpg").write_bytes(b"x" * 10)
    (camera / ".hidden.jpg").write_bytes(b"x")
    (camera / "notes.txt").write_text("n")
    (camera / "link.jpg").symlink_to(camera / "keep.jpg")

    entries = ve.enumerate_entries([tmp_path / "DCIM"], sdcard=tmp_path)

    assert [e.relpath for e in entries] == ["DCIM/Camera/keep.jpg"]
    assert entries[0].size == 10
    assert entries[0].mtime.tzinfo is not None


def test_entry_to_manifest_carries_a_utc_offset():
    entry = ve.Entry("DCIM/Camera/a.jpg", "a.jpg", 3, datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc))
    assert ve.entry_to_manifest(entry) == {
        "relpath": "DCIM/Camera/a.jpg", "name": "a.jpg", "size": 3,
        "mtime": "2026-07-01T08:00:00+00:00", "sha256": None,
    }
