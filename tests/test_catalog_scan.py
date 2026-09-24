"""Catalog schema and channel scan."""
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.database import (
    AsyncSessionLocal,
    CatalogItem,
    CatalogSource,
    ChannelRole,
)


async def test_a_catalog_row_defaults_to_unknown_provenance_and_no_enrichment(clean_db):
    async with AsyncSessionLocal() as session:
        session.add(
            CatalogItem(
                channel_id=-1002637897512,
                tg_message_id=1,
                channel_role=ChannelRole.ARCHIVE,
                media_kind="document",
                file_name="PXL_20230331_135108850.jpg",
                file_size=2_400_000,
                message_date=datetime(2025, 5, 3, 2, 7, 53),
            )
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))

    assert item.source is CatalogSource.UNKNOWN
    assert item.artifact is None
    assert item.enriched_at is None
    assert item.taken_at is None
    assert item.gps_lat is None
    assert item.exported_path is None


async def test_the_same_message_cannot_be_catalogued_twice_in_one_channel(clean_db):
    async with AsyncSessionLocal() as session:
        session.add_all(
            [
                CatalogItem(
                    channel_id=-100,
                    tg_message_id=7,
                    channel_role=ChannelRole.ARCHIVE,
                    media_kind="photo",
                ),
                CatalogItem(
                    channel_id=-100,
                    tg_message_id=7,
                    channel_role=ChannelRole.ARCHIVE,
                    media_kind="photo",
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_the_same_message_id_in_two_channels_is_two_rows(clean_db):
    """Message ids restart per channel, so the key must be the pair."""
    async with AsyncSessionLocal() as session:
        session.add_all(
            [
                CatalogItem(
                    channel_id=-100,
                    tg_message_id=7,
                    channel_role=ChannelRole.ARCHIVE,
                    media_kind="photo",
                ),
                CatalogItem(
                    channel_id=-200,
                    tg_message_id=7,
                    channel_role=ChannelRole.ARCHIVE,
                    media_kind="photo",
                ),
            ]
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        assert len((await session.scalars(select(CatalogItem))).all()) == 2


from types import SimpleNamespace

from app.services.catalog import (
    CatalogService,
    ChannelSpec,
    channel_spec_or_none,
    classify_artifact,
)


def _doc(mid, name, size=1000, date=None):
    return SimpleNamespace(
        id=mid,
        photo=None,
        video=None,
        animation=None,
        document=SimpleNamespace(file_name=name, file_size=size, mime_type="image/jpeg"),
        caption=None,
        date=date or datetime(2025, 6, 1, 12, 0, 0),
        empty=False,
    )


def _photo(mid, size=5000, date=None):
    return SimpleNamespace(
        id=mid,
        photo=SimpleNamespace(file_size=size),
        video=None,
        animation=None,
        document=None,
        caption=None,
        date=date or datetime(2025, 6, 1, 12, 0, 0),
        empty=False,
    )


class FakeClient:
    """Duck-types only get_chat_history, which is all the scan uses."""

    def __init__(self, history: dict[int, list]):
        self.history = history
        self.calls: list[int] = []

    async def get_chat_history(self, chat_id):
        self.calls.append(chat_id)
        for message in self.history.get(chat_id, []):
            yield message


def test_chunk_parts_and_manifests_are_classified_and_plain_names_are_not():
    assert classify_artifact("movie.mp4.part001-of-012") == "chunk"
    assert classify_artifact("movie.mp4.manifest.json") == "manifest"
    assert classify_artifact("PXL_20230331_135108850.jpg") is None
    assert classify_artifact(None) is None


def test_a_numeric_channel_id_becomes_a_channel_spec():
    spec = channel_spec_or_none("TELEGRAM_CHANNEL_ID", -100, ChannelRole.ARCHIVE)
    assert spec == ChannelSpec(-100, ChannelRole.ARCHIVE)


def test_a_username_channel_id_is_skipped_rather_than_crashing_a_scan():
    """catalog_items.channel_id is a BigInteger — a channel addressed by
    username has no numeric id to catalogue and must be skipped, not raise."""
    spec = channel_spec_or_none("TELEGRAM_CHANNEL_ID", "@somechannel", ChannelRole.ARCHIVE)
    assert spec is None


async def test_a_scan_ingests_every_media_message_with_its_channel_and_role(clean_db):
    client = FakeClient({-100: [_doc(1, "a.jpg"), _photo(2), _doc(3, "b.mp4.part001-of-002")]})
    service = CatalogService(client, [ChannelSpec(-100, ChannelRole.ARCHIVE)])

    result = await service.scan_channel(ChannelSpec(-100, ChannelRole.ARCHIVE))

    assert result == {"scanned": 3, "ingested": 3, "updated": 0}
    async with AsyncSessionLocal() as session:
        rows = {i.tg_message_id: i for i in (await session.scalars(select(CatalogItem))).all()}
    assert rows[1].media_kind == "document" and rows[1].artifact is None
    assert rows[2].media_kind == "photo" and rows[2].file_name is None
    assert rows[3].artifact == "chunk"
    assert all(r.channel_id == -100 for r in rows.values())
    assert all(r.channel_role is ChannelRole.ARCHIVE for r in rows.values())


async def test_rescanning_preserves_enrichment_and_export_state(clean_db):
    """The scan is a cache refresh, not a reset: it must never lose derived work."""
    client = FakeClient({-100: [_doc(1, "a.jpg")]})
    spec = ChannelSpec(-100, ChannelRole.ARCHIVE)
    service = CatalogService(client, [spec])
    await service.scan_channel(spec)

    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
        item.taken_at = datetime(2023, 3, 31, 15, 51, 8)
        item.gps_lat, item.gps_lon = 44.49, 11.34
        item.enriched_at = datetime(2026, 9, 21, 10, 0, 0)
        item.exported_path = "2023/2023-03-31/a.jpg"
        await session.commit()

    second = await service.scan_channel(spec)

    assert second == {"scanned": 1, "ingested": 0, "updated": 0}
    async with AsyncSessionLocal() as session:
        item = await session.scalar(select(CatalogItem))
    assert item.taken_at == datetime(2023, 3, 31, 15, 51, 8)
    assert item.gps_lat == 44.49
    assert item.exported_path == "2023/2023-03-31/a.jpg"


async def test_a_changed_file_size_updates_the_row_without_clearing_enrichment(clean_db):
    spec = ChannelSpec(-100, ChannelRole.ARCHIVE)
    service = CatalogService(FakeClient({-100: [_doc(1, "a.jpg", size=1000)]}), [spec])
    await service.scan_channel(spec)

    grown = CatalogService(FakeClient({-100: [_doc(1, "a.jpg", size=2000)]}), [spec])
    result = await grown.scan_channel(spec)

    assert result == {"scanned": 1, "ingested": 0, "updated": 1}
    async with AsyncSessionLocal() as session:
        assert (await session.scalar(select(CatalogItem))).file_size == 2000


async def test_scan_all_covers_every_configured_channel_with_its_own_role(clean_db):
    client = FakeClient({-100: [_doc(1, "a.jpg")], -200: [_photo(1)]})
    service = CatalogService(
        client,
        [ChannelSpec(-100, ChannelRole.ARCHIVE), ChannelSpec(-200, ChannelRole.MIRROR)],
    )

    results = await service.scan_all()

    assert set(results) == {"-100", "-200"}
    async with AsyncSessionLocal() as session:
        roles = {
            (i.channel_id, i.channel_role)
            for i in (await session.scalars(select(CatalogItem))).all()
        }
    assert roles == {(-100, ChannelRole.ARCHIVE), (-200, ChannelRole.MIRROR)}


async def test_a_service_with_no_channels_scans_nothing_rather_than_erroring(clean_db):
    """An unset IPHONE_CHANNEL_ID must be a no-op, not a crash on boot."""
    service = CatalogService(FakeClient({}), [])
    assert await service.scan_all() == {}
