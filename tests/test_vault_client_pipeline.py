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
