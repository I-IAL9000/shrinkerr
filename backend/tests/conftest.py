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
    from backend.filter_index import service
    task = service._state.rebuild
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except BaseException:
            pass
    service.reset()
