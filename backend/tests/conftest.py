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


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_tmdb_lookup: call the real lookup_original_language (stub its HTTP yourself)")


@pytest.fixture(autouse=True)
def original_languages(request, monkeypatch):
    """No test asks TMDB for an original language: scans, the watcher and
    the routes get it from this dict (path -> language), else None. A test
    marked real_tmdb_lookup gets the real lookup."""
    found: dict[str, str] = {}
    if request.node.get_closest_marker("real_tmdb_lookup") is None:
        import backend.metadata as metadata

        async def lookup(path):
            return found.get(path)
        monkeypatch.setattr(metadata, "lookup_original_language", lookup)
    return found


def pytest_runtest_logreport(report):
    """On GitHub Actions, a failing test is also an annotation ("::error"):
    the API lists annotations to anyone, the job's log only to a signed-in
    admin."""
    if report.failed and os.environ.get("GITHUB_ACTIONS") == "true":
        text = str(report.longrepr)[-1500:]
        text = text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
        print(f"\n::error title={report.nodeid}::{text}", flush=True)
