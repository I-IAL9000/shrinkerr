"""The Corrupt filter's "Re-check all" (v0.10.0): every file in the view
gets the kind of check that flagged it again — flags from older checks can
be wrong (harmless ffmpeg warnings counted as damage). It replaced "Clear
all corrupt flags", which erased real findings too."""
import aiosqlite
import pytest

# file: (health_status, health_check_type, probe_status)
FILES = {
    "/m/Thorough (2001)/t.mkv": ("corrupt", "thorough", "ok"),
    "/m/Quick (2002)/q.mkv": ("corrupt", "quick", "ok"),
    "/m/Unreadable (2003)/u.mkv": (None, None, "corrupt"),
    "/m/Fine (2004)/f.mkv": ("healthy", "quick", "ok"),
}


@pytest.mark.asyncio
async def test_each_corrupt_file_gets_the_check_that_flagged_it(test_db, monkeypatch):
    import sys
    from backend.queue import JobQueue, QueueWorker
    from backend.routes.jobs import HealthCheckRequest, init_job_routes, queue_health_checks
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    worker = QueueWorker(test_db)
    monkeypatch.setattr(worker, "start_if_idle", lambda *a, **kw: None)
    init_job_routes(worker, JobQueue(test_db))
    async with aiosqlite.connect(test_db) as db:
        for path, (health, kind, probe) in FILES.items():
            await db.execute("INSERT INTO scan_results (file_path, file_size, health_status, health_check_type, "
                             "probe_status, scan_timestamp) VALUES (?, 1, ?, ?, ?, 't')", (path, health, kind, probe))
        await db.commit()

    result = await queue_health_checks(HealthCheckRequest(mode="same", select_all=True, filter="corrupt"))

    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT file_path, encoder FROM jobs WHERE job_type = 'health_check'") as cur:
            jobs = dict(await cur.fetchall())
    # (an unreadable file has no check of its own: the quick one reads it again)
    assert jobs == {"/m/Thorough (2001)/t.mkv": "thorough", "/m/Quick (2002)/q.mkv": "quick",
                    "/m/Unreadable (2003)/u.mkv": "quick"}
    assert result["added"] == 3
