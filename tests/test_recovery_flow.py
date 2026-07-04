"""Functional test of the channel recovery flow against a fake Pyrogram client."""
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
        self.messages = messages
        self.content = content
        self.sent = []
        self.deleted = []

    async def get_chat_history(self, chat_id):
        for message in self.messages:
            yield message

    async def get_messages(self, chat_id, message_id):
        return next((m for m in self.messages if m.id == message_id), None)

    async def download_media(self, message, file_name):
        Path(file_name).parent.mkdir(parents=True, exist_ok=True)
        Path(file_name).write_bytes(self.content[message.id])
        return file_name

    async def send_document(self, chat_id, document, caption=None, file_name=None):
        self.sent.append({"caption": caption, "file_name": file_name})
        return SimpleNamespace(id=1000 + len(self.sent))

    async def delete_messages(self, chat_id, message_ids):
        self.deleted.append(message_ids)


@pytest.fixture
def env(clean_db, tmp_path):
    messages = [
        _msg(101, photo=SimpleNamespace(file_size=999), date=datetime(2021, 7, 9, 8, 30)),
        _msg(
            102,
            document=SimpleNamespace(file_name="IMG_1.jpg", file_size=10),
            caption="#2020 #01_2020 #2020_01_02",
        ),
        _msg(103, document=SimpleNamespace(file_name="IMG_20240612_193000.jpg", file_size=10)),
        _msg(104, document=SimpleNamespace(file_name="big.mp4.part001-of-002", file_size=10)),
        _msg(105),
        _msg(106, document=SimpleNamespace(file_name="IMG_copy.jpg", file_size=10)),
    ]
    content = {101: _jpeg((200, 10, 10)), 103: _jpeg((10, 200, 10)), 106: _jpeg((10, 200, 10))}
    client = FakeClient(messages, content)
    telegram = TelegramService(client, -100123, upload_delay_seconds=0)
    recovery = RecoveryService(
        telegram, download_root=tmp_path / "recovery", delay_seconds=0, delete_old=True
    )
    return client, recovery


async def _items():
    async with AsyncSessionLocal() as session:
        rows = (
            await session.scalars(select(RecoveryItem).order_by(RecoveryItem.tg_message_id))
        ).all()
        return {row.tg_message_id: row for row in rows}


async def test_full_recovery_flow(env):
    client, recovery = env

    # Scan: media ingested, tidy doc skipped, vault artifacts + text ignored.
    recovery.start_scan()
    await recovery._task
    assert recovery.last_error is None
    items = await _items()
    assert set(items) == {101, 102, 103, 106}
    assert items[102].status == RecoveryStatus.SKIPPED
    assert items[101].status == RecoveryStatus.SCANNED

    # Rescan is idempotent.
    recovery.start_scan()
    await recovery._task
    assert len(await _items()) == 4

    # Dry run: plans captions, flags the duplicate, touches nothing on Telegram.
    recovery.start_run(dry_run=True)
    await recovery._task
    assert recovery.last_error is None
    items = await _items()
    assert items[101].status == RecoveryStatus.PLANNED
    assert items[103].status == RecoveryStatus.PLANNED
    assert items[106].status == RecoveryStatus.DUPLICATE
    assert items[101].planned_caption == "#2021 #07_2021 #2021_07_09"
    assert items[103].planned_caption == "#2024 #06_2024 #2024_06_12"
    assert client.sent == [] and client.deleted == []
    assert Path(items[101].local_path).is_file()

    # Real run: re-uploads tidy documents, deletes originals, cleans up.
    recovery.start_run(dry_run=False)
    await recovery._task
    assert recovery.last_error is None
    items = await _items()
    assert items[101].status == RecoveryStatus.COMPLETED
    assert items[103].status == RecoveryStatus.COMPLETED
    assert client.deleted == [101, 103]
    assert len(client.sent) == 2
    names = [entry["file_name"] for entry in client.sent]
    assert names[0].startswith("photo_20210709")
    assert names[1] == "IMG_20240612_193000.jpg"
    assert items[101].new_tg_message_id is not None
    assert items[101].local_path is None


async def test_busy_guard(env):
    _, recovery = env
    recovery.start_scan()
    with pytest.raises(Exception):
        recovery.start_scan()
    await recovery._task
