"""Backfill copies native media into the browse channel via server-side copy."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.database import AsyncSessionLocal, RecoveryItem, RecoveryStatus
from app.services.recovery import RecoveryService
from app.services.telegram import TelegramService

MAIN = -100111
BROWSE = -100999


class FakeClient:
    def __init__(self):
        self.copies = []
        self.next_id = 9000

    async def copy_message(self, chat_id, from_chat_id, message_id, caption=None):
        assert chat_id == BROWSE and from_chat_id == MAIN
        self.next_id += 1
        self.copies.append({"message_id": message_id, "caption": caption})
        return SimpleNamespace(id=self.next_id)


def _recovery(browse=BROWSE, tmp_path=None):
    client = FakeClient()
    telegram = TelegramService(client, MAIN, upload_delay_seconds=0, browse_channel_id=browse)
    rec = RecoveryService(telegram, download_root=tmp_path / "r", delay_seconds=0)
    return client, rec


async def _seed(kind, mid, *, size=None, caption=None, name=None, date=None):
    async with AsyncSessionLocal() as session:
        session.add(RecoveryItem(
            tg_message_id=mid, media_kind=kind, file_name=name, file_size=size,
            message_date=date or datetime(2022, 3, 5, 10, 0), status=RecoveryStatus.SKIPPED,
            planned_caption=caption,
        ))
        await session.commit()


async def _items():
    async with AsyncSessionLocal() as session:
        rows = (await session.scalars(select(RecoveryItem))).all()
        return {r.tg_message_id: r for r in rows}


async def test_backfill_copies_native_media_only(clean_db, tmp_path):
    await _seed("photo", 1, caption="#2021 #07_2021 #2021_07_09")
    await _seed("video", 2, size=5_000_000, name="VID_20240101_120000.mp4")
    await _seed("animation", 3)
    await _seed("document", 4, name="IMG_1.jpg")  # excluded from the gallery

    client, rec = _recovery(tmp_path=tmp_path)
    rec.start_backfill(limit=100)
    await rec._task
    assert rec.last_error is None

    copied_ids = sorted(c["message_id"] for c in client.copies)
    assert copied_ids == [1, 2, 3]  # document (4) not copied

    items = await _items()
    assert items[1].browse_tg_message_id is not None
    assert items[4].browse_tg_message_id is None  # document untouched
    # caption comes from planned_caption, else derived from filename/date
    by_mid = {c["message_id"]: c["caption"] for c in client.copies}
    assert by_mid[1] == "#2021 #07_2021 #2021_07_09"
    assert by_mid[2] == "#2024 #01_2024 #2024_01_01"  # from filename VID_20240101_...


async def test_backfill_is_idempotent_and_resumable(clean_db, tmp_path):
    await _seed("photo", 1, caption="#2021 #07_2021 #2021_07_09")
    client, rec = _recovery(tmp_path=tmp_path)

    rec.start_backfill(limit=100)
    await rec._task
    rec.start_backfill(limit=100)  # second pass: nothing left to copy
    await rec._task

    assert len(client.copies) == 1  # not re-copied


async def test_backfill_video_size_cap(clean_db, tmp_path):
    await _seed("photo", 1, caption="#2021 #07_2021 #2021_07_09")
    await _seed("video", 2, size=200_000_000, name="VID_20240101_120000.mp4")  # 200 MB

    client, rec = _recovery(tmp_path=tmp_path)
    rec.start_backfill(limit=100, max_video_bytes=100 * 1024 * 1024)  # 100 MB cap
    await rec._task

    copied_ids = sorted(c["message_id"] for c in client.copies)
    assert copied_ids == [1]  # big video skipped
    assert rec._last_batch["skipped"] == 1
