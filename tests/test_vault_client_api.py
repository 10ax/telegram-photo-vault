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
