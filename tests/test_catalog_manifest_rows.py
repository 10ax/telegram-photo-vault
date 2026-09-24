import json
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app.models.database import AsyncSessionLocal, CatalogItem, ChannelRole
from app.services.catalog import CatalogService, ChannelSpec
from app.services.chunking import MANIFEST_KIND

CHANNEL = -1002637897512


def _manifest_bytes(*, name="movie.mp4", total_size=5_000_000_000, sha="a" * 64, count=3):
    return json.dumps(
        {
            "manifest_version": 1,
            "kind": MANIFEST_KIND,
            "original_filename": name,
            "total_size": total_size,
            "sha256": sha,
            "chunk_size": 1_950_000_000,
            "chunk_count": count,
            "chunks": [],
        }
    ).encode()


class FakeClient:
    """Duck-types only download_media, returning bytes for known message ids."""

    def __init__(self, payloads: dict[int, bytes]):
        self.payloads = payloads
        self.downloaded: list[int] = []

    async def download_media(self, message, in_memory=True):
        self.downloaded.append(message.id)
        payload = self.payloads.get(message.id)
        if payload is None:
            raise FileNotFoundError(f"no payload for {message.id}")
        return SimpleNamespace(getvalue=lambda: payload)

    async def get_messages(self, chat_id, message_ids):
        return SimpleNamespace(id=message_ids)


async def _add(session, **kwargs):
    row = CatalogItem(
        channel_id=CHANNEL,
        channel_role=ChannelRole.ARCHIVE,
        media_kind="document",
        message_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
        **kwargs,
    )
    session.add(row)
    return row


async def test_manifest_row_gains_the_originals_identity(clean_db):
    async with AsyncSessionLocal() as session:
        await _add(session, tg_message_id=10, artifact="manifest",
                   file_name="movie.mp4.manifest.json", file_size=800)
        await session.commit()

    service = CatalogService(
        FakeClient({10: _manifest_bytes()}),
        [ChannelSpec(channel_id=CHANNEL, role=ChannelRole.ARCHIVE)],
    )
    result = await service.resolve_manifests()

    assert result["resolved"] == 1
    async with AsyncSessionLocal() as session:
        row = await session.scalar(select(CatalogItem).where(CatalogItem.tg_message_id == 10))
        assert row.chunked_original_name == "movie.mp4"
        assert row.chunked_total_size == 5_000_000_000
        assert row.chunked_sha256 == "a" * 64


async def test_chunk_parts_and_plain_documents_are_never_resolved(clean_db):
    async with AsyncSessionLocal() as session:
        await _add(session, tg_message_id=11, artifact="chunk",
                   file_name="movie.mp4.part001-of-003", file_size=1_950_000_000)
        await _add(session, tg_message_id=12, artifact=None,
                   file_name="PXL_20260713_115033830.jpg", file_size=3_412_887)
        await session.commit()

    client = FakeClient({})
    service = CatalogService(client, [ChannelSpec(channel_id=CHANNEL, role=ChannelRole.ARCHIVE)])
    result = await service.resolve_manifests()

    assert result["resolved"] == 0
    assert client.downloaded == []


async def test_a_malformed_manifest_is_attempted_once_and_recorded(clean_db):
    async with AsyncSessionLocal() as session:
        await _add(session, tg_message_id=13, artifact="manifest",
                   file_name="broken.manifest.json", file_size=12)
        await session.commit()

    client = FakeClient({13: b"not json at all"})
    service = CatalogService(client, [ChannelSpec(channel_id=CHANNEL, role=ChannelRole.ARCHIVE)])

    first = await service.resolve_manifests()
    assert first["failed"] == 1

    second = await service.resolve_manifests()
    assert second["resolved"] == 0 and second["failed"] == 0
    assert client.downloaded == [13], "a permanently broken manifest must not be retried forever"

    async with AsyncSessionLocal() as session:
        row = await session.scalar(select(CatalogItem).where(CatalogItem.tg_message_id == 13))
        assert row.enrich_error is not None
        assert row.chunked_original_name is None


async def test_resolution_is_bounded_and_resumable(clean_db):
    payloads = {}
    async with AsyncSessionLocal() as session:
        for mid in range(20, 25):
            await _add(session, tg_message_id=mid, artifact="manifest",
                       file_name=f"f{mid}.mp4.manifest.json", file_size=700)
            payloads[mid] = _manifest_bytes(name=f"f{mid}.mp4", sha=str(mid) * 32)
        await session.commit()

    service = CatalogService(FakeClient(payloads),
                             [ChannelSpec(channel_id=CHANNEL, role=ChannelRole.ARCHIVE)])

    first = await service.resolve_manifests(limit=2)
    assert first == {"resolved": 2, "failed": 0, "remaining": 3}

    second = await service.resolve_manifests(limit=10)
    assert second == {"resolved": 3, "failed": 0, "remaining": 0}


async def test_a_mirror_channel_manifest_is_left_alone(clean_db):
    async with AsyncSessionLocal() as session:
        row = CatalogItem(
            channel_id=-1004367643112,
            tg_message_id=30,
            channel_role=ChannelRole.MIRROR,
            media_kind="document",
            artifact="manifest",
            file_name="movie.mp4.manifest.json",
            file_size=800,
        )
        session.add(row)
        await session.commit()

    client = FakeClient({30: _manifest_bytes()})
    service = CatalogService(client, [ChannelSpec(channel_id=-1004367643112, role=ChannelRole.MIRROR)])
    result = await service.resolve_manifests()

    assert result["resolved"] == 0
    assert client.downloaded == []
