import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import router
from app.models.database import (
    AsyncSessionLocal,
    Base,
    MediaType,
    Photo,
    PhotoStatus,
    engine,
    init_db,
)


class FakeWorker:
    def __init__(self):
        self.triggered = 0

    def status_snapshot(self):
        return {
            "mode": "interval",
            "run_interval_seconds": 900,
            "running": False,
            "last_run_started_at": None,
            "last_run_finished_at": None,
            "next_run_at": None,
            "last_run_error": None,
        }

    def trigger(self):
        self.triggered += 1


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", "secret")

    local_file = tmp_path / "x.jpg"
    local_file.write_bytes(b"data")

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await init_db()
        async with AsyncSessionLocal() as session:
            session.add(
                Photo(
                    mega_path="/Camera/x.jpg",
                    status=PhotoStatus.FAILED,
                    failed_status=PhotoStatus.TG_UPLOADED,
                    media_type=MediaType.IMAGE,
                    local_path=str(local_file),
                    retry_count=3,
                    error_log="boom",
                )
            )
            session.add(
                Photo(
                    mega_path="/Camera/gone.jpg",
                    status=PhotoStatus.FAILED,
                    failed_status=PhotoStatus.TG_UPLOADED,
                    media_type=MediaType.IMAGE,
                    local_path=str(tmp_path / "missing.jpg"),
                    retry_count=3,
                )
            )
            session.add(Photo(mega_path="/Camera/y.mp4", status=PhotoStatus.COMPLETED,
                              media_type=MediaType.VIDEO))
            await session.commit()
        # Pooled aiosqlite connections must not cross into TestClient's loop.
        await engine.dispose()

    asyncio.run(setup())

    app = FastAPI()
    app.include_router(router)
    app.state.worker = FakeWorker()
    with TestClient(app) as test_client:
        test_client.app_ref = app
        yield test_client


HEADERS = {"X-Api-Key": "secret"}


def test_requires_api_key(client):
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", headers={"X-Api-Key": "wrong"}).status_code == 401


def test_status_counts_and_worker(client):
    response = client.get("/api/status", headers=HEADERS)
    assert response.status_code == 200
    payload = response.json()
    assert payload["photos"]["FAILED"] == 2
    assert payload["photos"]["COMPLETED"] == 1
    assert payload["worker"]["mode"] == "interval"


def test_run_triggers_worker(client):
    response = client.post("/api/run", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["triggered"] is True
    assert client.app_ref.state.worker.triggered == 1


def test_list_photos_filter_and_validation(client):
    response = client.get("/api/photos?status=FAILED", headers=HEADERS)
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert {item["mega_path"] for item in payload["items"]} == {
        "/Camera/x.jpg",
        "/Camera/gone.jpg",
    }

    assert client.get("/api/photos?status=BOGUS", headers=HEADERS).status_code == 422


def test_retry_resumes_at_failed_step(client):
    photos = client.get("/api/photos?status=FAILED", headers=HEADERS).json()["items"]
    with_file = next(p for p in photos if p["mega_path"] == "/Camera/x.jpg")

    response = client.post(f"/api/photos/{with_file['id']}/retry", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "TG_UPLOADED"
    assert body["retry_count"] == 0 and body["error_log"] is None
    assert client.app_ref.state.worker.triggered == 1

    # Only FAILED photos can be retried.
    assert client.post(f"/api/photos/{with_file['id']}/retry", headers=HEADERS).status_code == 409
    assert client.post("/api/photos/999999/retry", headers=HEADERS).status_code == 404


def test_retry_walks_back_when_local_file_missing(client):
    photos = client.get("/api/photos?status=FAILED", headers=HEADERS).json()["items"]
    missing = next(p for p in photos if p["mega_path"] == "/Camera/gone.jpg")

    response = client.post(f"/api/photos/{missing['id']}/retry", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["status"] == "PENDING"
