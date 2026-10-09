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
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    from backend.routes.scan import get_scan_stats
    card = (await get_scan_stats())["summary"]["estimated_savings_bytes"]
    modal = (await estimate_jobs(EstimateRequest(file_paths=["/m/Film (2020)/film.mkv"])))["estimated_savings"]
    return card, modal


@pytest.mark.asyncio
async def test_the_scanner_card_and_add_to_queue_agree(env):
    card, modal = await _both(env)
    assert card == modal == int(GB * cq_to_savings_pct(20))


@pytest.mark.asyncio
async def test_a_libx265_default_estimates_from_its_crf(env):
    async with aiosqlite.connect(env) as db:
        for key, value in (("default_encoder", "libx265"), ("libx265_crf", "25"), ("nvenc_cq", "18")):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()
    card, modal = await _both(env)
    assert card == modal == int(GB * cq_to_savings_pct(23))
