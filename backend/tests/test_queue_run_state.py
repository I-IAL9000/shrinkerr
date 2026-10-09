"""The queue's Start / Pause is remembered across restarts (auto-queued files
waited for a click after an automatic image update), and webhooks, add-by-path
and health checks start an idle queue without undoing a manual pause."""
import asyncio

import aiosqlite
import pytest
import pytest_asyncio

from backend.queue import QueueWorker


@pytest_asyncio.fixture
async def make_worker(test_db, monkeypatch):
    workers = []

    async def idle_loop(self):
        await asyncio.Event().wait()  # stands in for the real loop

    monkeypatch.setattr(QueueWorker, "_run_loop", idle_loop)

    def make():
        w = QueueWorker(test_db)
        workers.append(w)
        return w

    yield make
    for w in workers:
        task, save = w._task, w._last_state_save
        w.stop()
        for t in (task, save):
            if t is not None:
                try:
                    await t
                except BaseException:
                    pass


async def _state(worker):
    if worker._last_state_save:
        await worker._last_state_save
    async with aiosqlite.connect(worker.db_path) as db:
        async with db.execute("SELECT value FROM settings WHERE key = 'queue_run_state'") as cur:
            row = await cur.fetchone()
    return row[0] if row else None


@pytest.mark.asyncio
async def test_start_pause_resume_are_remembered(make_worker):
    w = make_worker()
    w.start()
    assert await _state(w) == "running"
    w.pause()
    assert await _state(w) == "paused"
    w.resume()
    assert await _state(w) == "running"
    w.start()
    w.pause()  # in quick succession: the last one wins
    assert await _state(w) == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("saved, running, paused", [
    ("running", True, False),
    ("paused", True, True),
    (None, False, False),  # never started (or an older install): stays stopped
])
async def test_a_restart_restores_the_queue(make_worker, saved, running, paused):
    if saved:
        w = make_worker()
        w._remember_run_state(saved)
        await _state(w)
    after_restart = make_worker()
    await after_restart.restore_run_state()
    assert (after_restart._running, after_restart._paused) == (running, paused)
    assert await _state(after_restart) == saved


@pytest.mark.asyncio
async def test_new_work_never_undoes_a_manual_pause(make_worker):
    w = make_worker()
    w.start()
    w.pause()
    assert w.start_if_idle() is False
    assert w._paused and await _state(w) == "paused"
    idle = make_worker()
    assert idle.start_if_idle() is True
    assert idle._running and not idle._paused


@pytest.mark.asyncio
async def test_a_webhook_does_not_resume_a_paused_queue(make_worker, test_db, tmp_path, monkeypatch):
    import sys
    import backend.scanner as scanner
    from backend.queue import JobQueue
    from backend.routes.jobs import init_job_routes
    from backend.routes.webhooks import WebhookQueueRequest, webhook_queue
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    w = make_worker()
    w.start()
    w.pause()
    init_job_routes(w, JobQueue(test_db))
    media = tmp_path / "media"
    media.mkdir()
    film = media / "Film (2020).mkv"
    film.write_bytes(b"x")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO media_dirs (path) VALUES (?)", (str(media),))
        await db.commit()

    async def fake_probe(path, *a, **kw):
        return {"video_codec": "h264", "video_width": 1920, "video_height": 1080,
                "audio_tracks": [], "subtitle_tracks": [], "duration": 60, "file_size": 1}
    monkeypatch.setattr(scanner, "probe_file", fake_probe)
    assert (await webhook_queue(WebhookQueueRequest(paths=[str(film)])))["added"] == 1
    assert w._paused and await _state(w) == "paused"
