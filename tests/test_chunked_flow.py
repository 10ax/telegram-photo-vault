"""Functional test: worker splits a large file into chunks, manifest commits the set."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.database import (
    AsyncSessionLocal,
    ChunkStatus,
    MediaType,
    Photo,
    PhotoStatus,
    UploadChunk,
)
from app.services.chunking import chunk_name
from app.worker import PhotoWorker

FILE_SIZE = 10_000_000
CHUNK_SIZE = 3_000_000


class FakeTelegram:
    def __init__(self, channel_dir: Path):
        self.channel_dir = channel_dir
        self.next_id = 500
        self.captions = {}
        self.search_hits = {}

    async def upload_document(self, file_path, **kwargs):
        raise AssertionError("whole-file upload must not be used above the chunk threshold")

    async def upload_file_object(self, file_object, caption):
        data = file_object.read()
        (self.channel_dir / file_object.name).write_bytes(data)
        self.captions[file_object.name] = caption
        self.next_id += 1
        return SimpleNamespace(id=self.next_id)

    async def upload_bytes(self, data, *, file_name, caption):
        (self.channel_dir / file_name).write_bytes(data)
        self.captions[file_name] = caption
        self.next_id += 1
        return SimpleNamespace(id=self.next_id)

    async def find_document_by_name(self, file_name):
        return self.search_hits.get(file_name)


class FakeMega:
    def __init__(self):
        self.deleted = []

    async def delete_file(self, remote_path):
        self.deleted.append(remote_path)


@pytest.fixture
def env(clean_db, tmp_path):
    channel = tmp_path / "channel"
    channel.mkdir()
    telegram = FakeTelegram(channel)
    mega = FakeMega()
    worker = PhotoWorker(
        mega,
        telegram,
        None,
        "/srv",
        download_root=tmp_path / "dl",
        compressed_root=tmp_path / "cp",
        mode="manual",
        per_file_delay=0,
        chunk_size=CHUNK_SIZE,
        chunk_threshold=4_000_000,
    )
    return worker, telegram, mega, channel, tmp_path


async def _seed_photo(local_path: Path, mega_path: str) -> int:
    async with AsyncSessionLocal() as session:
        photo = Photo(
            mega_path=mega_path,
            status=PhotoStatus.DOWNLOADED,
            media_type=MediaType.VIDEO,
            local_path=str(local_path),
        )
        session.add(photo)
        await session.commit()
        return photo.id


async def test_chunked_upload_to_completion(env):
    worker, telegram, mega, channel, tmp_path = env

    original = tmp_path / "dl" / "Camera" / "VID_20240101_120000.mp4"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(os.urandom(FILE_SIZE))
    original_bytes = original.read_bytes()
    original_sha = hashlib.sha256(original_bytes).hexdigest()

    photo_id = await _seed_photo(original, "/Camera/VID_20240101_120000.mp4")

    for _ in range(10):
        progressed = await worker._process_photo_by_id(photo_id)
        async with AsyncSessionLocal() as session:
            photo = await session.get(Photo, photo_id)
            if photo.status == PhotoStatus.COMPLETED:
                break
        assert progressed, f"no progress at status {photo.status}"

    assert photo.status == PhotoStatus.COMPLETED
    assert photo.is_chunked and photo.sha256 == original_sha
    assert photo.total_size == FILE_SIZE
    assert photo.manifest_tg_message_id is not None
    assert mega.deleted == ["/Camera/VID_20240101_120000.mp4"]
    assert not original.exists()

    async with AsyncSessionLocal() as session:
        chunks = (
            await session.scalars(select(UploadChunk).order_by(UploadChunk.part_index))
        ).all()
    assert len(chunks) == 4
    assert all(c.status == ChunkStatus.UPLOADED and c.tg_message_id for c in chunks)

    part_names = sorted(p.name for p in channel.glob("*.part*"))
    assert part_names == [
        f"VID_20240101_120000.mp4.part{i:03d}-of-004" for i in range(1, 5)
    ]

    manifest = json.loads((channel / "VID_20240101_120000.mp4.manifest.json").read_text())
    assert manifest["kind"] == "telegram-photo-vault/chunked-file"
    assert manifest["sha256"] == original_sha
    assert manifest["chunk_count"] == 4
    assert manifest["source"]["capture_datetime_source"] == "filename"

    caption = telegram.captions["VID_20240101_120000.mp4.part002-of-004"]
    assert "#2024" in caption and "#chunked" in caption and "#part002_of_004" in caption
    assert f"sha256={original_sha[:16]}" in caption
    assert "#manifest" in telegram.captions["VID_20240101_120000.mp4.manifest.json"]

    # cat-merge equivalence: parts concatenated are byte-exact
    joined = b"".join((channel / name).read_bytes() for name in part_names)
    assert joined == original_bytes


async def test_crash_window_reuses_existing_chunk(env):
    worker, telegram, mega, channel, tmp_path = env

    source = tmp_path / "second.mp4"
    source.write_bytes(os.urandom(5_000_000))
    photo_id = await _seed_photo(source, "/Camera/second.mp4")

    await worker._process_photo_by_id(photo_id)  # prepare: 2 chunk rows

    telegram.search_hits[chunk_name("second.mp4", 1, 2)] = SimpleNamespace(id=42)
    sent_before = telegram.next_id
    await worker._process_photo_by_id(photo_id)  # chunk 1 found in channel, reused
    assert telegram.next_id == sent_before

    async with AsyncSessionLocal() as session:
        first_chunk = await session.scalar(
            select(UploadChunk).where(
                UploadChunk.photo_id == photo_id, UploadChunk.part_index == 1
            )
        )
    assert first_chunk.status == ChunkStatus.UPLOADED
    assert first_chunk.tg_message_id == 42


async def test_small_files_still_upload_whole(env):
    worker, telegram, mega, channel, tmp_path = env

    uploads = []

    async def upload_document(file_path, **kwargs):
        uploads.append(str(file_path))
        return SimpleNamespace(id=777)

    telegram.upload_document = upload_document

    source = tmp_path / "small.mp4"
    source.write_bytes(os.urandom(1_000_000))
    photo_id = await _seed_photo(source, "/Camera/small.mp4")

    await worker._process_photo_by_id(photo_id)
    async with AsyncSessionLocal() as session:
        photo = await session.get(Photo, photo_id)
    assert photo.status == PhotoStatus.TG_UPLOADED
    assert photo.tg_message_id == 777
    assert not photo.is_chunked
    assert uploads == [str(source)]
