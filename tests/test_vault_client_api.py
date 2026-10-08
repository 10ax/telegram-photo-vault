import json

import pytest

from vault_client.api import ApiError, VaultApi

SERVER = "http://atlas.test:8000"


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return self.responses.pop(0)


def test_freshness_sends_the_api_key_and_parses_json():
    transport = FakeTransport([(200, json.dumps({"archive_rows": 3}).encode())])
    api = VaultApi(SERVER, "k", transport=transport)
    assert api.freshness() == {"archive_rows": 3}
    call = transport.calls[0]
    assert call["url"] == f"{SERVER}/api/catalog/freshness"
    assert call["headers"]["X-Api-Key"] == "k"


def test_reconcile_sends_the_manifest_body():
    transport = FakeTransport([(200, b'{"snapshot_id": 7, "entries": []}')])
    api = VaultApi(SERVER, "k", transport=transport)
    body = api.reconcile("pixel", [{"name": "a.jpg"}])
    assert body["snapshot_id"] == 7
    sent = json.loads(transport.calls[0]["body"])
    assert sent["entries"] == [{"name": "a.jpg"}]
    assert sent["final"] is True
    assert transport.calls[0]["url"] == f"{SERVER}/api/devices/pixel/reconcile"


def test_verify_posts_the_hashes():
    transport = FakeTransport([(200, b'{"match": true}')])
    api = VaultApi(SERVER, "k", transport=transport)
    api.verify(channel_id=-100, tg_message_id=5, file_size=9, head_sha256="a" * 64, tail_sha256="b" * 64)
    assert json.loads(transport.calls[0]["body"]) == {
        "channel_id": -100, "tg_message_id": 5, "file_size": 9,
        "head_sha256": "a" * 64, "tail_sha256": "b" * 64,
    }


def test_a_non_2xx_becomes_an_apierror_with_status_and_detail():
    transport = FakeTransport([(409, b'{"detail": "catalog has never been scanned"}')])
    api = VaultApi(SERVER, "k", transport=transport)
    with pytest.raises(ApiError) as caught:
        api.freshness()
    assert caught.value.status == 409
    assert "never been scanned" in caught.value.detail


def _verdicts(names):
    return [{"relpath": n, "name": n, "verdict": "ARCHIVED", "tier": "NAME_SIZE",
             "reason": None, "channel_id": -1, "tg_message_id": 1} for n in names]


def test_reconcile_all_paginates_with_a_snapshot_id_and_finalises_at_the_end():
    names = [f"f{i}.jpg" for i in range(5)]
    transport = FakeTransport([
        (200, json.dumps({"snapshot_id": 7, "entries": _verdicts(names[:2])}).encode()),
        (200, json.dumps({"snapshot_id": 7, "entries": _verdicts(names[2:4])}).encode()),
        (200, json.dumps({"snapshot_id": 7, "entries": _verdicts(names[4:])}).encode()),
    ])
    api = VaultApi(SERVER, "k", transport=transport)
    out = api.reconcile_all("pixel", [{"name": n} for n in names], chunk_size=2)
    assert [v["name"] for v in out] == names
    sent = [json.loads(c["body"]) for c in transport.calls]
    assert sent[0]["snapshot_id"] is None and sent[0]["final"] is False
    assert sent[1]["snapshot_id"] == 7 and sent[1]["final"] is False
    assert sent[2]["snapshot_id"] == 7 and sent[2]["final"] is True


def test_reconcile_all_halves_the_chunk_when_the_server_says_413():
    names = [f"f{i}.jpg" for i in range(4)]
    transport = FakeTransport([
        (413, b'{"detail": "too many entries"}'),
        (200, json.dumps({"snapshot_id": 7, "entries": _verdicts(names[:2])}).encode()),
        (200, json.dumps({"snapshot_id": 7, "entries": _verdicts(names[2:])}).encode()),
    ])
    api = VaultApi(SERVER, "k", transport=transport)
    out = api.reconcile_all("pixel", [{"name": n} for n in names], chunk_size=4)
    assert [v["name"] for v in out] == names
    assert len(transport.calls) == 3
    split = json.loads(transport.calls[1]["body"])
    assert split["snapshot_id"] is None
    assert split["entries"] == [{"name": n} for n in names[:2]]
    assert split["final"] is False


def test_reconcile_all_propagates_a_non_413_apierror_without_retrying():
    names = [f"f{i}.jpg" for i in range(4)]
    transport = FakeTransport([(409, b'{"detail": "catalog has never been scanned"}')])
    api = VaultApi(SERVER, "k", transport=transport)
    with pytest.raises(ApiError) as caught:
        api.reconcile_all("pixel", [{"name": n} for n in names], chunk_size=2)
    assert caught.value.status == 409
    assert len(transport.calls) == 1


def test_reconcile_all_returns_empty_without_any_call():
    transport = FakeTransport([])
    api = VaultApi(SERVER, "k", transport=transport)
    assert api.reconcile_all("pixel", []) == []
    assert transport.calls == []


def test_reconcile_all_rejects_a_summary_that_disagrees_with_what_was_sent():
    transport = FakeTransport([
        (200, json.dumps({
            "snapshot_id": 7,
            "entries": _verdicts(["a.jpg"]),
            "summary": {"TOTAL": {"files": 1, "bytes": 10}},
        }).encode()),
    ])
    api = VaultApi(SERVER, "k", transport=transport)
    with pytest.raises(ApiError) as caught:
        api.reconcile_all("pixel", [{"name": "a.jpg"}, {"name": "b.jpg"}], chunk_size=2)
    assert caught.value.status == 500
    assert "summary mismatch" in caught.value.detail


def test_reconcile_all_accepts_a_summary_that_agrees_with_what_was_sent():
    transport = FakeTransport([
        (200, json.dumps({
            "snapshot_id": 7,
            "entries": _verdicts(["a.jpg", "b.jpg"]),
            "summary": {"TOTAL": {"files": 2, "bytes": 20}},
        }).encode()),
    ])
    api = VaultApi(SERVER, "k", transport=transport)
    out = api.reconcile_all("pixel", [{"name": "a.jpg"}, {"name": "b.jpg"}], chunk_size=2)
    assert [v["name"] for v in out] == ["a.jpg", "b.jpg"]
