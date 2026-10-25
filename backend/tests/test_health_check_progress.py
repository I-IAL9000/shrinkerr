"""A thorough health check decodes the whole file — minutes for a film —
and the queue showed it as a bare spinner the whole time: the check could
report its progress, but the queue never asked (v0.10.0)."""
import aiosqlite
import pytest

STATS = {"completed": 0, "total_jobs": 1, "total_space_saved": 0}


@pytest.mark.asyncio
async def test_a_thorough_check_reports_its_progress(test_db, tmp_path, monkeypatch):
    import backend.health_check as health_check
    from backend.queue import JobQueue, QueueWorker
    from backend.websocket import ws_manager
    film = tmp_path / "Film (1998).mkv"
    film.write_bytes(b"x")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, duration, scan_timestamp) "
                         "VALUES (?, 1, 5400, 't')", (str(film),))
        await db.commit()

    asked = {}

    async def check(path, mode="quick", progress_cb=None, duration_seconds_hint=0, proc_callback=None):
        asked.update(mode=mode, hint=duration_seconds_hint)
        await progress_cb(50.0)
        return {"status": "healthy", "errors": [], "duration_seconds": 1.0}

    sent = []

    async def progress(**kw):
        sent.append(kw)

    monkeypatch.setattr(health_check, "run_check", check)
    monkeypatch.setattr(ws_manager, "send_job_progress", progress)
    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(film), "health_check", encoder="thorough")
    job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)

    await QueueWorker(test_db)._run_health_check_job(job_id, str(film), film.name, job, STATS)

    assert asked == {"mode": "thorough", "hint": 5400}
    assert [k["progress"] for k in sent] == [0.0, 50.0, 100.0]  # started, halfway, done
    assert sent[1]["eta"] is not None and sent[1]["step_key"] == "steps.healthCheckMode"
