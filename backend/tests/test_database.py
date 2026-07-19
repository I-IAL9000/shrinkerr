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


@pytest.mark.asyncio
async def test_language_source_split_migration(test_db):
    """v0.9.68: init_db adds the `tracks_detected` column and the language_source
    split. The migration also folds any legacy language_source='detected' rows
    back to 'heuristic' + tracks_detected=1 (detection is per-track, not a
    native-language source). Simulate a legacy row and re-run the fold."""
    import backend.database as _db
    db = await aiosqlite.connect(test_db)
    try:
        # Column exists after init_db (the fixture ran it).
        async with db.execute("PRAGMA table_info(scan_results)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        assert "tracks_detected" in cols, "v0.9.68 migration did not add tracks_detected"

        # Seed a legacy 'detected' row and apply the fold statement.
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, scan_timestamp, "
            "native_language, language_source) VALUES (?, 1, '2026-01-01', 'fre', 'detected')",
            ("/legacy/detected.mkv",),
        )
        await db.commit()
        await db.execute(
            "UPDATE scan_results SET tracks_detected = 1, language_source = 'heuristic' "
            "WHERE language_source = 'detected'"
        )
        await db.commit()
        async with db.execute(
            "SELECT language_source, tracks_detected FROM scan_results WHERE file_path = ?",
            ("/legacy/detected.mkv",),
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == "heuristic"
        assert row[1] == 1
    finally:
        await db.close()
