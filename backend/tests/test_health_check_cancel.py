"""Cancelling a health check stops it (v0.10.0). A thorough check decodes
the whole file — minutes for an episode — and Cancel did nothing: its
ffmpeg wasn't the job's to kill, so it ran to the end and recorded its
verdict. Killed, a check's verdict says nothing about the file: none is
recorded."""
import asyncio
import shutil
import subprocess

import aiosqlite
import pytest

STATS = {"completed": 0, "total_jobs": 1, "total_space_saved": 0}


async def _job(test_db, tmp_path):
    from backend.queue import JobQueue
    episode = tmp_path / "Dragons' Den - S21E01.mkv"
    episode.write_bytes(b"x")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, duration, scan_timestamp) "
                         "VALUES (?, 1, 3600, 't')", (str(episode),))
        await db.commit()
    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(episode), "health_check", encoder="thorough")
    return episode, job_id, next(j for j in await queue.get_all_jobs() if j["id"] == job_id)


def _decode_for_a_minute(monkeypatch):
    """A check whose ffmpeg runs a minute, as a thorough one does (here: sleep)."""
    import backend.health_check as health_check

    async def check(path, mode="quick", progress_cb=None, duration_seconds_hint=0, proc_callback=None):
        proc = await asyncio.create_subprocess_exec("sleep", "60")
        proc_callback(proc)
        await proc.wait()
        return {"status": "corrupt", "errors": [f"exit code {proc.returncode}"], "duration_seconds": 1.0}
    monkeypatch.setattr(health_check, "run_check", check)


async def _outcome(test_db, job_id):
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT status, health_status FROM jobs WHERE id = ?", (job_id,)) as cur:
            job = tuple(await cur.fetchone())
        async with db.execute("SELECT health_status FROM scan_results") as cur:
            return job, (await cur.fetchone())[0]


@pytest.mark.asyncio
async def test_cancel_stops_a_thorough_check(test_db, tmp_path, monkeypatch):
    from backend.queue import QueueWorker
    episode, job_id, job = await _job(test_db, tmp_path)
    _decode_for_a_minute(monkeypatch)
    worker = QueueWorker(test_db)
    task = asyncio.create_task(worker._run_health_check_job(job_id, str(episode), episode.name, job, STATS))
    for _ in range(100):
        if job_id in worker._active_procs:
            break
        await asyncio.sleep(0.05)

    assert await worker.cancel_current(job_id) == job_id
    await asyncio.wait_for(task, timeout=10)  # not the minute

    assert await _outcome(test_db, job_id) == (("cancelled", None), None)


@pytest.mark.asyncio
async def test_cancelled_before_its_ffmpeg_started(test_db, tmp_path, monkeypatch):
    from backend.queue import QueueWorker
    episode, job_id, job = await _job(test_db, tmp_path)
    _decode_for_a_minute(monkeypatch)
    worker = QueueWorker(test_db)
    worker._cancel_flags.add(job_id)  # Cancel came while it read the duration

    await asyncio.wait_for(worker._run_health_check_job(job_id, str(episode), episode.name, job, STATS), timeout=10)

    assert await _outcome(test_db, job_id) == (("cancelled", None), None)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["quick", "thorough"])
async def test_the_check_hands_over_its_ffmpeg(tmp_path, mode):
    from backend.health_check import run_check
    clip = tmp_path / "clip.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=160x120:d=1",
                    "-c:v", "libx264", "-preset", "ultrafast", str(clip)], check=True)
    handed = []

    result = await run_check(str(clip), mode=mode, proc_callback=handed.append)

    assert result["status"] == "healthy" and len(handed) == 1 and handed[0].returncode == 0
