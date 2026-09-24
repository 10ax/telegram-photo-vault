"""Characterisation tests for the hand-rolled, additive-only migrations.

There is no Alembic here: `init_db()` is `create_all` plus a `PRAGMA
table_info` / `ALTER TABLE ADD COLUMN` pass. That means a column added to a
model must also be listed in `_COLUMN_MIGRATIONS`, and must be nullable or
defaulted — an existing SQLite file is upgraded in place, never rebuilt.
"""
from app.models.database import (
    _COLUMN_MIGRATIONS,
    Base,
    CatalogItem,
    DeletionAudit,
    DeviceFinding,
    DeviceSnapshot,
    Photo,
    RecoveryItem,
    UploadChunk,
    engine,
    init_db,
)

# The shape the database had before chunking, browse mirroring and the recovery
# subsystem existed — i.e. what a long-running deployment still has on disk.
LEGACY_PHOTOS = """
CREATE TABLE photos (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    mega_path VARCHAR(1024) NOT NULL UNIQUE,
    local_path VARCHAR(1024),
    compressed_path VARCHAR(1024),
    status VARCHAR(16) NOT NULL,
    tg_message_id BIGINT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    error_log TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""

LEGACY_RECOVERY_ITEMS = """
CREATE TABLE recovery_items (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    tg_message_id BIGINT NOT NULL UNIQUE,
    media_kind VARCHAR(32) NOT NULL,
    file_name VARCHAR(512),
    file_size BIGINT,
    message_date DATETIME,
    status VARCHAR(16) NOT NULL,
    local_path VARCHAR(1024),
    sha256 VARCHAR(64),
    planned_caption TEXT,
    new_tg_message_id BIGINT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    error_log TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""


async def _columns(table: str) -> set[str]:
    async with engine.begin() as conn:
        result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
        return {row[1] for row in result.fetchall()}


async def _install_legacy_schema() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.exec_driver_sql(LEGACY_PHOTOS)
        await conn.exec_driver_sql(LEGACY_RECOVERY_ITEMS)


async def test_init_db_adds_every_column_a_legacy_database_is_missing():
    await _install_legacy_schema()
    before = await _columns("photos")
    assert "is_chunked" not in before

    await init_db()

    try:
        photos = await _columns("photos")
        assert set(_COLUMN_MIGRATIONS["photos"]) <= photos
        # Every mapped column of the model is now present.
        assert {column.name for column in Photo.__table__.columns} <= photos

        recovery = await _columns("recovery_items")
        assert set(_COLUMN_MIGRATIONS["recovery_items"]) <= recovery
        assert {column.name for column in RecoveryItem.__table__.columns} <= recovery

        # Tables that did not exist at all are created outright.
        chunks = await _columns("upload_chunks")
        assert {column.name for column in UploadChunk.__table__.columns} == chunks
    finally:
        await engine.dispose()


async def test_init_db_is_idempotent():
    await _install_legacy_schema()
    await init_db()
    after_first = await _columns("photos")

    await init_db()  # a restart must not try to re-add the same columns

    try:
        assert await _columns("photos") == after_first
    finally:
        await engine.dispose()


async def test_migrated_columns_are_all_nullable_or_defaulted():
    """An ALTER TABLE ADD COLUMN on a populated table cannot add a bare NOT NULL."""
    await _install_legacy_schema()
    await init_db()

    try:
        for table, columns in _COLUMN_MIGRATIONS.items():
            async with engine.begin() as conn:
                result = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
                info = {row[1]: row for row in result.fetchall()}
            for column in columns:
                _, _, _, not_null, default, _ = info[column]
                assert not not_null or default is not None, (
                    f"{table}.{column} is NOT NULL without a default"
                )
    finally:
        await engine.dispose()


def test_every_migration_target_exists_on_its_model():
    """A typo in _COLUMN_MIGRATIONS would add a column nothing ever reads."""
    tables = {"photos": Photo, "recovery_items": RecoveryItem, "catalog_items": CatalogItem}
    for table, columns in _COLUMN_MIGRATIONS.items():
        mapped = {column.name for column in tables[table].__table__.columns}
        assert set(columns) <= mapped, f"{table}: {set(columns) - mapped}"


LEGACY_CATALOG_ITEMS = """
CREATE TABLE catalog_items (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    channel_id BIGINT NOT NULL,
    tg_message_id BIGINT NOT NULL,
    channel_role VARCHAR(8) NOT NULL,
    media_kind VARCHAR(32) NOT NULL,
    artifact VARCHAR(16),
    file_name VARCHAR(512),
    file_size BIGINT,
    message_date DATETIME,
    sha256 VARCHAR(64),
    source VARCHAR(16) NOT NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""


async def test_device_tables_are_created_on_a_legacy_database():
    """The three device tables appear on a database that predates them."""
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.exec_driver_sql(LEGACY_PHOTOS)

        await init_db()

        async with engine.begin() as conn:
            # The names come from the models, so renaming a __tablename__ without
            # updating this test fails here instead of passing quietly.
            for model in (DeviceSnapshot, DeviceFinding, DeletionAudit):
                table = model.__tablename__
                result = await conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
                )
                assert result.fetchone() is not None, f"{table} was not created"
    finally:
        await engine.dispose()


async def test_catalog_items_gains_the_chunked_columns_in_place():
    """A catalog_items written before this feature is upgraded, not rebuilt."""
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.exec_driver_sql(LEGACY_CATALOG_ITEMS)
            await conn.exec_driver_sql(
                "INSERT INTO catalog_items "
                "(channel_id, tg_message_id, channel_role, media_kind, source, "
                " file_name, created_at, updated_at) "
                "VALUES (-1, 1, 'ARCHIVE', 'document', 'UNKNOWN', 'keep.jpg', "
                " '2026-01-01', '2026-01-01')"
            )

        await init_db()

        async with engine.begin() as conn:
            info = await conn.exec_driver_sql("PRAGMA table_info(catalog_items)")
            columns = {row[1] for row in info.fetchall()}
            assert {"chunked_original_name", "chunked_total_size", "chunked_sha256"} <= columns

            rows = await conn.exec_driver_sql("SELECT file_name FROM catalog_items")
            assert [r[0] for r in rows.fetchall()] == ["keep.jpg"], "existing rows survive"
    finally:
        await engine.dispose()


async def test_every_new_column_is_nullable_or_defaulted():
    """The additive rule: ALTER TABLE ADD COLUMN cannot add a bare NOT NULL."""
    for table, columns in _COLUMN_MIGRATIONS.items():
        for name, ddl in columns.items():
            upper = ddl.upper()
            assert "NOT NULL" not in upper or "DEFAULT" in upper, (
                f"{table}.{name} is NOT NULL without a DEFAULT: {ddl}"
            )
