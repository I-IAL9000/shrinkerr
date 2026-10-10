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


# ── The Scanner's flag (the Audio cleanup filter) uses the same rule ──────────

def test_the_flag_rule(cleanup_settings):
    from backend.models import AudioTrack
    from backend.scanner import removable_audio_flag
    assert removable_audio_flag([a(1, "eng"), a(2, "jpn")], "jpn") == 1
    assert removable_audio_flag([a(1, "eng"), a(2, "spa")], "jpn") == 0   # nothing to move
    assert removable_audio_flag([a(1, "jpn"), a(2, "eng", keep=False)], "jpn") == 1  # a removal
    models = [AudioTrack(stream_index=1, language="eng", codec="aac", channels=2, keep=True),
              AudioTrack(stream_index=2, language="jpn", codec="aac", channels=2, keep=True)]
    assert removable_audio_flag(models, "jpn") == 1


async def _flags(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path, has_removable_tracks_flag FROM scan_results") as cur:
            return dict(await cur.fetchall())


@pytest.mark.asyncio
async def test_a_scan_sets_the_flag(jobs_env):
    import backend.routes.scan as scan_route
    from backend.models import AudioTrack, ScannedFile
    async with aiosqlite.connect(jobs_env) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('always_keep_languages', '[\"eng\", \"fre\"]')")
        # A stored TMDB native (French) wins over the scan's guess (English).
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, native_language, language_source, audio_tracks_json, "
            "scan_timestamp) VALUES ('/m/C/c.mkv', 1, 'fre', 'api', '[]', ?)", (NOW,))
        await db.commit()

    def scanned(path, langs, native):
        return ScannedFile(
            file_path=path, file_name=path.rsplit("/", 1)[1], folder_name="x", file_size=1, file_size_gb=0.0,
            video_codec="h264", needs_conversion=False, native_language=native, has_removable_tracks=False,
            estimated_savings_bytes=0, estimated_savings_gb=0.0,
            audio_tracks=[AudioTrack(stream_index=i, language=lang, codec="aac", channels=2, keep=True)
                          for i, lang in enumerate(langs, 1)])

    scan_route._write_batch_sync(jobs_env, [
        scanned("/m/A/a.mkv", ["eng", "jpn"], "jpn"),
        scanned("/m/B/b.mkv", ["eng", "spa"], "jpn"),
        scanned("/m/C/c.mkv", ["fre", "eng"], "eng"),
    ], NOW)
    flags = await _flags(jobs_env)
    assert (flags["/m/A/a.mkv"], flags["/m/B/b.mkv"], flags["/m/C/c.mkv"]) == (1, 0, 0)


@pytest.mark.asyncio
async def test_a_track_edit_keeps_the_reorder_in_the_flag(jobs_env):
    import backend.routes.scan as scan_route
    async with aiosqlite.connect(jobs_env) as db:
        async with db.execute("SELECT id FROM scan_results WHERE file_path = '/m/reorder.mkv'") as cur:
            (rid,) = await cur.fetchone()
    await scan_route.update_audio_tracks(rid, scan_route.UpdateTracksRequest(
        audio_tracks_json=json.dumps([a(1, "eng"), a(2, "jpn")])))
    assert (await _flags(jobs_env))["/m/reorder.mkv"] == 1


@pytest.mark.asyncio
async def test_existing_flags_are_rechecked_once(jobs_env):
    import backend.routes.scan as scan_route
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    async with aiosqlite.connect(jobs_env) as db:
        # As older versions left them: the reorder missing, or set with no
        # Japanese track to move.
        await db.execute("UPDATE scan_results SET has_removable_tracks_flag = 0")
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, native_language, audio_tracks_json, "
            "has_removable_tracks_flag, scan_timestamp) VALUES ('/m/dubs.mkv', 1, 'jpn', ?, 1, ?)",
            (json.dumps([a(1, "eng"), a(2, "spa")]), NOW))
        await db.commit()
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=["/m/convert.mkv"]))
    async with aiosqlite.connect(jobs_env) as db:  # queued before: no reorder
        await db.execute("UPDATE jobs SET job_type = 'convert'")
        await db.commit()

    assert await scan_route.realign_audio_flags_once() == 3
    assert await _flags(jobs_env) == {"/m/convert.mkv": 1, "/m/reorder.mkv": 1, "/m/nothing.mkv": 0, "/m/dubs.mkv": 0}
    assert (await _jobs(jobs_env))["/m/convert.mkv"] == "combined"  # the pending job follows
    async with aiosqlite.connect(jobs_env) as db:
        await db.execute("UPDATE scan_results SET has_removable_tracks_flag = 0")
        await db.commit()
    assert await scan_route.realign_audio_flags_once() == 0  # once


@pytest.mark.asyncio
async def test_turning_the_reorder_off_rechecks_the_flags(jobs_env, monkeypatch):
    import backend.routes.scan as scan_route
    import backend.routes.settings as settings_route
    from backend.models import SettingsUpdate
    monkeypatch.setattr(settings_route, "DB_PATH", jobs_env)
    scheduled = []
    monkeypatch.setattr(scan_route, "schedule_audio_flags_realign", lambda: scheduled.append(1))
    await settings_route.update_encoding_settings(SettingsUpdate(nvenc_cq=21))
    assert scheduled == []
    await settings_route.update_encoding_settings(SettingsUpdate(reorder_native_audio=False))
    assert scheduled == [1]
    async with aiosqlite.connect(jobs_env) as db:
        await db.execute("UPDATE scan_results SET has_removable_tracks_flag = 1")
        await db.commit()
    await scan_route.realign_audio_flags()
    assert set((await _flags(jobs_env)).values()) == {0}  # off: no reorder counts


@pytest.mark.asyncio
async def test_a_language_change_keeps_the_reorder_in_the_flag(jobs_env):
    """English was removed; keeping it now leaves only the reorder to do."""
    import backend.routes.scan as scan_route
    from backend.scanner import TrackRules
    async with aiosqlite.connect(jobs_env) as db:
        await db.execute("UPDATE scan_results SET audio_tracks_json = ?, has_removable_tracks_flag = 1 "
                         "WHERE file_path = '/m/reorder.mkv'", (json.dumps([a(1, "eng", keep=False), a(2, "jpn")]),))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('always_keep_languages', '[\"eng\"]')")
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('sub_cleanup_enabled', 'false')")
        await db.commit()
    await scan_route.reapply_track_rules(TrackRules(audio_keep=frozenset(), subs_enabled=False))
    assert (await _flags(jobs_env))["/m/reorder.mkv"] == 1
