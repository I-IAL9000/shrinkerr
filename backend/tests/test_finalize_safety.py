"""v0.10.0: once a job's output is in place, nothing may make it run again,
and no backup may replace another.

- A backup moved onto an existing backup of the same name replaced it.
- Re-dating the backup (os.utime) raised on shares that refuse it, failing a
  job whose original was already in the backup folder.
- Any failure disposing of the original after the output was placed failed
  the job, and Retry encoded the output — by then the file in the library —
  again; an audio cleanup re-ran by stream indexes that no longer match.
- The "output replaced the original" marker lived only in memory: after a
  restart the job went back to pending. Retry accepted any status.
"""
import os
import shutil
import subprocess
from pathlib import Path

import aiosqlite
import pytest

import backend.converter as converter
from backend.api_errors import ApiError
from backend.queue import JobQueue, QueueWorker


def _has_libx265() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "libx265" in out


async def _set(db_path, sql, *args):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(sql, args)
        await db.commit()


async def _job(db_path, job_id):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            return await cur.fetchone()


# --- backups ---------------------------------------------------------------

def test_backup_never_replaces_an_earlier_backup(tmp_path):
    (tmp_path / "backup").mkdir()
    earlier = tmp_path / "backup" / "Episode 1.mkv"
    earlier.write_bytes(b"earlier")
    src = tmp_path / "Episode 1.mkv"
    src.write_bytes(b"now")
    got = converter._move_into_backup(str(src), str(earlier))
    assert got == str(tmp_path / "backup" / "Episode 1.1.mkv")
    assert earlier.read_bytes() == b"earlier"
    assert Path(got).read_bytes() == b"now"


def test_backup_survives_a_share_that_refuses_redating(tmp_path, monkeypatch):
    src = tmp_path / "Movie.mkv"
    src.write_bytes(b"x")

    def refuse(*args, **kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(converter.os, "utime", refuse)
    got = converter._move_into_backup(str(src), str(tmp_path / "Movie.bak.mkv"))
    assert Path(got).read_bytes() == b"x" and not src.exists()


# --- disposal failures after the output is in place ------------------------

@pytest.mark.asyncio
@pytest.mark.skipif(not _has_libx265(), reason="needs ffmpeg with libx265")
@pytest.mark.parametrize("name", ["Show - S01E01 - 1080p WEB h264.mkv", "Movie (2009).mkv"])
async def test_conversion_succeeds_when_the_original_cant_be_backed_up(test_db, tmp_path, monkeypatch, name):
    src = tmp_path / name
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=2",
                    "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)
    original = src.read_bytes()
    await _set(test_db, "INSERT OR REPLACE INTO settings (key, value) VALUES ('backup_original_days', '7')")

    def share_says_no(src_path, dst):
        raise PermissionError(13, "Permission denied", dst)

    monkeypatch.setattr(converter, "_move_into_backup", share_says_no)
    result = await converter.convert_file(str(src), "libx265", 2.0, override_libx265_preset="ultrafast")

    assert result["success"], result.get("error")
    assert Path(result["output_path"]).exists()
    kept = src if result["output_path"] != str(src) else tmp_path / ".shrinkerr-replacing" / name
    assert kept.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
async def test_remux_succeeds_when_the_original_cant_be_removed(test_db, tmp_path, monkeypatch):
    from backend.audio import remux_audio
    from backend.tests.test_remux_guards import _clip
    src = _clip(tmp_path)
    original = src.read_bytes()
    real_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if ".shrinkerr-replacing" in str(self):
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    result = await remux_audio(str(src), [1], duration=3.0)

    assert result["success"] is True
    assert (tmp_path / ".shrinkerr-replacing" / src.name).read_bytes() == original
    assert src.read_bytes() != original


# --- the finalized marker ---------------------------------------------------

async def _running(db_path, queue, finalized: bool) -> int:
    job_id = await queue.add_job(f"/media/Movie {finalized}.mkv", "convert", encoder="libx265")
    await _set(db_path, "UPDATE jobs SET status = 'running', started_at = '2026-01-01T00:00:00+00:00', "
                        "finalized_at = ? WHERE id = ?", "2026-01-01T00:01:00+00:00" if finalized else None, job_id)
    return job_id


@pytest.mark.asyncio
async def test_finalize_is_recorded(test_db):
    queue = JobQueue(test_db)
    job_id = await queue.add_job("/media/Movie.mkv", "convert", encoder="libx265")
    await QueueWorker(test_db)._finalize(job_id)
    assert (await _job(test_db, job_id))["finalized_at"]


@pytest.mark.asyncio
async def test_restart_completes_finalized_jobs_and_requeues_the_rest(test_db):
    queue = JobQueue(test_db)
    done = await _running(test_db, queue, finalized=True)
    interrupted = await _running(test_db, queue, finalized=False)
    await queue.reset_stale_running()
    assert (await _job(test_db, done))["status"] == "completed"
    assert (await _job(test_db, interrupted))["status"] == "pending"


@pytest.mark.asyncio
async def test_reaper_completes_a_job_finalized_before_a_restart(test_db):
    queue = JobQueue(test_db)
    job_id = await _running(test_db, queue, finalized=True)
    await QueueWorker(test_db)._reap_orphaned_running()  # a fresh worker: nothing in memory
    assert (await _job(test_db, job_id))["status"] == "completed"


@pytest.mark.asyncio
async def test_a_new_run_starts_unfinalized(test_db):
    queue = JobQueue(test_db)
    job_id = await queue.add_job("/media/Movie.mkv", "convert", encoder="libx265")
    await _set(test_db, "UPDATE jobs SET finalized_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", job_id)
    assert await queue.claim_job(job_id)
    assert (await _job(test_db, job_id))["finalized_at"] is None


# --- Retry ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_only_reruns_jobs_that_never_replaced_their_original(test_db, tmp_path):
    from backend.routes.jobs import init_job_routes, retry_job
    queue = JobQueue(test_db)
    init_job_routes(QueueWorker(test_db), queue)
    src = tmp_path / "Movie.mkv"
    src.write_bytes(b"x")

    async def job(status, finalized=None):
        job_id = await queue.add_job(str(src), "convert", encoder="libx265")
        await _set(test_db, "UPDATE jobs SET status = ?, finalized_at = ? WHERE id = ?", status, finalized, job_id)
        return job_id

    for status in ("completed", "running", "pending"):
        with pytest.raises(ApiError) as exc:
            await retry_job(await job(status))
        assert exc.value.code == "jobs.cannotRetryStatus"
    with pytest.raises(ApiError) as exc:
        await retry_job(await job("failed", "2026-01-01T00:00:00+00:00"))
    assert exc.value.code == "jobs.retryAlreadyReplaced"
    failed = await job("failed")
    assert (await retry_job(failed))["status"] == "pending"
    assert (await _job(test_db, failed))["status"] == "pending"
