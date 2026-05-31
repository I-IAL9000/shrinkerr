"""Tests for backend/database.py.

v0.7.19: lock down the global aiosqlite.connect monkey-patch that
applies `busy_timeout` to every new connection. The patch fixes the
"database is locked" failure class — SQLite's busy_timeout is
per-connection (defaults to 0 = fail immediately), so without the
patch, the 118 connect sites that don't set it themselves race
against each writer and error out instantly under contention.
"""
import pytest
import aiosqlite

# Importing backend.database installs the monkey-patch at module load.
import backend.database  # noqa: F401
from backend.database import BUSY_TIMEOUT


@pytest.mark.asyncio
async def test_busy_timeout_applied_on_await_pattern():
    """`db = await aiosqlite.connect(...)` should yield a connection with
    busy_timeout set to BUSY_TIMEOUT (60000 in v0.7.19)."""
    db = await aiosqlite.connect(":memory:")
    try:
        async with db.execute("PRAGMA busy_timeout") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == BUSY_TIMEOUT, (
            f"busy_timeout = {row[0]!r}, expected {BUSY_TIMEOUT}. The "
            f"v0.7.19 patch in backend/database.py is not firing on the "
            f"await-pattern path."
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_busy_timeout_applied_on_async_with_pattern():
    """`async with aiosqlite.connect(...) as db:` should also yield a
    connection with busy_timeout applied."""
    async with aiosqlite.connect(":memory:") as db:
        async with db.execute("PRAGMA busy_timeout") as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == BUSY_TIMEOUT, (
            f"busy_timeout = {row[0]!r}, expected {BUSY_TIMEOUT}. The "
            f"v0.7.19 patch in backend/database.py is not firing on the "
            f"async-with-pattern path."
        )


@pytest.mark.asyncio
async def test_busy_timeout_patch_is_idempotent():
    """Importing backend.database multiple times must not double-wrap
    aiosqlite.connect (would break under module reload / test re-runs)."""
    import importlib
    importlib.reload(backend.database)
    # Patch should still work after reload
    db = await aiosqlite.connect(":memory:")
    try:
        async with db.execute("PRAGMA busy_timeout") as cur:
            row = await cur.fetchone()
        assert row[0] == BUSY_TIMEOUT
    finally:
        await db.close()
