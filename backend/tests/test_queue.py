import pytest
import pytest_asyncio
from backend.queue import JobQueue


@pytest.mark.asyncio
async def test_add_job(test_db):
    q = JobQueue(test_db)
    job_id = await q.add_job("/media/movie.mkv", "convert", encoder="nvenc")
    assert isinstance(job_id, int)
    assert job_id > 0


@pytest.mark.asyncio
async def test_get_pending_jobs(test_db):
    q = JobQueue(test_db)
    await q.add_job("/media/a.mkv", "convert", encoder="nvenc")
    await q.add_job("/media/b.mkv", "audio")
    jobs = await q.get_jobs_by_status("pending")
    assert len(jobs) == 2
    assert all(j["status"] == "pending" for j in jobs)


@pytest.mark.asyncio
async def test_next_job_returns_oldest_pending(test_db):
    q = JobQueue(test_db)
    id1 = await q.add_job("/media/first.mkv", "convert", encoder="nvenc")
    await q.add_job("/media/second.mkv", "convert", encoder="nvenc")
    next_job = await q.get_next_job()
    assert next_job is not None
    assert next_job["id"] == id1
    assert next_job["file_path"] == "/media/first.mkv"


@pytest.mark.asyncio
async def test_update_job_status(test_db):
    q = JobQueue(test_db)
    job_id = await q.add_job("/media/movie.mkv", "convert", encoder="libx265")

    await q.update_status(job_id, "running")
    jobs = await q.get_jobs_by_status("running")
    assert len(jobs) == 1
    assert jobs[0]["started_at"] is not None

    await q.update_status(job_id, "completed")
    jobs = await q.get_jobs_by_status("completed")
    assert len(jobs) == 1
    assert jobs[0]["completed_at"] is not None

    # Test progress update
    await q.update_progress(job_id, 55.5, fps=24.0, eta=120)
    all_jobs = await q.get_all_jobs()
    job = next(j for j in all_jobs if j["id"] == job_id)
    assert job["progress"] == 55.5
    assert job["fps"] == 24.0
    assert job["eta_seconds"] == 120


@pytest.mark.asyncio
async def test_cancel_job(test_db):
    q = JobQueue(test_db)
    job_id = await q.add_job("/media/movie.mkv", "convert")
    await q.update_status(job_id, "cancelled")
    jobs = await q.get_jobs_by_status("cancelled")
    assert len(jobs) == 1
    assert jobs[0]["id"] == job_id

    # clear_completed should remove cancelled jobs too
    await q.clear_completed()
    jobs = await q.get_jobs_by_status("cancelled")
    assert len(jobs) == 0


@pytest.mark.asyncio
async def test_remove_job(test_db):
    q = JobQueue(test_db)
    job_id = await q.add_job("/media/movie.mkv", "convert")
    all_before = await q.get_all_jobs()
    assert len(all_before) == 1

    await q.remove_job(job_id)
    all_after = await q.get_all_jobs()
    assert len(all_after) == 0


@pytest.mark.asyncio
async def test_queue_stats(test_db):
    q = JobQueue(test_db)
    id1 = await q.add_job("/media/a.mkv", "convert")
    id2 = await q.add_job("/media/b.mkv", "convert")
    id3 = await q.add_job("/media/c.mkv", "audio")

    await q.update_status(id1, "running")
    await q.update_status(id2, "completed")
    await q.update_space_saved(id2, 1024 * 1024 * 100)  # 100 MB
    await q.update_status(id3, "failed", error_log="something went wrong")

    stats = await q.get_stats()
    assert stats["total_jobs"] == 3
    assert stats["pending"] == 0
    assert stats["running"] == 1
    assert stats["completed"] == 1
    assert stats["failed"] == 1
    assert stats["total_space_saved"] == 1024 * 1024 * 100
