"""Savings figures agree (v0.10.0): the Scanner card used its own curve (25% at
CQ 20 where Add to Queue said 45%), and every estimate read the NVENC CQ even
when jobs encode with libx265, QSV, VAAPI or VideoToolbox."""
import aiosqlite
import pytest
import pytest_asyncio

from backend.encoding_estimates import cq_to_savings_pct, effective_cq

GB = 1024 ** 3


@pytest.mark.parametrize("values, cq", [
    ({}, 20),                                                   # stored defaults: NVENC CQ 20
    ({"default_encoder": "nvenc", "nvenc_cq": "24"}, 24),
    ({"default_encoder": "libx265", "libx265_crf": "20", "nvenc_cq": "28"}, 18),
    ({"default_encoder": "qsv", "qsv_cq": "25"}, 25),
    ({"default_encoder": "vaapi", "vaapi_qp": "21"}, 21),
    ({"default_encoder": "videotoolbox", "videotoolbox_quality": "55"}, 23),
    ({"default_encoder": "videotoolbox", "videotoolbox_quality": "50"}, 26),
    ({"default_encoder": "videotoolbox", "videotoolbox_quality": "70"}, 12),
])
def test_the_default_encoders_quality_on_the_cq_scale(values, cq):
    assert effective_cq(values) == cq


@pytest_asyncio.fixture
async def env(test_db, monkeypatch):
    import sys
    from backend.queue import JobQueue, QueueWorker
    from backend.routes.jobs import init_job_routes
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    init_job_routes(QueueWorker(test_db), JobQueue(test_db))
    async with aiosqlite.connect(test_db) as db:
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, "
            "video_width, video_height, duration, audio_tracks_json, subtitle_tracks_json, scan_timestamp) "
            "VALUES ('/m/Film (2020)/film.mkv', ?, 'h264', 1, 1920, 1080, 600, '[]', '[]', '2026-01-01')",
            (GB,))
        # Content detection off: only the global quality decides.
        await db.execute("INSERT INTO settings (key, value) VALUES ('content_type_detection', 'false')")
        await db.commit()
    return test_db


async def _both(db_path):
    from backend import savings_model
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    from backend.routes.scan import get_scan_stats
    await savings_model.reestimate_scan_rows()  # as the app does when the quality changes
    card = (await get_scan_stats())["summary"]["estimated_savings_bytes"]
    modal = (await estimate_jobs(EstimateRequest(file_paths=["/m/Film (2020)/film.mkv"])))["estimated_savings"]
    return card, modal


@pytest.mark.asyncio
async def test_the_scanner_card_and_add_to_queue_agree(env):
    card, modal = await _both(env)
    assert card == modal == int(GB * cq_to_savings_pct(20))


@pytest.mark.asyncio
async def test_they_use_what_conversions_taught(env, monkeypatch):
    """v0.10.0: the estimate learned from finished conversions, for files
    like this one (an h264 1080p film at ~0.29 bits per pixel: band 3; no
    source in its name)."""
    from backend import savings_model as sm
    from backend.routes.jobs import _job_savings
    monkeypatch.setattr(sm, "_model", sm.SavingsModel(
        {(): 1.0, (3,): 1.0, (3, "other"): 1.0, (3, "other", "h264"): 0.5}, samples=40))
    card, modal = await _both(env)
    assert card == modal == int(GB * cq_to_savings_pct(20) * 0.5)
    row = {"file_size": GB, "duration": 600, "video_width": 1920, "video_height": 1080, "video_codec": "h264"}
    assert _job_savings({"job_type": "convert", "nvenc_cq": 20}, row, [], 20) == int(GB * cq_to_savings_pct(20) * 0.5)


@pytest.mark.asyncio
async def test_a_libx265_default_estimates_from_its_crf(env):
    async with aiosqlite.connect(env) as db:
        for key, value in (("default_encoder", "libx265"), ("libx265_crf", "25"), ("nvenc_cq", "18")):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()
    card, modal = await _both(env)
    assert card == modal == int(GB * cq_to_savings_pct(23))


@pytest.mark.asyncio
async def test_the_estimate_says_what_will_happen(env, monkeypatch):
    """The Add to Queue "what will happen" panel: tracks removed by language,
    what becomes of the originals, and the encoder this server will run."""
    import json
    import backend.encoder_caps as encoder_caps
    from backend.routes.jobs import EstimateRequest, estimate_jobs

    class Caps:
        available = ["libx265", "qsv"]
    monkeypatch.setattr(encoder_caps, "detect_encoders", lambda *a, **kw: Caps())
    audio = [{"stream_index": 1, "language": "eng", "keep": True}, {"stream_index": 2, "language": "fre", "keep": False},
             {"stream_index": 3, "language": "fre", "keep": False},
             {"stream_index": 4, "language": "ger", "keep": False, "locked": True}]
    subs = [{"stream_index": 5, "language": "spa", "keep": False}]
    async with aiosqlite.connect(env) as db:
        await db.execute("UPDATE scan_results SET audio_tracks_json = ?, subtitle_tracks_json = ?",
                         (json.dumps(audio), json.dumps(subs)))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('backup_original_days', '7')")
        await db.commit()
    est = await estimate_jobs(EstimateRequest(file_paths=["/m/Film (2020)/film.mkv"], encoder="nvenc"))
    # A locked track unticked by hand goes too — the job removes it (v0.9.99).
    assert est["removals"] == {"audio": {"fre": 2, "ger": 1}, "subtitles": {"spa": 1}}
    assert est["originals"] == {"action": "keep", "days": 7}
    assert est["encoder"] == {"requested": "nvenc", "runs_here": "qsv"}


@pytest.mark.asyncio
async def test_time_estimates_use_each_encoders_recent_speed(env):
    """F13 (v0.10.0): one pass over recent conversions gives both encoders'
    speeds (it walked the whole job history once per encoder)."""
    import json
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    async with aiosqlite.connect(env) as db:
        for i, (encoder, ratio) in enumerate([("hevc_nvenc", 0.1)] * 3 + [("libx265", 0.5)] * 3 + [("qsv", 2.0)]):
            path = f"/m/Done/{i}.mkv"
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, duration, scan_timestamp) "
                "VALUES (?, 1, 600, '2026-01-01')", (path,))
            await db.execute(
                "INSERT INTO jobs (file_path, job_type, status, encoder, encoding_stats, created_at, completed_at) "
                "VALUES (?, 'convert', 'completed', ?, ?, '2026-10-01', ?)",
                (path, encoder, json.dumps({"encode_seconds": 600 * ratio, "duration": 600}), f"2026-10-0{i + 1}"))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('parallel_jobs', '1')")
        await db.commit()

    async def seconds(encoder):
        est = await estimate_jobs(EstimateRequest(file_paths=["/m/Film (2020)/film.mkv"], encoder=encoder))
        return est["estimated_time_seconds"]

    async with aiosqlite.connect(env) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('default_encoder', 'nvenc')")
        await db.commit()
    assert await seconds("nvenc") == 60     # 600 s of video at 0.1
    async with aiosqlite.connect(env) as db:
        await db.execute("UPDATE settings SET value = 'libx265' WHERE key = 'default_encoder'")
        await db.commit()
    assert await seconds("libx265") == 300  # at 0.5


@pytest.mark.asyncio
async def test_a_resize_is_in_the_estimate(env):
    """A 4K file resized to 1080p — by the dialog, a rule or the setting —
    is estimated resized: the dialog said 55 % where it saves 85 %+, and the
    Scanner card agrees with it (v0.10.0)."""
    from backend import savings_model
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    from backend.routes.scan import get_scan_stats
    async with aiosqlite.connect(env) as db:
        await db.execute("UPDATE scan_results SET video_width = 3840, video_height = 2160")
        await db.commit()
    req = EstimateRequest(file_paths=["/m/Film (2020)/film.mkv"])
    same = (await estimate_jobs(req))["estimated_savings"]
    resized = int(GB * (1 - (1 - cq_to_savings_pct(20)) * 0.25 ** savings_model.RESIZE_ALPHA))
    dialog = (await estimate_jobs(EstimateRequest(file_paths=req.file_paths, target_resolution_override="1080p")))
    assert same == int(GB * cq_to_savings_pct(20)) and dialog["estimated_savings"] == resized
    async with aiosqlite.connect(env) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('target_resolution', '1080p')")
        await db.commit()
    assert (await estimate_jobs(req))["estimated_savings"] == resized  # the setting
    assert (await estimate_jobs(EstimateRequest(file_paths=req.file_paths, target_resolution_override="copy"))
            )["estimated_savings"] == same  # the dialog keeps the size
    await savings_model.tick()  # the app notices the setting changed
    assert (await get_scan_stats())["summary"]["estimated_savings_bytes"] == resized



@pytest.mark.asyncio
async def test_the_dialog_knows_whether_srt_copies_replace_image_subtitles(env):
    """"No audio or subtitle tracks are removed" beside SRT copies that
    replace the image subtitles (v0.10.0)."""
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    req = EstimateRequest(file_paths=["/m/Film (2020)/film.mkv"])
    assert (await estimate_jobs(req))["image_subs_keep_original"] is True
    async with aiosqlite.connect(env) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('image_subs_keep_original', 'false')")
        await db.commit()
    assert (await estimate_jobs(req))["image_subs_keep_original"] is False


@pytest.mark.asyncio
async def test_cleanup_only_estimates_no_conversion(env):
    """Add to Queue's Cleanup only changed the summary, not the estimate:
    ~193 MB saved and "conversions 1" for a clean-up (v0.10.0)."""
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    paths = ["/m/Film (2020)/film.mkv"]
    assert (await estimate_jobs(EstimateRequest(file_paths=paths)))["estimated_savings"] > 0
    cleanup = await estimate_jobs(EstimateRequest(file_paths=paths, cleanup_only=True))
    assert cleanup["estimated_savings"] == 0 and not cleanup["by_type"].get("convert")
