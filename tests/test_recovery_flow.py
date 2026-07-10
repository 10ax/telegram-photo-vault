"""Functional test of the hybrid in-place tidy flow against a fake Pyrogram client."""
import io
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select

from app.models.database import AsyncSessionLocal, RecoveryItem, RecoveryStatus
from app.services.recovery import RecoveryService
from app.services.telegram import TelegramService


def _jpeg(color) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _msg(mid, *, photo=None, document=None, caption=None, date=None):
    return SimpleNamespace(
        id=mid,
        photo=photo,
        document=document,
        video=None,
        animation=None,
        caption=caption,
        date=date or datetime(2022, 3, 5, 10, 0, 0),
        empty=False,
    )


class FakeClient:
    def __init__(self, messages, content):
        self.messages = {m.id: m for m in messages}
        self.content = content
        self.edited = []
        self.deleted = []
        self.downloaded = []

    async def get_chat_history(self, chat_id):
        for message in self.messages.values():
            yield message

    async def get_messages(self, chat_id, message_id):
        return self.messages.get(message_id)

    async def download_media(self, message, file_name):
        self.downloaded.append(message.id)
        Path(file_name).parent.mkdir(parents=True, exist_ok=True)
        Path(file_name).write_bytes(self.content[message.id])
        return file_name

    async def edit_message_caption(self, chat_id, message_id, caption):
        self.edited.append({"message_id": message_id, "caption": caption})
        # Reflect the edit so a re-run sees the message as already tidy.
        self.messages[message_id].caption = caption
        return self.messages[message_id]

    async def delete_messages(self, chat_id, message_ids):
        self.deleted.append(message_ids)


def _messages():
    return [
        # photo, no filename, no caption -> caption from post date, no download
        _msg(101, photo=SimpleNamespace(file_size=999), date=datetime(2021, 7, 9, 8, 30)),
        # document already tidy -> SKIPPED at scan
        _msg(
            102,
            document=SimpleNamespace(file_name="IMG_1.jpg", file_size=10),
            caption="#2020 #01_2020 #2020_01_02",
        ),
        # document with date in filename -> caption from filename, no download
        _msg(103, document=SimpleNamespace(file_name="IMG_20240612_193000.jpg", file_size=10)),
        # vault artifact -> ignored at scan
        _msg(104, document=SimpleNamespace(file_name="big.mp4.part001-of-002", file_size=10)),
        # text-only message -> ignored at scan
        _msg(105),
        # image document, no date in filename -> EXIF download path (falls back to post date)
        _msg(106, document=SimpleNamespace(file_name="IMG_copy.jpg", file_size=1234)),
        # document with a date filename AND existing free-text -> caption preserved
        _msg(
            107,
            document=SimpleNamespace(file_name="PXL_20230101_120000.jpg", file_size=10),
            caption="Holiday",
        ),
    ]


@pytest.fixture
def env(clean_db, tmp_path):
    client = FakeClient(_messages(), content={106: _jpeg((10, 200, 10))})
    telegram = TelegramService(client, -100123, upload_delay_seconds=0)
    recovery = RecoveryService(
        telegram, download_root=tmp_path / "recovery", delay_seconds=0
    )
    return client, recovery


async def _items():
    async with AsyncSessionLocal() as session:
        rows = (
            await session.scalars(select(RecoveryItem).order_by(RecoveryItem.tg_message_id))
        ).all()
        return {row.tg_message_id: row for row in rows}


async def test_full_tidy_flow(env):
    client, recovery = env

    # Scan: media ingested, already-tidy skipped, vault artifacts + text ignored.
    recovery.start_scan()
    await recovery._task
    assert recovery.last_error is None
    items = await _items()
    assert set(items) == {101, 102, 103, 106, 107}
    assert items[102].status == RecoveryStatus.SKIPPED
    assert items[101].status == RecoveryStatus.SCANNED

    # Rescan is idempotent.
    recovery.start_scan()
    await recovery._task
    assert len(await _items()) == 5

    # Dry run: plans captions (downloads only the EXIF-only item), no edits.
    recovery.start_run(dry_run=True)
    await recovery._task
    assert recovery.last_error is None
    items = await _items()
    assert items[101].status == RecoveryStatus.PLANNED
    assert items[101].planned_caption == "#2021 #07_2021 #2021_07_09"
    assert items[103].planned_caption == "#2024 #06_2024 #2024_06_12"
    assert items[106].planned_caption == "#2022 #03_2022 #2022_03_05"  # post-date fallback
    assert items[107].planned_caption == "#2023 #01_2023 #2023_01_01"
    assert client.downloaded == [106]  # only the filename-dateless image doc
    assert client.edited == [] and client.deleted == []

    # Real run: edits captions in place, never deletes, preserves free-text.
    recovery.start_run(dry_run=False)
    await recovery._task
    assert recovery.last_error is None
    items = await _items()
    assert all(items[i].status == RecoveryStatus.COMPLETED for i in (101, 103, 106, 107))
    assert client.deleted == []
    edited = {e["message_id"]: e["caption"] for e in client.edited}
    assert edited[101] == "#2021 #07_2021 #2021_07_09"
    assert edited[103] == "#2024 #06_2024 #2024_06_12"
    assert edited[107] == "Holiday\n\n#2023 #01_2023 #2023_01_01"  # original text kept
    # Planned items already had their caption, so the real run did not re-download.
    assert client.downloaded == [106]


async def test_free_space_floor_defers_downloads(clean_db, tmp_path):
    client = FakeClient(_messages(), content={106: _jpeg((10, 200, 10))})
    telegram = TelegramService(client, -100123, upload_delay_seconds=0)
    # An impossibly high floor: any download would breach it.
    recovery = RecoveryService(
        telegram,
        download_root=tmp_path / "recovery",
        delay_seconds=0,
        min_free_bytes=10**18,
    )

    recovery.start_scan()
    await recovery._task
    recovery.start_run(dry_run=False)
    await recovery._task

    items = await _items()
    # In-place items are still tidied; the EXIF download is deferred (stays SCANNED).
    assert items[101].status == RecoveryStatus.COMPLETED
    assert items[103].status == RecoveryStatus.COMPLETED
    assert items[106].status == RecoveryStatus.SCANNED
    assert client.downloaded == []
    assert recovery._last_batch["deferred"] == 1


async def test_batch_size_limits_work_per_run(clean_db, tmp_path):
    client = FakeClient(_messages(), content={106: _jpeg((10, 200, 10))})
    telegram = TelegramService(client, -100123, upload_delay_seconds=0)
    recovery = RecoveryService(
        telegram, download_root=tmp_path / "recovery", delay_seconds=0, batch_size=2
    )

    recovery.start_scan()
    await recovery._task
    recovery.start_run(dry_run=False)
    await recovery._task

    completed = sum(1 for it in (await _items()).values() if it.status == RecoveryStatus.COMPLETED)
    assert completed == 2  # only one batch of two items processed
    assert recovery._last_batch["total"] == 2


async def test_busy_guard(env):
    _, recovery = env
    recovery.start_scan()
    with pytest.raises(Exception):
        recovery.start_scan()
    await recovery._task
