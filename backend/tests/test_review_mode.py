"""Review before replace (v0.10.0): a converted file waits in
.shrinkerr-review/ beside its destination — the original untouched — until
it's approved (the queue puts it in place: finalize_review) or rejected
(deleted). The original must not change meanwhile."""
import json
import shutil
import subprocess
from types import SimpleNamespace

import aiosqlite
import pytest
import pytest_asyncio

from backend.queue import JobQueue, QueueWorker


def _has_libx265() -> bool:
    return bool(shutil.which("ffmpeg")) and "libx265" in subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


needs_x265 = pytest.mark.skipif(not _has_libx265(), reason="needs ffmpeg with libx265")


@pytest_asyncio.fixture
async def library(test_db, tmp_path, monkeypatch):
    import sys
    for name, module in list(sys.modules.items()):  # module DB_PATH copies (file events...)
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    src = tmp_path / "Film (2020)" / "Film (2020) 1080p x264.mkv"
    src.parent.mkdir()
    if shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=2",
                        "-c:v", "mpeg4", "-q:v", "1", str(src)], check=True)
    else:
        src.write_bytes(b"x" * 1000)
    async with aiosqlite.connect(test_db) as db:
        for k, v in (("review_before_replace", "true"), ("backup_original_days", "0"),
                     ("trash_original_after_conversion", "false"), ("vmaf_analysis_enabled", "false")):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.execute("INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, "
                         "audio_tracks_json, scan_timestamp) VALUES (?, ?, 'mpeg4', 1, '[]', '2026-10-10')",
                         (str(src), src.stat().st_size))
        await db.commit()
    return src


async def _job(db_path, job_id):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            return dict(await cur.fetchone())


async def _review_then(test_db, src):
    """Convert in review mode; returns (queue, worker, job id)."""
    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(src), "convert", encoder="libx265")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE jobs SET libx265_preset = 'ultrafast' WHERE id = ?", (job_id,))
        await db.commit()
    worker = QueueWorker(test_db)
    await worker._process_job(await _job(test_db, job_id))
    return queue, worker, job_id


@needs_x265
@pytest.mark.asyncio
async def test_held_then_approved(test_db, library):
    src = library
    original = src.read_bytes()
    queue, worker, job_id = await _review_then(test_db, src)

    job = await _job(test_db, job_id)
    assert job["status"] == "review" and job["space_saved"] > 0
    state = json.loads(job["review_json"])["state"]
    held = state["review_path"]
    assert "/.shrinkerr-review/" in held and src.read_bytes() == original  # the original untouched
    assert (await queue.get_stats())["review"] == 1
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT video_codec FROM scan_results") as cur:
            assert (await cur.fetchone())[0] == "mpeg4"  # the Scanner still describes the original
    assert await queue.add_jobs_bulk([{"file_path": str(src), "job_type": "convert"}]) == [0]  # not queued twice

    assert await queue.approve_reviews([job_id]) == 1
    nxt = await queue.get_next_job(affinity="nvenc_only")  # placement needs no encoder
    assert nxt["id"] == job_id
    await worker._process_job(nxt)

    job = await _job(test_db, job_id)
    assert job["status"] == "completed" and job["review_json"] is None
    final = state["final_path"]
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=codec_name",
                          "-of", "csv=p=0", final], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "hevc"
    assert not src.exists() or str(src) == final  # the original is gone (delete mode)
    assert not (src.parent / ".shrinkerr-review").exists()
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT video_codec FROM scan_results WHERE file_path = ?", (final,)) as cur:
            assert (await cur.fetchone())[0] == "hevc"
        async with db.execute("SELECT summary FROM file_events WHERE event_type = 'completed' ORDER BY id") as cur:
            summaries = [r[0] for r in await cur.fetchall()]
    assert summaries[0] == "Converted; waiting for review" and summaries[-1].startswith("Converted: saved")


@needs_x265
@pytest.mark.asyncio
async def test_the_original_changed_meanwhile(test_db, library):
    src = library
    queue, worker, job_id = await _review_then(test_db, src)
    held = json.loads((await _job(test_db, job_id))["review_json"])["state"]["review_path"]
    src.write_bytes(b"a new release from Radarr")  # an upgrade replaced it
    await queue.approve_reviews([job_id])
    await worker._process_job(await queue.get_next_job())
    job = await _job(test_db, job_id)
    assert (job["status"], job["error_key"]) == ("failed", "errors.reviewOriginalChanged")
    assert job["review_json"] is None  # a retry converts it again
    assert src.read_bytes() == b"a new release from Radarr"
    assert not (src.parent / ".shrinkerr-review").exists() and not __import__("os").path.exists(held)


@needs_x265
@pytest.mark.asyncio
async def test_rejected(test_db, library):
    src = library
    original = src.read_bytes()
    queue, worker, job_id = await _review_then(test_db, src)
    assert await queue.reject_reviews([job_id]) == 1
    job = await _job(test_db, job_id)
    assert (job["status"], job["error_key"], job["review_json"]) == ("cancelled", "errors.reviewRejected", None)
    assert src.read_bytes() == original and not (src.parent / ".shrinkerr-review").exists()
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT reason FROM ignored_files WHERE file_path = ?", (str(src),)) as cur:
            assert (await cur.fetchone())[0] == "review_rejected"


@pytest.mark.asyncio
async def test_queue_rules_for_reviews(test_db, tmp_path):
    held = tmp_path / ".shrinkerr-review" / "a.mkv"
    held.parent.mkdir()
    held.write_bytes(b"x")
    review = json.dumps({"state": {"review_path": str(held)}, "result": {}})
    queue = JobQueue(test_db)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO jobs (file_path, job_type, status, created_at, review_json, review_approved) "
                         "VALUES ('/m/a.mkv', 'convert', 'pending', 'x', ?, 1)", (review,))
        await db.execute("INSERT INTO jobs (file_path, job_type, status, created_at) VALUES ('/m/b.mkv', 'convert', 'pending', 'x')")
        await db.commit()
    await queue.clear_pending()  # an approved review stays: its output would be stranded
    assert [j["file_path"] for j in await queue.get_jobs_by_status("pending")] == ["/m/a.mkv"]
    [job] = await queue.get_jobs_by_status("pending")
    await queue.remove_job(job["id"])  # removing it rejects it: the output goes
    assert not held.exists() and (await _job(test_db, job["id"]))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_remote_workers(test_db, monkeypatch):
    """A worker's held conversion is recorded with this server's paths, and an
    approved one is never handed to a worker."""
    import backend.nodes as nodes
    import backend.routes.nodes as nodes_route
    monkeypatch.setattr(nodes, "DB_PATH", test_db)

    async def no_token(*a, **kw):
        return None
    monkeypatch.setattr(nodes_route, "_require_node_token", no_token)
    nm = nodes.NodeManager()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(node_manager=nm)))
    queue = JobQueue(test_db)
    job_id = await queue.add_job("/media/Film/film.mkv", "convert", encoder="libx265")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO worker_nodes (id, name, capabilities, status, registered_at, path_mappings) "
                         "VALUES ('n1', 'n1', '[\"libx265\"]', 'online', 'x', ?)",
                         (json.dumps([{"server": "/media", "worker": "/mnt/media"}]),))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('review_before_replace', 'true')")
        await db.commit()
    assigned = (await nodes_route.request_job(nodes_route.RequestJobBody(node_id="n1"), request))["job"]
    assert assigned["id"] == job_id and assigned["review_before_replace"] is True
    await nodes_route.report_complete(nodes_route.CompletionReport(
        node_id="n1", job_id=job_id, success=True, space_saved=500, vmaf_score=95.0,
        review={"input_path": "/mnt/media/Film/film.mkv", "review_path": "/mnt/media/Film/.shrinkerr-review/film.mkv",
                "final_path": "/mnt/media/Film/film.mkv", "source_size": 1, "source_mtime": 1.0,
                "external_sub_files": [{"path": "/mnt/media/Film/film.is.srt"}]},
        review_result={"space_saved": 500}), request)
    job = await _job(test_db, job_id)
    assert (job["status"], job["space_saved"]) == ("review", 500)
    state = json.loads(job["review_json"])["state"]
    assert (state["review_path"], state["external_sub_files"][0]["path"]) == (
        "/media/Film/.shrinkerr-review/film.mkv", "/media/Film/film.is.srt")
    await queue.approve_reviews([job_id])
    assert (await nodes_route.request_job(nodes_route.RequestJobBody(node_id="n1"), request))["job"] is None
