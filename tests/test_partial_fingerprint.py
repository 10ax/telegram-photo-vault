"""The fingerprint reads the two ends of a file and never the middle."""
import hashlib
from types import SimpleNamespace

import pytest

from app.services.telegram import ArchivedMessageMissing, TelegramService

CHUNK = 1024 * 1024


class FakeClient:
    """Serves a known blob in 1 MiB chunks, recording which ones were asked for."""

    def __init__(self, blob: bytes):
        self.blob = blob
        self.requested: list[tuple[int, int]] = []

    async def stream_media(self, message, *, offset: int = 0, limit: int = 0):
        self.requested.append((offset, limit))
        start = offset * CHUNK
        end = start + (limit or 1) * CHUNK
        yield self.blob[start:end]


def _service(client):
    return TelegramService(client, channel_id=-1, upload_delay_seconds=0)


async def test_head_and_tail_are_hashed_and_the_middle_is_never_fetched():
    blob = bytes(range(256)) * 20_000  # 5.12 MB, > 5 chunks
    client = FakeClient(blob)
    window = 262_144

    result = await _service(client).partial_fingerprint(
        SimpleNamespace(id=1), file_size=len(blob), window=window
    )

    last_chunk = (len(blob) - 1) // CHUNK
    assert result["head_sha256"] == hashlib.sha256(blob[:window]).hexdigest()
    assert result["tail_sha256"] == hashlib.sha256(blob[-window:]).hexdigest()
    assert client.requested == [(0, 1), (last_chunk, 1)]
    assert len(client.requested) == 2, "exactly two chunks, whatever the file size"


async def test_a_file_smaller_than_the_window_hashes_what_exists():
    blob = b"tiny file contents"
    client = FakeClient(blob)

    result = await _service(client).partial_fingerprint(
        SimpleNamespace(id=1), file_size=len(blob), window=262_144
    )

    whole = hashlib.sha256(blob).hexdigest()
    assert result["head_sha256"] == whole
    assert result["tail_sha256"] == whole


async def test_two_identical_blobs_fingerprint_identically():
    blob = bytes(range(256)) * 20_000
    first = await _service(FakeClient(blob)).partial_fingerprint(
        SimpleNamespace(id=1), file_size=len(blob)
    )
    second = await _service(FakeClient(bytes(blob))).partial_fingerprint(
        SimpleNamespace(id=2), file_size=len(blob)
    )
    assert first == second


async def test_fingerprint_message_fetches_the_message_then_hashes_it():
    blob = bytes(range(256)) * 20_000

    class FetchingClient(FakeClient):
        def __init__(self, blob):
            super().__init__(blob)
            self.asked: list[tuple[int, int]] = []

        async def get_messages(self, chat_id, message_ids):
            self.asked.append((chat_id, message_ids))
            return SimpleNamespace(id=message_ids)

    client = FetchingClient(blob)
    result = await _service(client).fingerprint_message(-100, 7, file_size=len(blob))

    assert client.asked == [(-100, 7)]
    assert result["head_sha256"] == hashlib.sha256(blob[:262_144]).hexdigest()


async def test_fingerprint_message_raises_a_purpose_built_error_when_the_message_is_gone():
    """get_messages returns None for a single missing/deleted id (real pyrogram
    behaviour); this must surface as ArchivedMessageMissing, not propagate into
    stream_media and blow up there as a bare, hard-to-attribute AttributeError."""

    class MissingMessageClient(FakeClient):
        async def get_messages(self, chat_id, message_ids):
            return None

    client = MissingMessageClient(b"")
    with pytest.raises(ArchivedMessageMissing):
        await _service(client).fingerprint_message(-100, 999, file_size=100)


async def test_a_difference_in_the_tail_changes_the_fingerprint():
    blob = bytes(range(256)) * 20_000
    tampered = blob[:-1] + b"\x00"
    first = await _service(FakeClient(blob)).partial_fingerprint(
        SimpleNamespace(id=1), file_size=len(blob)
    )
    second = await _service(FakeClient(tampered)).partial_fingerprint(
        SimpleNamespace(id=2), file_size=len(tampered)
    )
    assert first["head_sha256"] == second["head_sha256"]
    assert first["tail_sha256"] != second["tail_sha256"]
