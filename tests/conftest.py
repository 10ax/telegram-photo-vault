import os
import tempfile

# Must be set before any app import binds the async engine.
_DB_FILE = tempfile.mkstemp(prefix="vault_test_", suffix=".db")[1]
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_FILE}"

import pytest

from app.models.database import Base, engine, init_db


@pytest.fixture
async def clean_db():
    """Fresh schema for the test, engine pool disposed afterwards.

    Disposal matters: each async test runs in its own event loop, and pooled
    aiosqlite connections must not leak across loops.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    yield
    await engine.dispose()
