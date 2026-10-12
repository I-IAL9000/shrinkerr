import pytest
import pytest_asyncio
import aiosqlite
import os

@pytest_asyncio.fixture
async def test_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    os.environ["SHRINKERR_DB_PATH"] = db_path
    import backend.config
    from backend.config import Settings
    backend.config.settings = Settings(db_path=db_path, media_root=str(tmp_path / "media"))
    import backend.database
    backend.database.DB_PATH = db_path
    await backend.database.init_db()
    yield db_path
    # A test that asked for counts can have started the filter index's
    # background build: stop it on this test's event loop, so its database
    # thread is closed. One left running could keep the test run from
    # exiting after it passed (a CI job timed out that way).
    import asyncio
    from backend.filter_index import service
    service.reset()  # cancels a running build; it and any reset() cancelled before wait here
    while service._cancelled:
        task = service._cancelled.pop()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
