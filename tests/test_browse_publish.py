"""The worker mirrors native captioned copies to the shared browse channel."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.database import AsyncSessionLocal, MediaType, Photo, PhotoStatus
from app.worker import PhotoWorker


class FakeTelegram:
    def __init__(self, *, browse_channel_id=None):
        self.browse_channel_id = browse_channel_id
        self.next_id = 500
        self.docs = []
        self.media = []
        self.browsed = []

    async def upload_document(self, file_path, **kwargs):
        self.next_id += 1
        self.docs.append(str(file_path))
        return SimpleNamespace(id=self.next_id)

    async def upload_media(self, file_path, media_type, **kwargs):
        self.next_id += 1
        self.media.append(str(file_path))
        return SimpleNamespace(id=self.next_id)

    async def publish_browse(self, file_path, media_type, **kwargs):
        if self.browse_channel_id is None:
            return None
        self.next_id += 1
        self.browsed.append((str(file_path), media_type))
        return SimpleNamespace(id=self.next_id)


class FakeMega:
    async def delete_file(self, remote_path):
        pass


def _worker(telegram, tmp_path, *, browse_max_video_mb=0):
    return PhotoWorker(
        FakeMega(),
        telegram,
        None,
        "/srv",
        download_root=tmp_path / "dl",
        compressed_root=tmp_path / "cp",
        mode="manual",
        per_file_delay=0,
        chunk_threshold=4_000_000,
        browse_max_video_mb=browse_max_video_mb,
    )


async def _seed(local_path: Path, media_type: MediaType) -> int:
    async with AsyncSessionLocal() as session:
        photo = Photo(
            mega_path=f"/Camera/{local_path.name}",
            status=PhotoStatus.DOWNLOADED,
            media_type=media_type,
            local_path=str(local_path),
        )
        session.add(photo)
        await session.commit()
        return photo.id


async def _run_downloaded(worker, photo_id):
    async with AsyncSessionLocal() as session:
        photo = await session.get(Photo, photo_id)
        await worker._handle_downloaded(session, photo)
        await session.commit()
        return photo


@pytest.fixture
def img(tmp_path):
    p = tmp_path / "dl" / "IMG_20240612_193000.jpg"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(os.urandom(1000))
    return p


async def test_photo_is_mirrored_to_browse_channel(clean_db, tmp_path, img):
    telegram = FakeTelegram(browse_channel_id=-100999)
    worker = _worker(telegram, tmp_path)
    photo_id = await _seed(img, MediaType.IMAGE)

    photo = await _run_downloaded(worker, photo_id)

    assert telegram.docs == [str(img)]  # archival document still uploaded
    assert telegram.browsed == [(str(img), MediaType.IMAGE)]
    assert photo.browse_tg_message_id is not None


async def test_no_browse_channel_means_no_mirror(clean_db, tmp_path, img):
    telegram = FakeTelegram(browse_channel_id=None)
    worker = _worker(telegram, tmp_path)
    photo_id = await _seed(img, MediaType.IMAGE)

    photo = await _run_downloaded(worker, photo_id)

    assert telegram.browsed == []
    assert photo.browse_tg_message_id is None


async def test_small_video_gated_by_threshold(clean_db, tmp_path):
    vid = tmp_path / "dl" / "VID_20240101_120000.mp4"
    vid.parent.mkdir(parents=True, exist_ok=True)
    vid.write_bytes(os.urandom(2_000_000))  # 2 MB

    # Default (browse_max_video_mb=0): photos only, video skipped.
    telegram = FakeTelegram(browse_channel_id=-100999)
    photo_id = await _seed(vid, MediaType.VIDEO)
    await _run_downloaded(_worker(telegram, tmp_path), photo_id)
    assert telegram.browsed == []

    # With a 5 MB allowance the 2 MB clip is mirrored.
    telegram2 = FakeTelegram(browse_channel_id=-100999)
    vid2 = tmp_path / "dl" / "VID_20240101_120001.mp4"
    vid2.write_bytes(os.urandom(2_000_000))
    photo_id2 = await _seed(vid2, MediaType.VIDEO)
    await _run_downloaded(_worker(telegram2, tmp_path, browse_max_video_mb=5), photo_id2)
    assert telegram2.browsed == [(str(vid2), MediaType.VIDEO)]
