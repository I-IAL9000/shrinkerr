"""Hardlink-aware (v0.10.0).

A file with another hardlink — a torrent client still seeding the download
Sonarr / Radarr linked into the library — keeps its space when Shrinkerr
writes a new file, so converting or cleaning it frees nothing and uses more.
Scans record the link count; the "Hardlinked" filter shows them; with
"skip_hardlinked" (new installs: on) the queue leaves them out."""
import json
import os
import shutil
import subprocess

import aiosqlite
import pytest
import pytest_asyncio

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _clip(path):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=24:duration=1",
                    "-c:v", "libx264", str(path)], check=True)


@needs_ffmpeg
@pytest.mark.asyncio
async def test_scans_record_the_link_count(test_db, tmp_path, monkeypatch):
    import backend.config
    import backend.scanner as scanner
    from backend.scanner import probe_file, scan_directory
    from backend.watcher import scanned_from_probe
    monkeypatch.setattr(scanner, "settings", backend.config.settings)
    media = tmp_path / "media" / "Film (2020)"
    media.mkdir(parents=True)
    film, other = media / "Film.mkv", media / "Other.mkv"
    _clip(film)
    _clip(other)
    seeding = tmp_path / "torrents"
    seeding.mkdir()
    os.link(film, seeding / "Film.mkv")  # the torrent client's copy
    results = {r.file_path: r.link_count for r in await scan_directory(str(tmp_path / "media"))}
    assert results == {str(film): 2, str(other): 1}
    watched = await scanned_from_probe(str(film), await probe_file(str(film)), ["h264"], 23)
    assert watched.link_count == 2


@pytest_asyncio.fixture
async def env(test_db, monkeypatch):
    import sys
    from backend.queue import JobQueue, QueueWorker
    from backend.routes.jobs import init_job_routes
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    worker = QueueWorker(test_db)
    monkeypatch.setattr(worker, "start", lambda *a, **kw: None)
    init_job_routes(worker, JobQueue(test_db))
    async with aiosqlite.connect(test_db) as db:
        for path, links in (("/m/seeding.mkv", 2), ("/m/alone.mkv", 1), ("/m/unknown.mkv", None)):
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, duration, video_codec, needs_conversion, "
                "audio_tracks_json, scan_timestamp, link_count) VALUES (?, 4000000000, 3600, 'h264', 1, '[]', "
                "'2026-10-10', ?)", (path, links))
        await db.commit()
    return test_db


async def _queued(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path FROM jobs ORDER BY file_path") as cur:
            return [r[0] for r in await cur.fetchall()]


async def _set(db_path, value):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('skip_hardlinked', ?)", (value,))
        await db.commit()


@pytest.mark.asyncio
async def test_the_filter(env):
    from backend.routes.scan import _paths_matching
    assert await _paths_matching("hardlinked") == ["/m/seeding.mkv"]


@pytest.mark.asyncio
async def test_the_queue_leaves_them_out_by_default(env):
    from backend.routes.jobs import BulkQueueFromScanRequest, EstimateRequest, add_jobs_from_scan, estimate_jobs
    paths = ["/m/seeding.mkv", "/m/alone.mkv", "/m/unknown.mkv"]
    est = await estimate_jobs(EstimateRequest(file_paths=paths))
    assert (est["hardlinked"], est["total_files"]) == (1, 2)
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=paths))  # no setting row: the default, on
    assert await _queued(env) == ["/m/alone.mkv", "/m/unknown.mkv"]
    # "Include ignored files and override rules" queues it anyway.
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=paths, override_rules=True))
    assert await _queued(env) == ["/m/alone.mkv", "/m/seeding.mkv", "/m/unknown.mkv"]


@pytest.mark.asyncio
async def test_turned_off_they_are_queued(env):
    from backend.routes.jobs import BulkQueueFromScanRequest, EstimateRequest, add_jobs_from_scan, estimate_jobs
    await _set(env, "false")
    assert (await estimate_jobs(EstimateRequest(file_paths=["/m/seeding.mkv"])))["hardlinked"] == 0
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=["/m/seeding.mkv"]))
    assert await _queued(env) == ["/m/seeding.mkv"]


@pytest.mark.asyncio
async def test_new_installs_skip_them_existing_ones_keep_converting(test_db, monkeypatch):
    import backend.routes.settings as settings_route
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    await settings_route.seed_v010_defaults()
    assert (await settings_route.get_encoding_settings())["skip_hardlinked"] is True  # new install
    async with aiosqlite.connect(test_db) as db:
        await db.execute("DELETE FROM settings WHERE key IN ('skip_hardlinked_seeded', 'skip_hardlinked')")
        await db.execute("INSERT INTO media_dirs (path) VALUES ('/media')")  # an install in use
        await db.commit()
    await settings_route.seed_v010_defaults()
    assert (await settings_route.get_encoding_settings())["skip_hardlinked"] is False
    async with aiosqlite.connect(test_db) as db:  # turned on by the user: stays on
        await db.execute("UPDATE settings SET value = 'true' WHERE key = 'skip_hardlinked'")
        await db.commit()
    await settings_route.seed_v010_defaults()
    assert (await settings_route.get_encoding_settings())["skip_hardlinked"] is True


@pytest.mark.asyncio
async def test_a_converted_file_has_no_other_links(env, tmp_path, monkeypatch):
    import backend.scanner as scanner
    from backend.queue import JobQueue, refresh_converted_scan_row

    async def probe(path, *a, **kw):
        return {"video_codec": "hevc", "audio_tracks": [], "subtitle_tracks": [], "duration": 60, "file_size": 1}
    monkeypatch.setattr(scanner, "probe_file", probe)
    job_id = await JobQueue(env).add_job("/m/seeding.mkv", "convert")
    await refresh_converted_scan_row(env, job_id, "/m/seeding.mkv", "/m/seeding.mkv")
    async with aiosqlite.connect(env) as db:
        async with db.execute("SELECT link_count FROM scan_results WHERE file_path = '/m/seeding.mkv'") as cur:
            assert (await cur.fetchone())[0] is None
