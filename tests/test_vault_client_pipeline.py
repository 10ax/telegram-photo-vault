from datetime import datetime, timezone
import os

import pytest

from vault_client import pipeline
from vault_client.config import Config


class FakeApi:
    def __init__(self, freshness, verdicts, verify_result=None):
        self._freshness = freshness
        self._verdicts = verdicts
        self._verify_result = verify_result or {}
        self.verify_calls = []
        self.deletion_calls = []

    def freshness(self):
        return self._freshness

    def reconcile_all(self, device_id, entries, *, chunk_size=2000, taken_at=None):
        return list(self._verdicts)

    def verify(self, **kwargs):
        self.verify_calls.append(kwargs)
        return self._verify_result

    def deletions(self, device_id, records):
        self.deletion_calls.append(records)
        return {"recorded": len(records)}


FRESH = {"archive_rows": 5, "frontier": "2026-10-01T00:00:00+00:00", "fingerprint_window_bytes": 262144}


def _config(tmp_path) -> Config:
    return Config(server="http://x", device_id="pixel", api_key="k",
                  roots=(tmp_path / "DCIM",), report_dir=tmp_path)


def _verdict(name, verdict, **extra):
    base = {"relpath": f"DCIM/{name}", "name": name, "verdict": verdict, "tier": None,
            "reason": None, "channel_id": None, "tg_message_id": None}
    base.update(extra)
    return base


def test_a_report_write_failure_is_best_effort(tmp_path):
    (tmp_path / "DCIM").mkdir()
    (tmp_path / "DCIM" / "a.jpg").write_bytes(b"x" * 4)
    (tmp_path / "blocked").write_text("not a directory")
    config = Config(server="http://x", device_id="pixel", api_key="k",
                    roots=(tmp_path / "DCIM",), report_dir=tmp_path / "blocked")
    verdicts = [_verdict("a.jpg", "ARCHIVED", tier="NAME_SIZE", channel_id=-1, tg_message_id=1)]
    api = FakeApi(FRESH, verdicts)
    calls = []

    result = pipeline.run(config, sdcard=tmp_path, api=api, dry_run=True,
                          yes=True, confirm=lambda _: True, out=calls.append)

    assert (tmp_path / "DCIM" / "a.jpg").exists()
    assert result.deleted == []
    assert any(c.startswith("warning: could not write report") for c in calls)


def test_partition_splits_verdicts_by_value():
    parts = pipeline.partition([
        _verdict("a", "ARCHIVED"), _verdict("b", "IN_FLIGHT"),
        _verdict("c", "AMBIGUOUS"), _verdict("d", "NOT_ARCHIVED"),
    ])
    assert [v["name"] for v in parts["ARCHIVED"]] == ["a"]
    assert [v["name"] for v in parts["IN_FLIGHT"]] == ["b"]
    assert [v["name"] for v in parts["AMBIGUOUS"]] == ["c"]
    assert [v["name"] for v in parts["NOT_ARCHIVED"]] == ["d"]


def test_an_ambiguous_entry_is_promoted_when_verify_matches(tmp_path):
    (tmp_path / "DCIM").mkdir()
    (tmp_path / "DCIM" / "c.jpg").write_bytes(b"hi")
    verdicts = [_verdict("c.jpg", "AMBIGUOUS", reason="case_only_match", channel_id=-1, tg_message_id=9)]
    api = FakeApi(FRESH, verdicts, verify_result={"match": True, "archived_file_size": 2})

    result = pipeline.run(_config(tmp_path), sdcard=tmp_path, api=api, dry_run=True,
                          yes=False, confirm=lambda _: False)

    assert [d["name"] for d in result.deletable] == ["c.jpg"]
    assert result.deletable[0]["tier"] == "FINGERPRINT"
    assert api.verify_calls[0]["tg_message_id"] == 9


def test_an_unmatched_ambiguous_entry_is_kept(tmp_path):
    (tmp_path / "DCIM").mkdir()
    (tmp_path / "DCIM" / "c.jpg").write_bytes(b"hi")
    verdicts = [_verdict("c.jpg", "AMBIGUOUS", reason="size_mismatch", channel_id=-1, tg_message_id=9)]
    api = FakeApi(FRESH, verdicts, verify_result={"match": False, "archived_file_size": 99})

    result = pipeline.run(_config(tmp_path), sdcard=tmp_path, api=api, dry_run=True,
                          yes=False, confirm=lambda _: False)

    assert result.deletable == []


def test_an_unscanned_catalog_aborts_before_any_verdict(tmp_path):
    api = FakeApi({"archive_rows": 0, "frontier": None, "fingerprint_window_bytes": 262144}, [])
    with pytest.raises(pipeline.PreconditionError):
        pipeline.run(_config(tmp_path), sdcard=tmp_path, api=api, dry_run=True,
                     yes=False, confirm=lambda _: False)


def test_dry_run_never_deletes_even_when_confirmed(tmp_path):
    (tmp_path / "DCIM").mkdir()
    (tmp_path / "DCIM" / "a.jpg").write_bytes(b"x" * 4)
    verdicts = [_verdict("a.jpg", "ARCHIVED", tier="NAME_SIZE", channel_id=-1, tg_message_id=1)]
    api = FakeApi(FRESH, verdicts)

    result = pipeline.run(_config(tmp_path), sdcard=tmp_path, api=api, dry_run=True,
                          yes=True, confirm=lambda _: True)

    assert (tmp_path / "DCIM" / "a.jpg").exists()
    assert result.deleted == []


def test_a_declined_confirmation_deletes_nothing(tmp_path):
    (tmp_path / "DCIM").mkdir()
    (tmp_path / "DCIM" / "a.jpg").write_bytes(b"x" * 4)
    verdicts = [_verdict("a.jpg", "ARCHIVED", tier="NAME_SIZE", channel_id=-1, tg_message_id=1)]
    api = FakeApi(FRESH, verdicts)

    result = pipeline.run(_config(tmp_path), sdcard=tmp_path, api=api, dry_run=False,
                          yes=False, confirm=lambda _: False)

    assert (tmp_path / "DCIM" / "a.jpg").exists()
    assert result.deleted == []


def test_confirmed_deletion_removes_files_and_records_the_audit(tmp_path):
    (tmp_path / "DCIM").mkdir()
    (tmp_path / "DCIM" / "a.jpg").write_bytes(b"x" * 4)
    verdicts = [_verdict("a.jpg", "ARCHIVED", tier="NAME_SIZE", channel_id=-1, tg_message_id=1)]
    api = FakeApi(FRESH, verdicts)

    result = pipeline.run(_config(tmp_path), sdcard=tmp_path, api=api, yes=True,
                          confirm=lambda _: True, refresh=lambda paths: None)

    assert not (tmp_path / "DCIM" / "a.jpg").exists()
    assert result.deleted[0]["relpath"] == "DCIM/a.jpg"
    assert result.deleted[0]["tier"] == "NAME_SIZE"
    assert result.deleted[0]["channel_id"] == -1
    assert result.deleted[0]["tg_message_id"] == 1
    assert api.deletion_calls and api.deletion_calls[0][0]["relpath"] == "DCIM/a.jpg"


def test_a_file_changed_since_enumeration_is_skipped_not_deleted(tmp_path):
    from vault_client import enumerate as ve

    (tmp_path / "DCIM").mkdir()
    verdicts = [_verdict("a.jpg", "ARCHIVED", tier="NAME_SIZE", channel_id=-1, tg_message_id=1)]
    api = FakeApi(FRESH, verdicts)

    def rivalrous_enumerate(roots, *, sdcard):
        # Enumerate reports 4 bytes; the file grows to 99 before deletion runs.
        (tmp_path / "DCIM" / "a.jpg").write_bytes(b"x" * 99)
        return [ve.Entry("DCIM/a.jpg", "a.jpg", 4, datetime.now(timezone.utc))]

    result = pipeline.run(_config(tmp_path), sdcard=tmp_path, api=api, yes=True,
                          confirm=lambda _: True, enumerate_fn=rivalrous_enumerate,
                          refresh=lambda paths: None)

    assert (tmp_path / "DCIM" / "a.jpg").exists(), "a changed file must not be deleted"
    assert result.deleted == []
    assert result.skipped[0]["relpath"] == "DCIM/a.jpg"


def test_media_store_refresh_runs_the_scan_once_per_directory():
    calls = []
    pipeline._refresh_media_store(
        ["/sdcard/DCIM/Camera/a.jpg", "/sdcard/DCIM/Camera/b.jpg", "/sdcard/Pictures/c.jpg"],
        runner=lambda *args, **kwargs: calls.append(args),
    )
    assert calls == [
        (["termux-media-scan", "-r", "/sdcard/DCIM/Camera"],),
        (["termux-media-scan", "-r", "/sdcard/Pictures"],),
    ]


def test_confirmed_run_deletes_only_archived_and_leaves_other_verdicts_alone(tmp_path):
    (tmp_path / "DCIM").mkdir()
    for name in ("keep.jpg", "flight.jpg", "plain.jpg", "ambig.jpg"):
        (tmp_path / "DCIM" / name).write_bytes(b"x" * 4)
    (tmp_path / "DCIM" / "gone.jpg").write_bytes(b"x" * 4)
    verdicts = [
        _verdict("gone.jpg", "ARCHIVED", tier="NAME_SIZE", channel_id=-1, tg_message_id=1),
        _verdict("keep.jpg", "IN_FLIGHT", channel_id=-1, tg_message_id=2),
        _verdict("flight.jpg", "NOT_ARCHIVED"),
        _verdict("plain.jpg", "AMBIGUOUS", reason="size_mismatch"),
        _verdict("ambig.jpg", "AMBIGUOUS", reason="size_mismatch", channel_id=-1, tg_message_id=None),
    ]
    api = FakeApi(FRESH, verdicts)

    result = pipeline.run(_config(tmp_path), sdcard=tmp_path, api=api, yes=True,
                          confirm=lambda _: True, refresh=lambda paths: None)

    assert not (tmp_path / "DCIM" / "gone.jpg").exists()
    for name in ("keep.jpg", "flight.jpg", "plain.jpg", "ambig.jpg"):
        assert (tmp_path / "DCIM" / name).exists(), f"{name} must survive a confirmed run"
    assert [d["relpath"] for d in result.deleted] == ["DCIM/gone.jpg"]


def test_a_file_with_same_size_but_new_mtime_is_skipped_not_deleted(tmp_path):
    from vault_client import enumerate as ve

    (tmp_path / "DCIM").mkdir()
    path = tmp_path / "DCIM" / "a.jpg"
    path.write_bytes(b"x" * 4)
    verdicts = [_verdict("a.jpg", "ARCHIVED", tier="NAME_SIZE", channel_id=-1, tg_message_id=1)]
    api = FakeApi(FRESH, verdicts)

    def rivalrous_enumerate(roots, *, sdcard):
        entries = ve.enumerate_entries(roots, sdcard=sdcard)
        later = datetime.now(timezone.utc).timestamp() + 5
        os.utime(path, (later, later))  # same size, newer mtime
        return entries

    result = pipeline.run(_config(tmp_path), sdcard=tmp_path, api=api, yes=True,
                          confirm=lambda _: True, enumerate_fn=rivalrous_enumerate,
                          refresh=lambda paths: None)

    assert path.exists(), "a file touched after enumeration must not be deleted"
    assert result.deleted == []
    assert result.skipped[0]["relpath"] == "DCIM/a.jpg"


def test_a_file_missing_at_delete_time_is_skipped_not_deleted(tmp_path):
    from vault_client import enumerate as ve

    (tmp_path / "DCIM").mkdir()
    path = tmp_path / "DCIM" / "a.jpg"
    path.write_bytes(b"x" * 4)
    verdicts = [_verdict("a.jpg", "ARCHIVED", tier="NAME_SIZE", channel_id=-1, tg_message_id=1)]
    api = FakeApi(FRESH, verdicts)

    def vanishing_enumerate(roots, *, sdcard):
        entries = ve.enumerate_entries(roots, sdcard=sdcard)
        path.unlink()  # someone else removed it before deletion runs
        return entries

    result = pipeline.run(_config(tmp_path), sdcard=tmp_path, api=api, yes=True,
                          confirm=lambda _: True, enumerate_fn=vanishing_enumerate,
                          refresh=lambda paths: None)

    assert not path.exists()
    assert result.deleted == []
    assert [s["relpath"] for s in result.skipped] == ["DCIM/a.jpg"]
    assert result.skipped[0]["reason"] == "changed_or_missing"


def test_a_failing_deletion_audit_is_best_effort(tmp_path):
    (tmp_path / "DCIM").mkdir()
    (tmp_path / "DCIM" / "a.jpg").write_bytes(b"x" * 4)
    verdicts = [_verdict("a.jpg", "ARCHIVED", tier="NAME_SIZE", channel_id=-1, tg_message_id=1)]

    class RaisingApi(FakeApi):
        def deletions(self, device_id, records):
            raise RuntimeError("audit endpoint down")

    api = RaisingApi(FRESH, verdicts)

    result = pipeline.run(_config(tmp_path), sdcard=tmp_path, api=api, yes=True,
                          confirm=lambda _: True, refresh=lambda paths: None)

    assert not (tmp_path / "DCIM" / "a.jpg").exists()
    assert [d["relpath"] for d in result.deleted] == ["DCIM/a.jpg"]
