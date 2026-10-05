"""The endpoints: auth, the refusals, continuation rules and the audit.

Built like tests/test_api.py: a bare FastAPI with the router mounted and
app.state set by hand, never running the lifespan.
"""
import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import router
from app.models.database import (
    AsyncSessionLocal,
    CatalogItem,
    ChannelRole,
    DeletionAudit,
)
from app.services.catalog import CatalogBusyError
from app.services.reconcile import ReconcileService
from app.services.telegram import ArchivedMessageMissing

ARCHIVE = -1002637897512
KEY = "test-key"


class FakeTelegram:
    """Duck-types only fingerprint_message: a fixed digest pair and a size.

    The size is the archived copy's own, as the real service reports it — the
    client's claimed size is never echoed back.
    """

    def __init__(self, fingerprint: dict[str, str], archived_file_size: int | None = 100):
        self.fingerprint = fingerprint
        self.archived_file_size = archived_file_size

    async def fingerprint_message(self, channel_id, message_id, *, file_size, window):
        return {**self.fingerprint, "archived_file_size": self.archived_file_size}


class MissingMessageTelegram:
    """Reproduces the real service's contract for a missing/deleted message:
    TelegramService.fingerprint_message raises ArchivedMessageMissing itself
    (it checks get_messages's result for None), not a bare AttributeError.
    """

    async def fingerprint_message(self, channel_id, message_id, *, file_size, window):
        raise ArchivedMessageMissing(
            f"Message {message_id} in channel {channel_id} was not found or has been deleted."
        )


@pytest.fixture
def app_state(client):
    """The FastAPI state the client fixture built, for tests that add a service."""
    return client.app.state


@pytest.fixture
def client(clean_db, monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)

    async def seed():
        async with AsyncSessionLocal() as session:
            session.add(
                CatalogItem(
                    channel_id=ARCHIVE,
                    tg_message_id=1,
                    channel_role=ChannelRole.ARCHIVE,
                    media_kind="document",
                    file_name="a.jpg",
                    file_size=100,
                    message_date=datetime(2026, 7, 20, tzinfo=timezone.utc),
                    scanned_at=datetime(2026, 9, 25, tzinfo=timezone.utc),
                )
            )
            await session.commit()

    asyncio.run(seed())

    app = FastAPI()
    app.include_router(router)
    app.state.reconcile = ReconcileService(max_entries=3)
    with TestClient(app) as test_client:
        yield test_client
    asyncio.run(_dispose())


async def _dispose():
    from app.models.database import engine

    await engine.dispose()


def _entry(name="a.jpg", size=100):
    return {"relpath": f"DCIM/{name}", "name": name, "size": size,
            "mtime": "2026-07-01T00:00:00Z", "sha256": None}


def _post(client, path, body):
    return client.post(path, json=body, headers={"X-Api-Key": KEY})


def test_reconcile_requires_the_api_key(client):
    response = client.post("/api/devices/pixel/reconcile", json={"entries": []})
    assert response.status_code == 401


def test_reconcile_returns_a_verdict_per_entry_and_a_summary(client):
    response = _post(client, "/api/devices/pixel/reconcile",
                     {"entries": [_entry(), _entry("missing.jpg", 5)]})
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["ARCHIVED"]["files"] == 1
    assert body["summary"]["NOT_ARCHIVED"]["files"] == 1
    assert [e["verdict"] for e in body["entries"]] == ["ARCHIVED", "NOT_ARCHIVED"]
    assert body["catalog"]["archive_rows"] == 1


def test_only_non_archived_entries_are_stored_as_findings(client):
    body = _post(client, "/api/devices/pixel/reconcile",
                 {"entries": [_entry(), _entry("missing.jpg", 5)]}).json()
    snapshot = client.get("/api/devices/pixel/snapshot", headers={"X-Api-Key": KEY}).json()
    assert snapshot["snapshot"]["archived_files"] == 1
    assert [f["file_name"] for f in snapshot["findings"]] == ["missing.jpg"]
    assert body["snapshot_id"] == snapshot["snapshot"]["id"]


def test_too_many_entries_is_refused_with_413(client):
    response = _post(client, "/api/devices/pixel/reconcile",
                     {"entries": [_entry(f"f{i}.jpg") for i in range(4)]})
    assert response.status_code == 413


def test_a_continuation_accumulates_into_one_snapshot(client):
    first = _post(client, "/api/devices/pixel/reconcile",
                  {"entries": [_entry()], "final": False}).json()
    second = _post(client, "/api/devices/pixel/reconcile",
                   {"entries": [_entry("missing.jpg", 5)],
                    "snapshot_id": first["snapshot_id"], "final": True}).json()

    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["summary"]["ARCHIVED"]["files"] == 1
    assert second["summary"]["NOT_ARCHIVED"]["files"] == 1


def test_taken_at_is_recorded_on_the_snapshot(client):
    """The request accepts the client's own clock; it must actually be stored,
    not silently dropped on the way to ReconcileService.reconcile."""
    _post(client, "/api/devices/pixel/reconcile",
          {"entries": [_entry()], "taken_at": "2026-09-24T10:00:00Z"})
    snapshot = client.get("/api/devices/pixel/snapshot", headers={"X-Api-Key": KEY}).json()
    assert snapshot["snapshot"]["taken_at"].startswith("2026-09-24T10:00:00")


def test_a_snapshot_belonging_to_another_device_is_refused(client):
    first = _post(client, "/api/devices/pixel/reconcile",
                  {"entries": [_entry()], "final": False}).json()
    response = _post(client, "/api/devices/samsung/reconcile",
                     {"entries": [_entry()], "snapshot_id": first["snapshot_id"]})
    assert response.status_code == 409


def test_a_finished_snapshot_cannot_be_continued(client):
    first = _post(client, "/api/devices/pixel/reconcile",
                  {"entries": [_entry()], "final": True}).json()
    response = _post(client, "/api/devices/pixel/reconcile",
                     {"entries": [_entry()], "snapshot_id": first["snapshot_id"]})
    assert response.status_code == 409


def test_an_unscanned_catalog_is_a_409_not_a_confident_answer(clean_db, monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)
    app = FastAPI()
    app.include_router(router)
    app.state.reconcile = ReconcileService()
    with TestClient(app) as test_client:
        response = test_client.post("/api/devices/pixel/reconcile",
                                    json={"entries": [_entry()]},
                                    headers={"X-Api-Key": KEY})
    assert response.status_code == 409
    assert "scan" in response.json()["detail"].lower()
    asyncio.run(_dispose())


def test_recorded_deletions_are_audited(client):
    response = _post(client, "/api/devices/pixel/deletions", {
        "deleted": [{
            "relpath": "DCIM/a.jpg", "name": "a.jpg", "size": 100,
            "tier": "NAME_SIZE", "channel_id": ARCHIVE, "tg_message_id": 1,
            "deleted_at": "2026-09-24T10:00:00Z",
        }]
    })
    assert response.status_code == 200
    assert response.json()["recorded"] == 1

    async def read():
        async with AsyncSessionLocal() as session:
            from sqlalchemy import select

            return list((await session.scalars(select(DeletionAudit))).all())

    rows = asyncio.run(read())
    assert len(rows) == 1
    assert rows[0].tg_message_id == 1
    assert rows[0].device_id == "pixel"


def test_verify_compares_the_clients_fingerprint_with_the_archived_copy(client, app_state):
    app_state.telegram = FakeTelegram({"head_sha256": "h" * 64, "tail_sha256": "t" * 64})

    agreeing = _post(client, "/api/vault/verify", {
        "channel_id": ARCHIVE, "tg_message_id": 1, "file_size": 100,
        "head_sha256": "h" * 64, "tail_sha256": "t" * 64,
    })
    assert agreeing.json()["match"] is True

    assert agreeing.json()["archived_file_size"] == 100

    disagreeing = _post(client, "/api/vault/verify", {
        "channel_id": ARCHIVE, "tg_message_id": 1, "file_size": 100,
        "head_sha256": "h" * 64, "tail_sha256": "x" * 64,
    })
    assert disagreeing.json()["match"] is False


def test_verify_refuses_a_match_when_the_archived_size_differs(client, app_state):
    """Tier A- is equal fingerprints *plus equal size*. Two files can share
    512 KiB of head and tail and differ in the middle, and size_mismatch is the
    commonest reason an entry is AMBIGUOUS in the first place — so agreeing
    digests alone must never come back as `match: true`."""
    app_state.telegram = FakeTelegram(
        {"head_sha256": "h" * 64, "tail_sha256": "t" * 64}, archived_file_size=999
    )

    response = _post(client, "/api/vault/verify", {
        "channel_id": ARCHIVE, "tg_message_id": 1, "file_size": 100,
        "head_sha256": "h" * 64, "tail_sha256": "t" * 64,
    })

    body = response.json()
    assert body["match"] is False
    assert body["archived_file_size"] == 999


def test_verify_refuses_a_match_when_the_archived_size_is_unknown(client, app_state):
    """A message carrying no size at all (a native photo, say) is not evidence."""
    app_state.telegram = FakeTelegram(
        {"head_sha256": "h" * 64, "tail_sha256": "t" * 64}, archived_file_size=None
    )

    response = _post(client, "/api/vault/verify", {
        "channel_id": ARCHIVE, "tg_message_id": 1, "file_size": 100,
        "head_sha256": "h" * 64, "tail_sha256": "t" * 64,
    })

    assert response.json()["match"] is False


def test_a_negative_entry_size_is_refused_rather_than_totalled(client):
    """A negative size would drive the snapshot's total_bytes below zero, and
    that total is what the owner reads before deciding how much to delete."""
    entry = _entry()
    entry["size"] = -1
    assert _post(client, "/api/devices/pixel/reconcile", {"entries": [entry]}).status_code == 422

    deletion = {
        "relpath": "DCIM/a.jpg", "name": "a.jpg", "size": -1,
        "tier": "NAME_SIZE", "channel_id": ARCHIVE, "tg_message_id": 1,
    }
    assert _post(client, "/api/devices/pixel/deletions",
                 {"deleted": [deletion]}).status_code == 422


def test_verify_without_a_telegram_service_is_503(client):
    response = _post(client, "/api/vault/verify", {
        "channel_id": ARCHIVE, "tg_message_id": 1, "file_size": 100,
        "head_sha256": "h" * 64, "tail_sha256": "t" * 64,
    })
    assert response.status_code == 503


def test_verify_of_a_missing_message_is_404_not_500(client, app_state):
    """The known issue: a missing/deleted message must become a clean 404,
    not the bare AttributeError the underlying client raises internally."""
    app_state.telegram = MissingMessageTelegram()

    response = _post(client, "/api/vault/verify", {
        "channel_id": ARCHIVE, "tg_message_id": 999, "file_size": 100,
        "head_sha256": "h" * 64, "tail_sha256": "t" * 64,
    })
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert str(ARCHIVE) in detail
    assert "999" in detail


class FakeCatalog:
    """Duck-types the background-task surface the scan route uses."""

    def __init__(self, *, busy=False):
        self.busy = busy
        self.calls: list[str] = []

    def start_scan(self):
        self.calls.append("start_scan")
        if self.busy:
            raise CatalogBusyError("A catalog task is already running.")

    def status_snapshot(self):
        return {"running": self.busy, "activity": None, "last_error": None,
                "result": None, "channels": []}

    async def resolve_manifests(self, limit=50):
        self.calls.append(f"resolve_manifests:{limit}")
        return {"resolved": 1, "failed": 0, "remaining": 0}


def test_scan_and_resolve_routes_reach_the_catalog_service(client, app_state):
    """Without these two, nothing could ever populate the catalog."""
    app_state.catalog = FakeCatalog()

    scanned = _post(client, "/api/catalog/scan", {})
    assert scanned.status_code == 200
    assert scanned.json()["running"] is False

    resolved = client.post("/api/catalog/resolve-manifests", params={"limit": 10},
                           headers={"X-Api-Key": KEY})
    assert resolved.json()["resolved"] == 1
    assert app_state.catalog.calls == ["start_scan", "resolve_manifests:10"]


def test_a_second_scan_while_one_is_running_is_409(client, app_state):
    """A full history is minutes of paced traffic; two at once would race."""
    app_state.catalog = FakeCatalog(busy=True)

    response = _post(client, "/api/catalog/scan", {})

    assert response.status_code == 409
    assert "already running" in response.json()["detail"]


def test_catalog_status_reports_whether_a_scan_is_running(client, app_state):
    app_state.catalog = FakeCatalog(busy=True)

    body = client.get("/api/catalog/status", headers={"X-Api-Key": KEY}).json()

    assert body["running"] is True


def test_the_catalog_routes_are_503_without_the_service(client):
    assert _post(client, "/api/catalog/scan", {}).status_code == 503
    assert client.get("/api/catalog/status", headers={"X-Api-Key": KEY}).status_code == 503
    assert client.post("/api/catalog/resolve-manifests",
                       headers={"X-Api-Key": KEY}).status_code == 503


def test_catalog_freshness_reports_what_the_client_needs(client):
    body = client.get("/api/catalog/freshness", headers={"X-Api-Key": KEY}).json()
    assert body["archive_rows"] == 1
    assert body["frontier"].startswith("2026-09-25")
    assert body["channels"][0]["newest_message_date"].startswith("2026-07-20")


def test_lookup_answers_a_single_file(client):
    body = client.get("/api/vault/lookup", params={"name": "a.jpg", "size": 100},
                      headers={"X-Api-Key": KEY}).json()
    assert body["verdict"] == "ARCHIVED"
    assert body["freshness_gate"] is False, "the lookup opts out and must say so"


def test_reconcile_entry_missing_mtime_is_in_flight_not_a_guess(client):
    """Unlike GET /api/vault/lookup, the reconcile path keeps the freshness
    gate on — an entry with no mtime must not be silently promoted."""
    entry = {"relpath": "DCIM/a.jpg", "name": "a.jpg", "size": 100, "sha256": None}
    body = _post(client, "/api/devices/pixel/reconcile", {"entries": [entry]}).json()
    assert body["entries"][0]["verdict"] == "IN_FLIGHT"


def test_reconcile_against_an_unknown_snapshot_is_404(client):
    """A snapshot id that does not exist at all is a 404, not a false claim
    that it belongs to someone else."""
    response = _post(client, "/api/devices/pixel/reconcile",
                     {"entries": [_entry()], "snapshot_id": 999999})
    assert response.status_code == 404


def test_a_continuation_accumulates_bytes_not_just_file_counts(client):
    first = _post(client, "/api/devices/pixel/reconcile",
                  {"entries": [_entry()], "final": False}).json()
    second = _post(client, "/api/devices/pixel/reconcile",
                   {"entries": [_entry("missing.jpg", 5)],
                    "snapshot_id": first["snapshot_id"], "final": True}).json()

    assert second["summary"]["ARCHIVED"]["bytes"] == 100
    assert second["summary"]["NOT_ARCHIVED"]["bytes"] == 5
    assert second["summary"]["TOTAL"]["bytes"] == 105


def test_snapshot_404s_for_a_device_with_no_history(client):
    response = client.get("/api/devices/never-seen/snapshot", headers={"X-Api-Key": KEY})
    assert response.status_code == 404


def test_an_invalid_deletion_tier_is_422_and_nothing_is_audited(client):
    """A typo in tier must be rejected before anything is considered audited —
    not raise a raw ValueError after the client has already deleted the file."""
    response = _post(client, "/api/devices/pixel/deletions", {
        "deleted": [{
            "relpath": "DCIM/a.jpg", "name": "a.jpg", "size": 100,
            "tier": "NOT_A_REAL_TIER", "channel_id": ARCHIVE, "tg_message_id": 1,
            "deleted_at": "2026-09-24T10:00:00Z",
        }]
    })
    assert response.status_code == 422

    async def read():
        async with AsyncSessionLocal() as session:
            from sqlalchemy import select

            return list((await session.scalars(select(DeletionAudit))).all())

    assert asyncio.run(read()) == []
