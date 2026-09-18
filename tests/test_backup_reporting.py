"""Characterisation tests for what the local-folder backup script tells a human.

This script's whole purpose is to answer one question — "may I now delete the
original folder?" — so the wording of the report is load-bearing, and so is the
refusal to continue when the source scan found nothing (an unmounted volume
must not look like success).
"""
import pytest

from scripts.backup_local_folder import (
    get_meta,
    get_row,
    main,
    open_state_db,
    print_report,
    scan_folder,
    set_meta,
    set_status,
)


@pytest.fixture
def state(tmp_path):
    source = tmp_path / "source"
    (source / "100APPLE").mkdir(parents=True)
    (source / "100APPLE" / "IMG_0001.HEIC").write_bytes(b"a" * 10)
    (source / "100APPLE" / "IMG_0002.MOV").write_bytes(b"b" * 20)
    conn = open_state_db(tmp_path / "state.db")
    scan_folder(conn, source)
    return source, conn


def test_nothing_is_safe_to_delete_while_anything_is_unverified(state, capsys):
    _, conn = state
    set_status(conn, "100APPLE/IMG_0001.HEIC", "VERIFIED")

    print_report(conn)

    out = capsys.readouterr().out
    assert "NOT SAFE TO DELETE: 1/2 file(s) not VERIFIED yet." in out
    assert "PENDING: 1 VERIFIED: 1" in out


def test_the_all_clear_is_given_only_at_one_hundred_percent(state, capsys):
    _, conn = state
    set_status(conn, "100APPLE/IMG_0001.HEIC", "VERIFIED")
    set_status(conn, "100APPLE/IMG_0002.MOV", "VERIFIED")

    print_report(conn)

    assert "SAFE TO DELETE: 2/2 files VERIFIED." in capsys.readouterr().out


def test_an_empty_state_db_is_never_an_all_clear(tmp_path, capsys):
    print_report(open_state_db(tmp_path / "empty.db"))

    out = capsys.readouterr().out
    assert "NOT SAFE TO DELETE: 0 files tracked." in out
    assert "SAFE TO DELETE" not in out.replace("NOT SAFE TO DELETE", "")


def test_failures_are_listed_with_their_reason(state, capsys):
    _, conn = state
    set_status(conn, "100APPLE/IMG_0002.MOV", "FAILED", error="hash mismatch on verify")

    print_report(conn)

    out = capsys.readouterr().out
    assert "Failed files:" in out
    assert "100APPLE/IMG_0002.MOV — hash mismatch on verify" in out


def test_the_channel_is_named_once_it_is_known(state, capsys):
    _, conn = state
    print_report(conn)
    assert "Channel:" not in capsys.readouterr().out

    set_meta(conn, "channel_id", "-1001234567890")
    print_report(conn)
    assert "Channel: -1001234567890" in capsys.readouterr().out


async def test_scan_only_reports_without_opening_a_telegram_client(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.jpg").write_bytes(b"1")

    def _fail():
        raise AssertionError("build_client must not be called in --scan-only mode")

    monkeypatch.setattr("scripts.backup_local_folder.build_client", _fail)

    await main(["--source", str(source), "--state-db", str(tmp_path / "state.db"), "--scan-only"])

    assert "PENDING: 1" in capsys.readouterr().out


async def test_scan_only_does_not_create_the_verify_scratch_directory(
    tmp_path, monkeypatch, capsys
):
    # The default --tmp-verify-dir is /data-rooted; --scan-only must not try to
    # create it just to print counts.
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.jpg").write_bytes(b"1")
    scratch = tmp_path / "tmp-verify"
    monkeypatch.setattr("scripts.backup_local_folder.build_client", lambda: None)

    await main(
        [
            "--source", str(source),
            "--state-db", str(tmp_path / "state.db"),
            "--tmp-verify-dir", str(scratch),
            "--scan-only",
        ]
    )
    capsys.readouterr()

    assert not scratch.exists()


async def test_channel_id_is_ignored_in_scan_only_mode(tmp_path, capsys):
    # Known quirk (see docs/TROUBLESHOOTING.md): main() returns from --scan-only
    # before it seeds meta.channel_id, so adopting a channel needs a real run.
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.jpg").write_bytes(b"1")
    state_db = tmp_path / "state.db"

    await main(
        [
            "--source", str(source),
            "--state-db", str(state_db),
            "--channel-id", "-1001234567890",
            "--scan-only",
        ]
    )
    capsys.readouterr()

    assert get_meta(open_state_db(state_db), "channel_id") is None


async def test_an_empty_source_aborts_instead_of_reporting_success(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "source"
    source.mkdir()  # exists but empty — e.g. a bind mount that did not mount
    monkeypatch.setattr("scripts.backup_local_folder.build_client", lambda: None)

    with pytest.raises(SystemExit, match="No files tracked"):
        await main(["--source", str(source), "--state-db", str(tmp_path / "state.db")])

    assert "NOT SAFE TO DELETE: 0 files tracked." in capsys.readouterr().out


async def test_a_missing_source_directory_aborts_before_touching_the_state_db(tmp_path):
    state_db = tmp_path / "state.db"

    with pytest.raises(SystemExit, match="--source path does not exist"):
        await main(["--source", str(tmp_path / "nope"), "--state-db", str(state_db)])

    assert not state_db.exists()


def test_reading_a_row_that_was_never_scanned_raises(state):
    # Known quirk (see docs/TROUBLESHOOTING.md): get_row() zips the cursor
    # description against a None row rather than returning None.
    _, conn = state
    with pytest.raises(TypeError):
        get_row(conn, "100APPLE/NEVER_SCANNED.HEIC")
