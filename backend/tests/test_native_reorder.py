"""Original-language audio moved first, as queued work (v0.10.0).

The worker and the converter move the original language's audio first
("reorder_native_audio"), but the queue's check for it read a column the
queue never loaded, so it never fired: a conversion that only reordered was
"convert", and lost the reorder when its encode came out larger (the cleanup
then runs for "combined" only); "cleanup only" skipped reorder-only files;
and the plans didn't say. It now fires — only when a kept track is in the
original language and isn't first, which is when the worker reorders."""
import json

import aiosqlite
import pytest
import pytest_asyncio

NOW = "2026-10-10T00:00:00"


def a(si, lang, keep=True):
    return {"stream_index": si, "language": lang, "codec": "aac", "channels": 2, "keep": keep, "locked": False}


def row(audio, native="jpn"):
    return {"audio_tracks_json": json.dumps(audio), "subtitle_tracks_json": "[]", "native_language": native}


@pytest.fixture
def cleanup_settings(test_db, monkeypatch):
    import backend.config
    import backend.scanner as scanner
    monkeypatch.setattr(scanner, "settings", backend.config.settings)
    scanner.invalidate_sub_settings_cache()
    yield test_db
    scanner.invalidate_sub_settings_cache()


def test_reorder_is_work_only_when_the_worker_would_do_it(cleanup_settings):
    from backend.routes.jobs import track_work
    assert track_work(row([a(1, "eng"), a(2, "jpn")]))[2] is True
    assert track_work(row([a(1, "jpn"), a(2, "eng")]))[2] is False      # already first
    assert track_work(row([a(1, "eng"), a(2, "spa")]))[2] is False      # no Japanese track to move
    assert track_work(row([a(1, "eng"), a(2, "jpn")], native="und"))[2] is False
    assert track_work(row([a(1, "eng"), a(2, "jpn")], native=None))[2] is False


def test_reorder_follows_the_setting(cleanup_settings):
    import sqlite3
    from backend.routes.jobs import track_work
    with sqlite3.connect(cleanup_settings) as db:
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('reorder_native_audio', 'false')")
    assert track_work(row([a(1, "eng"), a(2, "jpn")]))[2] is False


@pytest_asyncio.fixture
async def jobs_env(cleanup_settings, monkeypatch):
    import sys
    from backend.queue import JobQueue, QueueWorker
    from backend.routes.jobs import init_job_routes
    test_db = cleanup_settings
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    worker = QueueWorker(test_db)
    monkeypatch.setattr(worker, "start", lambda *a, **kw: None)  # no encoding the fakes
    init_job_routes(worker, JobQueue(test_db))
    async with aiosqlite.connect(test_db) as db:
        for path, needs, audio in (
            ("/m/convert.mkv", 1, [a(1, "eng"), a(2, "jpn")]),
            ("/m/reorder.mkv", 0, [a(1, "eng"), a(2, "jpn")]),
            ("/m/nothing.mkv", 0, [a(1, "jpn"), a(2, "eng")]),
        ):
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, native_language, "
                "duration, audio_tracks_json, subtitle_tracks_json, scan_timestamp) "
                "VALUES (?, 1000000000, 'h264', ?, 'jpn', 3600, ?, '[]', ?)",
                (path, needs, json.dumps(audio), NOW))
        await db.commit()
    return test_db


async def _jobs(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path, job_type FROM jobs ORDER BY file_path") as cur:
            return dict(await cur.fetchall())


@pytest.mark.asyncio
async def test_queued_jobs_include_the_reorder(jobs_env):
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    await add_jobs_from_scan(BulkQueueFromScanRequest(
        file_paths=["/m/convert.mkv", "/m/reorder.mkv", "/m/nothing.mkv"], cleanup_only=False))
    assert (await _jobs(jobs_env))["/m/convert.mkv"] == "combined"


@pytest.mark.asyncio
async def test_cleanup_only_queues_a_reorder(jobs_env):
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    await add_jobs_from_scan(BulkQueueFromScanRequest(
        file_paths=["/m/reorder.mkv", "/m/nothing.mkv"], cleanup_only=True))
    assert await _jobs(jobs_env) == {"/m/reorder.mkv": "audio"}  # nothing.mkv: nothing to do


@pytest.mark.asyncio
async def test_the_estimate_and_the_plans_agree(jobs_env):
    from backend.routes.jobs import (BulkQueueFromScanRequest, EstimateRequest, add_jobs_from_scan,
                                     estimate_jobs, job_plan, refresh_pending_jobs)
    est = await estimate_jobs(EstimateRequest(file_paths=["/m/convert.mkv"]))
    assert est["by_type"]["combined"] == 1 and est["by_type"]["convert"] == 0
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=["/m/convert.mkv", "/m/nothing.mkv"]))
    assert await refresh_pending_jobs() == 0  # a refresh keeps "combined"
    async with aiosqlite.connect(jobs_env) as db:
        async with db.execute("SELECT file_path, id FROM jobs") as cur:
            ids = dict(await cur.fetchall())
    assert (await job_plan(ids["/m/convert.mkv"]))["native_first"] == "jpn"
    assert (await job_plan(ids["/m/nothing.mkv"]))["native_first"] is None
