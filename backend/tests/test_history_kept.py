""""Clear done" keeps your history (v0.10.0).

It deleted every finished job: the Dashboard's totals (computed from them)
went back to zero, Undo was gone and the Scanner forgot which files it had
converted. Completed jobs are now only hidden from the Queue."""
import aiosqlite
import pytest
import pytest_asyncio

from backend.queue import JobQueue


@pytest_asyncio.fixture
async def history(test_db):
    async with aiosqlite.connect(test_db) as db:
        for path, status, saved in (("/m/a.mkv", "completed", 4_000), ("/m/b.mkv", "completed", 6_000),
                                    ("/m/c.mkv", "failed", 0), ("/m/d.mkv", "cancelled", 0), ("/m/e.mkv", "pending", 0)):
            await db.execute(
                "INSERT INTO jobs (file_path, job_type, status, created_at, completed_at, space_saved, original_size, "
                "backup_path) VALUES (?, 'convert', ?, '2026-10-10', '2026-10-10T12:00:00', ?, 10000, ?)",
                (path, status, saved, "/backups/a.mkv" if path == "/m/a.mkv" else None))
        await db.commit()
    return test_db


@pytest.mark.asyncio
async def test_clear_done_hides_completed_jobs_and_deletes_failed_ones(history):
    q = JobQueue(history)
    await q.clear_completed()
    assert await q.get_jobs_by_status("completed") == []
    assert await q.get_job_ids_by_status("completed") == []
    assert await q.get_jobs_by_status("failed") == [] and await q.get_jobs_by_status("cancelled") == []
    assert [j["file_path"] for j in await q.get_all_jobs()] == ["/m/e.mkv"]
    stats = await q.get_stats()  # the Queue's tab counts
    assert (stats["completed"], stats["total_space_saved"], stats["pending"]) == (0, 0, 1)
    async with aiosqlite.connect(history) as db:
        async with db.execute("SELECT file_path FROM jobs ORDER BY file_path") as cur:
            assert [r[0] for r in await cur.fetchall()] == ["/m/a.mkv", "/m/b.mkv", "/m/e.mkv"]


@pytest.mark.asyncio
async def test_the_dashboard_and_undo_still_see_them(history, monkeypatch):
    import backend.routes.stats as stats_route
    import backend.routes.scan as scan_route
    from backend.scan_filters import FILTERS
    await JobQueue(history).clear_completed()
    summary = stats_route._stats_summary(history)
    assert (summary["total_saved"], summary["files_processed"]) == (10_000, 2)
    monkeypatch.setattr(scan_route, "DB_PATH", history)
    async with aiosqlite.connect(history) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES ('/m/a.mkv', 1, '2026-10-10')")
        await db.commit()
    assert "undo_possible" in FILTERS
    assert await scan_route._paths_matching("undo_possible") == ["/m/a.mkv"]


@pytest.mark.asyncio
async def test_removing_one_finished_job(history):
    q = JobQueue(history)
    ids = {j["file_path"]: j["id"] for j in await q.get_all_jobs()}
    await q.remove_job(ids["/m/a.mkv"])  # completed: hidden
    await q.remove_job(ids["/m/e.mkv"])  # pending: gone
    assert sorted(j["file_path"] for j in await q.get_all_jobs()) == ["/m/b.mkv", "/m/c.mkv", "/m/d.mkv"]
    async with aiosqlite.connect(history) as db:
        async with db.execute("SELECT file_path, cleared FROM jobs WHERE file_path IN ('/m/a.mkv', '/m/e.mkv')") as cur:
            assert await cur.fetchall() == [("/m/a.mkv", 1)]
