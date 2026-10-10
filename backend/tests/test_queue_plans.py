"""Pending jobs follow their files' tracks, and say what they'll do (v0.10.0).

A pending job's track removals came from its file when it was queued and
never changed. Now they're re-derived when the keep languages change or the
file's tracks are edited (in the Scanner or in the queue), and the queue can
show each job's plan and a summary of the whole pending queue."""
import json

import aiosqlite
import pytest

from backend.scanner import TrackRules

NOW = "2026-10-10T00:00:00"
GB = 1024 ** 3


def tr(si, lang, keep, **extra):
    return {"stream_index": si, "language": lang, "codec": "aac", "channels": 2,
            "keep": keep, "locked": False, "size_estimate_bytes": 100 * 1024 ** 2, **extra}


@pytest.fixture
def routes(test_db, monkeypatch):
    import backend.config
    import backend.routes.jobs as jobs_route
    import backend.routes.scan as scan_route
    import backend.scanner as scanner
    monkeypatch.setattr(scan_route, "DB_PATH", test_db)
    monkeypatch.setattr(scanner, "settings", backend.config.settings)
    scanner.invalidate_sub_settings_cache()
    yield jobs_route, scan_route
    scanner.invalidate_sub_settings_cache()


async def _file(db_path, path, audio, subs=None, needs=1):
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO scan_results (file_path, file_size, scan_timestamp, native_language, duration, "
            "needs_conversion, audio_tracks_json, subtitle_tracks_json) VALUES (?, ?, ?, 'jpn', 3600, ?, ?, ?)",
            (path, 4 * GB, NOW, needs, json.dumps(audio), json.dumps(subs) if subs is not None else None))
        await db.commit()
        return cur.lastrowid


async def _job(db_path, path, job_type, audio_remove=(), status="pending", **cols):
    async with aiosqlite.connect(db_path) as db:
        names = ", ".join(["file_path", "job_type", "status", "created_at", "audio_tracks_to_remove",
                           "subtitle_tracks_to_remove", *cols])
        cur = await db.execute(
            f"INSERT INTO jobs ({names}) VALUES ({', '.join('?' * (6 + len(cols)))})",
            (path, job_type, status, NOW, json.dumps(list(audio_remove)), "[]", *cols.values()))
        await db.commit()
        return cur.lastrowid


async def _jobrow(db_path, jid):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT job_type, audio_tracks_to_remove FROM jobs WHERE id = ?", (jid,)) as cur:
            jt, a = await cur.fetchone()
    return jt, json.loads(a)


@pytest.mark.asyncio
async def test_pending_jobs_follow_their_files(routes, test_db):
    jobs_route, _ = routes
    await _file(test_db, "/m/a.mkv", [tr(1, "jpn", True), tr(2, "eng", True)])
    await _file(test_db, "/m/b.mkv", [tr(1, "jpn", True), tr(2, "fra", True)])
    await _file(test_db, "/m/c.mkv", [tr(1, "jpn", True), tr(2, "spa", True)], needs=0)
    combined = await _job(test_db, "/m/a.mkv", "combined", [2])   # its cleanup is gone now
    convert = await _job(test_db, "/m/b.mkv", "convert")
    running = await _job(test_db, "/m/b.mkv", "convert", status="running")
    audio = await _job(test_db, "/m/c.mkv", "audio", [2])
    async with aiosqlite.connect(test_db) as db:  # b's French goes now
        await db.execute("UPDATE scan_results SET audio_tracks_json = ? WHERE file_path = '/m/b.mkv'",
                         (json.dumps([tr(1, "jpn", True), tr(2, "fra", False)]),))
        await db.commit()
    assert await jobs_route.refresh_pending_jobs() == 3
    assert await _jobrow(test_db, combined) == ("convert", [])
    assert await _jobrow(test_db, convert) == ("combined", [2])
    assert await _jobrow(test_db, running) == ("convert", [])     # started: left alone
    assert await _jobrow(test_db, audio) == ("audio", [])         # cleanup-only keeps its type
    assert await jobs_route.refresh_pending_jobs() == 0


@pytest.mark.asyncio
async def test_editing_a_files_tracks_updates_its_pending_job(routes, test_db):
    jobs_route, scan_route = routes
    sid = await _file(test_db, "/m/a.mkv", [tr(1, "jpn", True), tr(2, "eng", True)])
    jid = await _job(test_db, "/m/a.mkv", "convert")
    await scan_route.update_audio_tracks(sid, scan_route.UpdateTracksRequest(
        audio_tracks_json=json.dumps([tr(1, "jpn", True), tr(2, "eng", False)])))
    assert await _jobrow(test_db, jid) == ("combined", [2])
    plan = await jobs_route.job_plan(jid)
    assert plan["scan_id"] == sid and plan["job_type"] == "combined"
    assert [(t["language"], t["remove"], t.get("manual", False)) for t in plan["audio"]] == [
        ("jpn", False, False), ("eng", True, True)]


@pytest.mark.asyncio
async def test_a_language_change_reaches_pending_jobs(routes, test_db):
    jobs_route, scan_route = routes
    await _file(test_db, "/m/a.mkv", [tr(1, "jpn", True), tr(2, "eng", True), tr(3, "fra", False)])
    jid = await _job(test_db, "/m/a.mkv", "combined", [3])
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('always_keep_languages', '[\"eng\", \"fra\"]')")
        await db.commit()
    old = TrackRules(audio_keep=frozenset({"eng"}), subs_enabled=False)
    assert await scan_route.reapply_track_rules(old) == 1
    assert await _jobrow(test_db, jid) == ("convert", [])


@pytest.mark.asyncio
async def test_the_pending_summary(routes, test_db):
    jobs_route, _ = routes
    await _file(test_db, "/m/a.mkv", [tr(1, "jpn", True), tr(2, "eng", False), tr(3, "fra", False)],
                subs=[tr(4, "fra", False)])
    await _file(test_db, "/m/b.mkv", [tr(1, "jpn", True), tr(2, "fra", False)], needs=0)
    await _job(test_db, "/m/a.mkv", "combined", [2, 3], encoder="qsv", nvenc_cq=26)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE jobs SET subtitle_tracks_to_remove = '[4]' WHERE file_path = '/m/a.mkv'")
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('backup_original_days', '7')")
        await db.commit()
    await _job(test_db, "/m/b.mkv", "audio", [2])
    await _job(test_db, "/m/b.mkv", "convert", status="completed")
    await _job(test_db, "/m/a.mkv", "health_check")  # changes nothing: no size, no encoder
    s = await jobs_route.pending_summary()
    assert s["jobs"] == 3 and s["by_type"] == {"combined": 1, "audio": 1, "health_check": 1}
    assert s["by_encoder"] == {"qsv": 1}
    assert s["removals"] == {"audio": {"eng": 1, "fra": 2}, "subtitles": {"fra": 1}}
    assert s["total_size"] == 8 * GB and s["originals"] == {"action": "keep", "days": 7}
    # a: two removed tracks (200 MB), then CQ 26 (70%) on the rest; b: one track.
    mb = 1024 ** 2
    assert s["estimated_savings"] == 200 * mb + int((4 * GB - 200 * mb) * 0.70) + 100 * mb
