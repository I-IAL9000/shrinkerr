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
    # The whisper worker a conversion started, and its idle timer: stopped on
    # this loop too (the fixture below kills one left by a test without a
    # database).
    import sys
    detection = sys.modules.get("backend.language_detection")
    worker = getattr(detection, "_whisper_worker", None)
    if worker is not None and (worker._proc is not None or worker._idle_task is not None):
        await worker.shutdown()
    # Other background tasks the test left (fire-and-forget: telling Sonarr /
    # Radarr after a remote worker's completion...) get a moment to finish on
    # this loop. pytest-asyncio cancels them when it closes the loop, and one
    # cancelled while opening a database connection leaves the connection's
    # thread waiting: the test run then never exited (CI ran into its time
    # limit). Those still running are cancelled here, once connected.
    leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    if leftover:
        _, leftover = await asyncio.wait(leftover, timeout=1)
        for task in leftover:
            task.cancel()
        if leftover:
            await asyncio.wait(leftover, timeout=5)


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


@pytest.fixture(autouse=True)
def no_whisper_worker_left():
    """A conversion that detected a track's language started the persistent
    whisper worker, tied to the test's event loop: closed after the test, so
    the worker was never stopped and the test run never exited once it
    passed (CI ran into its 20-minute limit). `test_db` stops it on the
    test's loop; one left anyway is killed, and the next test gets a new
    one."""
    yield
    import signal
    import sys
    detection = sys.modules.get("backend.language_detection")
    proc = getattr(getattr(detection, "_whisper_worker", None), "_proc", None)
    if proc is None:
        return
    if proc.returncode is None:
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    detection._whisper_worker = detection._WhisperWorker()


def pytest_runtest_logreport(report):
    """On GitHub Actions, a failing test is also an annotation ("::error"):
    the API lists annotations to anyone, the job's log only to a signed-in
    admin."""
    if report.failed and os.environ.get("GITHUB_ACTIONS") == "true":
        text = str(report.longrepr)[-1500:]
        text = text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
        print(f"\n::error title={report.nodeid}::{text}", flush=True)


_exit_status = 0


def pytest_sessionfinish(session, exitstatus):
    """On GitHub Actions, how the run ended, as an annotation: when the job
    then runs into its time limit, the hang was after the tests."""
    global _exit_status
    _exit_status = int(exitstatus)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"\n::notice title=pytest::finished, exit status {int(exitstatus)}, "
              f"{session.testsfailed} failed of {session.testscollected}", flush=True)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    """On GitHub Actions: a thread a test left running (a database connection
    never closed, a worker) kept the interpreter from exiting after the run
    passed, and the job ran into its 20-minute limit — now and then, with
    nothing to show. Name each one, with where it waits, then exit anyway."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    import sys
    import threading
    import traceback
    frames = sys._current_frames()
    for thread in threading.enumerate():
        if thread is threading.main_thread() or thread.daemon or not thread.is_alive():
            continue
        frame = frames.get(thread.ident)
        stack = "".join(traceback.format_stack(frame)[-6:]) if frame else ""
        if "concurrent/futures/thread.py" in stack and "work_queue.get" in stack:
            continue  # an idle executor thread: shut down at exit
        text = f"{type(thread).__module__}.{type(thread).__name__} {thread.name}\n{stack}"[-1500:]
        text = text.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
        print(f"\n::warning title=Thread left running::{text}", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_exit_status)
