import pytest
import pytest_asyncio
import aiosqlite
import os

@pytest_asyncio.fixture
async def test_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    os.environ["SHRINKARR_DB_PATH"] = db_path
    import backend.config
    from backend.config import Settings
    backend.config.settings = Settings(db_path=db_path, media_root=str(tmp_path / "media"))
    import backend.database
    backend.database.DB_PATH = db_path
    await backend.database.init_db()
    yield db_path
