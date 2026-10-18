"""Keep anyway (v0.10.0): a conversion that came out larger than the
original was deleted, and the original kept. With originals kept for N days
the larger output is held as long (beside it, like a review), and the
Queue's Keep anyway puts it in place — the original is backed up as usual.
Without kept originals it's deleted, as before."""
import json
import shutil
import subprocess

import aiosqlite
import pytest
import pytest_asyncio

from backend.queue import JobQueue, QueueWorker


def _has_libx265() -> bool:
    return bool(shutil.which("ffmpeg")) and "libx265" in subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


pytestmark = pytest.mark.skipif(not _has_libx265(), reason="needs ffmpeg with libx265")


@pytest_asyncio.fixture
async def film(test_db, tmp_path, monkeypatch):
    import sys
    for name, module in list(sys.modules.items()):  # module DB_PATH copies (file events...)
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    src = tmp_path / "Film (2020)" / "Film (2020) 480p DVDRip.mkv"
    src.parent.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=2",
                    "-c:v", "mpeg4", "-b:v", "100k", str(src)], check=True)
    async with aiosqlite.connect(test_db) as db:
        for k, v in (("vmaf_analysis_enabled", "false"), ("trash_original_after_conversion", "false")):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.execute("INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, "
                         "audio_tracks_json, scan_timestamp) VALUES (?, ?, 'mpeg4', 1, '[]', '2026-10-19')",
                         (str(src), src.stat().st_size))
        await db.commit()
    return src


async def _larger(test_db, src, keep_days):
    """Convert losslessly: far larger than the 100 kb/s original."""
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('backup_original_days', ?)", (str(keep_days),))
        await db.commit()
    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(src), "convert", encoder="libx265")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE jobs SET libx265_preset = 'ultrafast', libx265_crf = 0 WHERE id = ?", (job_id,))
        await db.commit()
    worker = QueueWorker(test_db)
    await worker._process_job(await _job(test_db, job_id))
    return queue, worker, job_id


async def _job(db_path, job_id):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            return dict(await cur.fetchone())


@pytest.mark.asyncio
async def test_kept_anyway(test_db, film):
    original = film.read_bytes()
    queue, worker, job_id = await _larger(test_db, film, keep_days=7)
    job = await _job(test_db, job_id)
    assert (job["status"], job["error_key"]) == ("completed", "errors.encodeLarger")
    params = json.loads(job["error_params"])
    assert params["held"] is True and params["pct"] > 0
    held = json.loads(job["review_json"])["state"]["review_path"]
    assert "/.shrinkerr-review/" in held and film.read_bytes() == original  # held; the original untouched
    from backend import frames
    assert len(frames.load(job_id)) == 3  # to compare before keeping it

    assert await queue.keep_larger(job_id)
    worker.place_approved()
    await worker._placing
    job = await _job(test_db, job_id)
    assert job["status"] == "completed" and job["space_saved"] < 0 and job["error_key"] is None
    final = job["file_path"]
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=codec_name",
                          "-of", "csv=p=0", final], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "hevc"
    assert job["backup_path"] and open(job["backup_path"], "rb").read() == original  # the original, kept
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT 1 FROM ignored_files WHERE file_path = ?", (str(film),)) as cur:
            assert await cur.fetchone()  # still not converted again
    assert not await queue.keep_larger(job_id)  # once


@pytest.mark.asyncio
async def test_without_kept_originals_it_is_deleted(test_db, film):
    queue, _, job_id = await _larger(test_db, film, keep_days=0)
    job = await _job(test_db, job_id)
    assert job["error_key"] == "errors.encodeLarger" and json.loads(job["error_params"])["held"] is False
    assert job["review_json"] is None and not (film.parent / ".shrinkerr-review").exists()
    assert not await queue.keep_larger(job_id)


@pytest.mark.asyncio
async def test_it_goes_when_the_originals_would(test_db, film):
    from backend.queue import expire_held_larger
    queue, _, job_id = await _larger(test_db, film, keep_days=7)
    held = json.loads((await _job(test_db, job_id))["review_json"])["state"]["review_path"]
    assert await expire_held_larger() == 0  # not yet
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE jobs SET completed_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (job_id,))
        await db.commit()
    assert await expire_held_larger() == 1
    job = await _job(test_db, job_id)
    assert job["review_json"] is None and json.loads(job["error_params"])["held"] is False
    assert not __import__("os").path.exists(held)
    assert not await queue.keep_larger(job_id)
