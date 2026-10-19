"""A background task a test leaves running (fire-and-forget, as telling
Sonarr / Radarr after a remote worker's completion is) finishes on the
test's loop: cut off mid-query when the loop closed, its database
connection's thread waited forever and the test run couldn't exit (CI ran
into its 20-minute limit after every test passed). These two run in order."""
import asyncio
import threading

import aiosqlite
import pytest


@pytest.mark.asyncio
async def test_a_test_leaves_a_task_holding_a_connection(test_db):
    async def tell_someone():
        db = await aiosqlite.connect(test_db)  # cancelled in here, it's never closed
        try:
            await asyncio.sleep(0.3)  # Sonarr taking its time
        finally:
            await db.close()

    asyncio.create_task(tell_someone())
    await asyncio.sleep(0)  # it has started connecting when the test ends


def test_no_connection_is_left_open():
    import aiosqlite.core
    assert not [t for t in threading.enumerate() if isinstance(t, aiosqlite.core.Connection) and t.is_alive()]
