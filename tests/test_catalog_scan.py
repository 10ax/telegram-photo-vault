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
