"""Characterisation tests for TelegramService's send path.

The load-bearing assertion in this file is `force_document=True`. Telegram
transcodes anything it is allowed to interpret as a photo, video or animation,
which would silently break the archive's byte-for-byte promise (and its
manifest SHA-256s). Commit 9aaa70a set the flag; these tests keep it set.
"""
import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pyrogram.errors import MessageNotModified

from app.models.database import MediaType
from app.services.telegram import TelegramService

CHANNEL = -100111
BROWSE = -100999


class FakeClient:
    """Duck-types only the pyrogram methods TelegramService actually calls."""

    def __init__(self, *, search_results=(), fail_send=False, fail_search=False):
        self.documents = []
        self.photos = []
        self.videos = []
        self.copies = []
        self.edits = []
        self.search_results = list(search_results)
        self.fail_send = fail_send
        self.fail_search = fail_search
        self.next_id = 1000

    def _next(self):
        self.next_id += 1
        return SimpleNamespace(id=self.next_id)

    async def send_document(self, **kwargs):
        self.documents.append(kwargs)
        return self._next()

    async def send_photo(self, **kwargs):
        if self.fail_send:
            raise RuntimeError("Telegram said no")
        self.photos.append(kwargs)
        return self._next()

    async def send_video(self, **kwargs):
        if self.fail_send:
            raise RuntimeError("Telegram said no")
        self.videos.append(kwargs)
        return self._next()

    async def copy_message(self, **kwargs):
        self.copies.append(kwargs)
        return self._next()

    async def edit_message_caption(self, **kwargs):
        self.edits.append(kwargs)
        return self._next()

    async def search_messages(self, chat_id, **kwargs):
        if self.fail_search:
            raise RuntimeError("search is flaky")
        for message in self.search_results:
            yield message


def _document_message(message_id, file_name):
    return SimpleNamespace(id=message_id, document=SimpleNamespace(file_name=file_name))


@pytest.fixture
def jpg(tmp_path) -> Path:
    path = tmp_path / "IMG_20240612_193000.jpg"
    path.write_bytes(b"not really a jpeg")
    return path


async def test_upload_document_forces_document_and_derives_caption(jpg):
    client = FakeClient()
    service = TelegramService(client, CHANNEL, upload_delay_seconds=0)

    message = await service.upload_document(jpg)

    assert message.id == 1001
    sent = client.documents[0]
    assert sent["force_document"] is True
    assert sent["chat_id"] == CHANNEL
    assert sent["document"] == str(jpg)
    # No caption passed in: the date chain reads it off the filename.
    assert sent["caption"] == "#2024 #06_2024 #2024_06_12"


async def test_upload_document_keeps_an_explicit_caption_and_file_name(jpg):
    client = FakeClient()
    service = TelegramService(client, CHANNEL, upload_delay_seconds=0)

    await service.upload_document(jpg, caption="given", file_name="renamed.jpg")

    sent = client.documents[0]
    assert (sent["caption"], sent["file_name"]) == ("given", "renamed.jpg")
    assert sent["force_document"] is True


async def test_upload_document_falls_back_to_the_given_datetime(tmp_path):
    undated = tmp_path / "clip.bin"
    undated.write_bytes(b"x")
    client = FakeClient()
    service = TelegramService(client, CHANNEL, upload_delay_seconds=0)

    await service.upload_document(undated, caption_fallback=datetime(2019, 2, 3, 4, 5, 6))

    assert client.documents[0]["caption"] == "#2019 #02_2019 #2019_02_03"


async def test_upload_bytes_sends_a_named_document_not_media():
    client = FakeClient()
    service = TelegramService(client, CHANNEL, upload_delay_seconds=0)

    await service.upload_bytes(b"{}", file_name="a.mp4.manifest.json", caption="#manifest")

    sent = client.documents[0]
    assert sent["file_name"] == "a.mp4.manifest.json"
    assert sent["force_document"] is True
    assert sent["document"].read() == b"{}"
    assert client.photos == [] and client.videos == []


async def test_every_upload_paces_itself_by_upload_delay_seconds(monkeypatch, jpg):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    client = FakeClient()
    service = TelegramService(client, CHANNEL, upload_delay_seconds=2.5)

    await service.upload_document(jpg, caption="c")
    await service.upload_bytes(b"x", file_name="n", caption="c")

    assert slept == [2.5, 2.5]


async def test_upload_media_returns_none_for_unsupported_and_on_failure(jpg):
    service = TelegramService(FakeClient(), CHANNEL, upload_delay_seconds=0)
    assert await service.upload_media(jpg, MediaType.OTHER, caption="c") is None

    failing = TelegramService(FakeClient(fail_send=True), CHANNEL, upload_delay_seconds=0)
    assert await failing.upload_media(jpg, MediaType.IMAGE, caption="c") is None


async def test_publish_browse_is_a_no_op_without_a_browse_channel(jpg):
    client = FakeClient()
    service = TelegramService(client, CHANNEL, upload_delay_seconds=0)

    assert await service.publish_browse(jpg, MediaType.IMAGE, caption="c") is None
    assert client.photos == []


async def test_publish_browse_targets_the_browse_channel(jpg):
    client = FakeClient()
    service = TelegramService(
        client, CHANNEL, upload_delay_seconds=0, browse_channel_id=BROWSE
    )

    message = await service.publish_browse(jpg, MediaType.IMAGE, caption="c")

    assert message is not None
    assert client.photos[0]["chat_id"] == BROWSE


async def test_publish_browse_swallows_send_failures(jpg):
    service = TelegramService(
        FakeClient(fail_send=True), CHANNEL, upload_delay_seconds=0, browse_channel_id=BROWSE
    )

    # Browse mirroring is best-effort: a failure must never surface to the worker.
    assert await service.publish_browse(jpg, MediaType.IMAGE, caption="c") is None


async def test_copy_to_browse_needs_a_browse_channel():
    client = FakeClient()
    assert await TelegramService(client, CHANNEL).copy_to_browse(CHANNEL, 5) is None
    assert client.copies == []


async def test_copy_to_browse_uses_server_side_copy():
    client = FakeClient()
    service = TelegramService(client, CHANNEL, browse_channel_id=BROWSE)

    await service.copy_to_browse(CHANNEL, 5, caption="#2024 #01_2024 #2024_01_01")

    assert client.copies == [
        {
            "chat_id": BROWSE,
            "from_chat_id": CHANNEL,
            "message_id": 5,
            "caption": "#2024 #01_2024 #2024_01_01",
        }
    ]


async def test_edit_caption_reports_success_and_tolerates_an_unchanged_caption():
    client = FakeClient()
    service = TelegramService(client, CHANNEL)

    assert await service.edit_caption(12, "#2024 #06_2024 #2024_06_12") is True
    assert client.edits[0]["message_id"] == 12

    async def already_identical(**kwargs):
        raise MessageNotModified()

    client.edit_message_caption = already_identical
    assert await service.edit_caption(12, "same") is True


async def test_find_document_by_name_requires_an_exact_filename_match():
    client = FakeClient(
        search_results=[
            SimpleNamespace(id=1, document=None),
            _document_message(2, "movie.mp4.part001-of-004.other"),
            _document_message(3, "movie.mp4.part001-of-004"),
        ]
    )
    service = TelegramService(client, CHANNEL)

    found = await service.find_document_by_name("movie.mp4.part001-of-004")

    # Not the captionless message, and not the longer near-miss name.
    assert found is not None and found.id == 3


async def test_find_document_by_name_treats_any_search_error_as_not_found():
    service = TelegramService(FakeClient(fail_search=True), CHANNEL)

    assert await service.find_document_by_name("movie.mp4.part001-of-004") is None
