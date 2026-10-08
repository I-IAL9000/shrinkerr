"""v0.10.0 (H5): a database lock after the output replaced the original
requeued the job, and the re-run re-encoded the already-converted file.

Several steps run after the original is gone (space saved, scan row updates,
the inline health check, stats). A "database is locked" in any of them went
through the transient-lock requeue, and so did a job the orphan reaper found.
"""
import sqlite3

import aiosqlite
import pytest

from backend.queue import JobQueue, QueueWorker
from backend.tests.test_same_name_conversion import _mkv


async def _status(db_path, job_id):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)) as cur:
            return (await cur.fetchone())[0]


@pytest.mark.asyncio
async def test_lock_after_the_original_was_replaced_completes_the_job(test_db, tmp_path, monkeypatch):
    src = tmp_path / "Movie (2009).mkv"
    _mkv(src, ["eng"])
    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(src), "convert", encoder="libx265")

    import backend.converter as converter

    async def converted(**kwargs):
        return {"success": True, "output_path": str(src), "space_saved": 1000, "error": None}

    monkeypatch.setattr(converter, "convert_file", converted)
    worker = QueueWorker(test_db)

    async def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(worker.queue, "update_space_saved", locked)
    job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)
    await worker._worker_task(job)
    assert await _status(test_db, job_id) == "completed"


@pytest.mark.asyncio
async def test_lock_before_anything_was_replaced_still_requeues(test_db, tmp_path, monkeypatch):
    src = tmp_path / "Movie (2009).mkv"
    _mkv(src, ["eng"])
    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(src), "convert", encoder="libx265")

    import backend.converter as converter

    async def locked(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(converter, "convert_file", locked)
    worker = QueueWorker(test_db)
    job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)
    await worker._worker_task(job)
    assert await _status(test_db, job_id) == "pending"


@pytest.mark.asyncio
async def test_reaper_completes_a_finalized_job_instead_of_requeueing(test_db):
    queue = JobQueue(test_db)
    job_id = await queue.add_job("/media/Movie (2009).mkv", "convert", encoder="libx265")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE jobs SET status = 'running', started_at = '2026-01-01T00:00:00+00:00' "
                         "WHERE id = ?", (job_id,))
        await db.commit()
    worker = QueueWorker(test_db)
    worker._finalized_jobs.add(job_id)
    await worker._reap_orphaned_running()
    assert await _status(test_db, job_id) == "completed"
