"""Characterisation tests for the API's guards: auth, availability, and resume points.

tests/test_api.py covers the happy paths against a seeded database. This file
covers the refusals — the responses you get when the key is missing, the
service is not wired up, or a background task is already running — plus the
pure resume-point rule that `POST /api/photos/{id}/retry` uses.

Like test_api.py these build a bare FastAPI app and set `app.state` by hand;
the real lifespan is never run.
"""
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import _resume_status, router
from app.models.database import Photo, PhotoStatus
from app.services.recovery import RecoveryBusyError

HEADERS = {"X-Api-Key": "secret"}


class FakeRecovery:
    def __init__(self, *, busy=False):
        self.busy = busy
        self.calls = []

    def _guard(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if self.busy:
            raise RecoveryBusyError("A recovery task is already running.")

    def start_scan(self):
        self._guard("scan")

    def start_run(self, dry_run, *, limit=None, max_download_bytes=None):
        self._guard("run", dry_run=dry_run, limit=limit, max_download_bytes=max_download_bytes)

    def start_backfill(self, *, limit=None, max_video_bytes=None):
        self._guard("backfill", limit=limit, max_video_bytes=max_video_bytes)

    def status_snapshot(self):
        return {"running": self.busy, "activity": None, "batch": None}


def _app(**state):
    app = FastAPI()
    app.include_router(router)
    for key, value in state.items():
        setattr(app.state, key, value)
    return app


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("API_KEY", "secret")


def test_an_unconfigured_api_key_locks_the_api_rather_than_opening_it(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)

    with TestClient(_app()) as client:
        response = client.get("/api/system", headers=HEADERS)

    assert response.status_code == 503
    assert response.json()["detail"] == "API key is not configured."


def test_a_wrong_or_missing_key_is_rejected(keyed):
    with TestClient(_app()) as client:
        assert client.get("/api/system").status_code == 401
        assert client.get("/api/system", headers={"X-Api-Key": ""}).status_code == 401
        assert client.get("/api/system", headers={"X-Api-Key": "secre"}).status_code == 401
        assert client.get("/api/system", headers=HEADERS).status_code == 200


def test_run_reports_503_when_no_worker_is_wired_up(keyed):
    with TestClient(_app()) as client:
        response = client.post("/api/run", headers=HEADERS)

    assert response.status_code == 503
    assert response.json()["detail"] == "Worker is not running."


@pytest.mark.parametrize(
    "path, payload",
    [
        ("/api/recovery/scan", None),
        ("/api/recovery/run", {"dry_run": True}),
        ("/api/recovery/backfill", {}),
    ],
)
def test_recovery_endpoints_report_503_without_a_recovery_service(keyed, path, payload):
    with TestClient(_app()) as client:
        response = client.post(path, headers=HEADERS, json=payload)

    assert response.status_code == 503
    assert response.json()["detail"] == "Recovery service is not available."


@pytest.mark.parametrize(
    "path, payload",
    [
        ("/api/recovery/scan", None),
        ("/api/recovery/run", {"dry_run": False}),
        ("/api/recovery/backfill", {}),
    ],
)
def test_only_one_recovery_task_runs_at_a_time(keyed, path, payload):
    with TestClient(_app(recovery=FakeRecovery(busy=True))) as client:
        response = client.post(path, headers=HEADERS, json=payload)

    assert response.status_code == 409
    assert "already running" in response.json()["detail"]


def test_a_recovery_run_defaults_to_a_dry_run(keyed):
    recovery = FakeRecovery()
    with TestClient(_app(recovery=recovery)) as client:
        assert client.post("/api/recovery/run", headers=HEADERS).status_code == 200

    assert recovery.calls == [
        ("run", {"dry_run": True, "limit": None, "max_download_bytes": None})
    ]


def test_recovery_overrides_are_passed_through(keyed):
    recovery = FakeRecovery()
    with TestClient(_app(recovery=recovery)) as client:
        client.post(
            "/api/recovery/run",
            headers=HEADERS,
            json={"dry_run": False, "limit": 5, "max_download_bytes": 1024},
        )
        client.post(
            "/api/recovery/backfill", headers=HEADERS, json={"limit": 7, "max_video_bytes": 99}
        )

    assert recovery.calls == [
        ("run", {"dry_run": False, "limit": 5, "max_download_bytes": 1024}),
        ("backfill", {"limit": 7, "max_video_bytes": 99}),
    ]


def test_system_reports_disk_usage_for_the_data_volume(keyed, monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_VOLUME_PATH", str(tmp_path))

    with TestClient(_app()) as client:
        payload = client.get("/api/system", headers=HEADERS).json()

    assert payload["path"] == str(tmp_path)
    assert payload["total_bytes"] > 0
    assert payload["free_bytes"] <= payload["total_bytes"]
    assert 0.0 <= payload["used_percent"] <= 100.0


def test_system_falls_back_to_the_root_filesystem_when_the_volume_is_absent(
    keyed, monkeypatch, tmp_path
):
    monkeypatch.setenv("DATA_VOLUME_PATH", str(tmp_path / "not-mounted"))

    with TestClient(_app()) as client:
        payload = client.get("/api/system", headers=HEADERS).json()

    assert payload["path"] == "/"


# -- where a retry resumes --------------------------------------------------


def _photo(
    tmp_path, *, failed_status=None, local=False, compressed=False, tg=None, claims_webp=None
):
    """A FAILED row. `local`/`compressed` decide whether the files really exist.

    `claims_webp` is what compressed_path holds; it defaults to "set whenever a
    step past compression was recorded", which is the shape of a real row.
    """
    local_path = tmp_path / "IMG_1.jpg"
    compressed_path = tmp_path / "IMG_1.webp"
    if local:
        local_path.write_bytes(b"jpeg")
    if compressed:
        compressed_path.write_bytes(b"webp")
    if claims_webp is None:
        claims_webp = compressed or failed_status is not None
    return Photo(
        mega_path="/Camera/IMG_1.jpg",
        status=PhotoStatus.FAILED,
        failed_status=failed_status,
        local_path=str(local_path),
        compressed_path=str(compressed_path) if claims_webp else None,
        tg_message_id=tg,
    )


def test_a_recorded_failed_step_is_resumed_when_its_inputs_still_exist(tmp_path):
    photo = _photo(tmp_path, failed_status=PhotoStatus.COMPRESSED, local=True, compressed=True)
    assert _resume_status(photo) == PhotoStatus.COMPRESSED


def test_a_missing_webp_walks_the_retry_back_to_the_compression_step(tmp_path):
    photo = _photo(tmp_path, failed_status=PhotoStatus.COMPRESSED, local=True)
    assert _resume_status(photo) == PhotoStatus.TG_UPLOADED


def test_a_missing_original_walks_the_retry_all_the_way_back_to_pending(tmp_path):
    for step in (
        PhotoStatus.DOWNLOADED,
        PhotoStatus.CHUNK_UPLOADING,
        PhotoStatus.TG_UPLOADED,
        PhotoStatus.COMPRESSED,
    ):
        photo = _photo(tmp_path, failed_status=step)
        assert _resume_status(photo) == PhotoStatus.PENDING, step


def test_a_download_failure_simply_starts_over(tmp_path):
    photo = _photo(tmp_path, failed_status=PhotoStatus.PENDING, local=True)
    assert _resume_status(photo) == PhotoStatus.PENDING


def test_legacy_rows_without_a_failed_step_infer_one_from_what_is_on_disk(tmp_path):
    # Rows predating the failed_status column: the resume point is reconstructed
    # from compressed_path, then tg_message_id, then local_path.
    assert _resume_status(_photo(tmp_path, local=True, compressed=True)) == (
        PhotoStatus.COMPRESSED
    )
    assert _resume_status(_photo(tmp_path, local=True, tg=42)) == PhotoStatus.TG_UPLOADED
    assert _resume_status(_photo(tmp_path, local=True)) == PhotoStatus.DOWNLOADED

    nothing = Photo(mega_path="/Camera/IMG_1.jpg", status=PhotoStatus.FAILED)
    assert _resume_status(nothing) == PhotoStatus.PENDING


def test_the_resume_rule_never_trusts_a_path_it_cannot_see(tmp_path):
    photo = Photo(
        mega_path="/Camera/IMG_1.jpg",
        status=PhotoStatus.FAILED,
        local_path=str(Path(tmp_path) / "vanished.jpg"),
        tg_message_id=42,
    )
    assert _resume_status(photo) == PhotoStatus.PENDING
