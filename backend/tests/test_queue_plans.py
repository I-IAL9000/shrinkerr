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


@pytest.mark.asyncio
async def test_a_jobs_estimate_uses_the_quality_it_will_encode_with(routes, test_db, monkeypatch):
    """A job without a quality of its own encodes at its encoder's setting
    (NVENC's CQ for an NVENC job) and its row says so ("P6 / CQ 36"), but the
    estimate took the default encoder's: here libx265's CRF 25, about CQ 23,
    so 3.0 GB "→ about 1.3 GB" where CQ 36 makes it about 0.6 GB."""
    from backend import savings_model
    from backend.encoding_estimates import cq_to_savings_pct
    jobs_route, _ = routes
    monkeypatch.setattr(savings_model, "_model", savings_model.SavingsModel())  # the curve alone
    await _file(test_db, "/m/a.mkv", [tr(1, "jpn", True)])
    await _file(test_db, "/m/b.mkv", [tr(1, "jpn", True)])
    nvenc = await _job(test_db, "/m/a.mkv", "convert", encoder="nvenc")
    default = await _job(test_db, "/m/b.mkv", "convert")  # no encoder: the default one's quality
    async with aiosqlite.connect(test_db) as db:
        for key, value in (("default_encoder", "libx265"), ("libx265_crf", "25"), ("nvenc_cq", "36")):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()
    assert (await jobs_route.job_plan(nvenc))["estimated_savings"] == int(4 * GB * cq_to_savings_pct(36))
    assert (await jobs_route.job_plan(default))["estimated_savings"] == int(4 * GB * cq_to_savings_pct(23))
    assert (await jobs_route.pending_summary())["estimated_savings"] == \
        int(4 * GB * cq_to_savings_pct(36)) + int(4 * GB * cq_to_savings_pct(23))


@pytest.mark.asyncio
async def test_add_to_queue_estimates_at_the_encoder_chosen(routes, test_db, monkeypatch):
    """Add to Queue's encoder (or a rule's) decides the quality an encode
    uses; the preview's savings took the default encoder's."""
    from backend import savings_model
    from backend.encoding_estimates import cq_to_savings_pct
    jobs_route, _ = routes
    monkeypatch.setattr(savings_model, "_model", savings_model.SavingsModel())
    await _file(test_db, "/m/a.mkv", [tr(1, "jpn", True)])
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE scan_results SET video_codec = 'h264', video_height = 1080, video_width = 1920")
        for key, value in (("default_encoder", "libx265"), ("libx265_crf", "25"), ("nvenc_cq", "36")):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()
    est = await jobs_route.estimate_jobs(jobs_route.EstimateRequest(file_paths=["/m/a.mkv"], encoder="nvenc"))
    assert est["estimated_savings"] == int(4 * GB * cq_to_savings_pct(36))
    est = await jobs_route.estimate_jobs(jobs_route.EstimateRequest(file_paths=["/m/a.mkv"]))
    assert est["estimated_savings"] == int(4 * GB * cq_to_savings_pct(23))  # the default: libx265 CRF 25


@pytest.mark.asyncio
async def test_a_jobs_plan_lists_its_direct_play_fixes(routes, test_db):
    """A clean-up queued for SRT copies of image subtitles read "Remux" with
    a plan of "only the tracks change" (v0.10.0): the plan lists the fixes
    the job makes on its file, and whether the copies replace the images."""
    jobs_route, _ = routes
    pgs = {"stream_index": 3, "language": "eng", "codec": "hdmv_pgs_subtitle", "keep": True}
    await _file(test_db, "/m/a.mkv", [tr(1, "jpn", True)], subs=[pgs])
    chosen = await _job(test_db, "/m/a.mkv", "audio", direct_play="subs")
    every = await _job(test_db, "/m/a.mkv", "audio", direct_play=1)  # the setting's: those the file has
    none = await _job(test_db, "/m/a.mkv", "audio")
    plan = await jobs_route.job_plan(chosen)
    assert plan["direct_play"] == ["subs"] and plan["image_subs_keep_original"] is True
    assert (await jobs_route.job_plan(every))["direct_play"] == ["subs"]
    assert (await jobs_route.job_plan(none))["direct_play"] == []
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('image_subs_keep_original', 'false')")
        await db.commit()
    assert (await jobs_route.job_plan(chosen))["image_subs_keep_original"] is False
