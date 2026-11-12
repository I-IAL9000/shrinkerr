"""The Dashboard's source cards (Source Types, Avg Reduction by Source,
Encoding Efficiency by Source) and Add to Queue's By Source: a disc or ISO
is a source of its own, "Disc / ISO" as the Scanner's filter calls it.
They were Other: the source was read from the file's name, and a disc's is
index.bdmv, VIDEO_TS.IFO or the .iso's own. The Resolution card read the
name too, so a disc was Unknown."""
import json
import sqlite3

import aiosqlite
import pytest

from backend.routes.stats import _stats_summary

GB = 1024 ** 3
JOBS = [  # the source (original_file_path), the output, original GB, saved GB
    ("/media/Movies/The Nest (2020)/BDMV/index.bdmv", "/media/Movies/The Nest (2020)/The Nest (2020).mkv", 40, 30),
    ("/media/Movies/Amelie (2001)/VIDEO_TS/VIDEO_TS.IFO", "/media/Movies/Amelie (2001)/Amelie (2001).mkv", 8, 6),
    ("/media/Movies/Heat (1995)/rz0u.iso", "/media/Movies/Heat (1995)/Heat (1995).mkv", 30, 20),
    ("/media/Movies/Mute (2018)/Mute.2018.1080p.BluRay.Remux.mkv", "/media/Movies/Mute (2018)/Mute.2018.1080p.x265.mkv", 30, 21),
    # A folder's name isn't the source: only a disc's own folder (BDMV, VIDEO_TS).
    ("/media/DVD Rips/Ran (1985)/Ran.1985.mkv", "/media/DVD Rips/Ran (1985)/Ran.1985.x265.mkv", 4, 1),
]


@pytest.mark.asyncio
async def test_a_disc_or_iso_is_a_source_of_its_own(test_db):
    con = sqlite3.connect(test_db)
    con.executemany(
        "INSERT INTO jobs (file_path, original_file_path, job_type, status, created_at, original_size, space_saved) "
        "VALUES (?, ?, 'convert', 'completed', datetime('now'), ?, ?)",
        [(output, source, size * GB, saved * GB) for source, output, size, saved in JOBS])
    con.commit()
    con.close()
    stats = _stats_summary(test_db)
    assert dict(stats["source_types"]) == {"Disc / ISO": 3, "Remux": 1, "Other": 1}
    assert stats["savings_by_source"]["Disc / ISO"] == {
        "saved": 56 * GB, "original": 78 * GB, "count": 3, "percent": 71.8}


@pytest.mark.asyncio
async def test_a_disc_has_its_sources_resolution(test_db):
    """The name doesn't tell (a disc's never does): the source's measured
    size decides. A job converted before it was recorded stays Unknown."""
    def stats(width, height):
        return json.dumps({"source_width": width, "source_height": height}) if height else None
    jobs = [("/media/Movies/The Nest (2020)/BDMV/index.bdmv", stats(1920, 1080)),
            ("/media/Movies/Amelie (2001)/VIDEO_TS/VIDEO_TS.IFO", stats(720, 576)),
            ("/media/Movies/Heat (1995)/rz0u.iso", None),
            ("/media/Movies/Mute (2018)/Mute.2018.720p.BluRay.mkv", stats(1920, 1080))]  # the name decides
    con = sqlite3.connect(test_db)
    con.executemany(
        "INSERT INTO jobs (file_path, original_file_path, job_type, status, created_at, original_size, "
        "space_saved, encoding_stats) VALUES (?, ?, 'convert', 'completed', datetime('now'), ?, ?, ?)",
        [(source + ".mkv", source, 10 * GB, 5 * GB, enc) for source, enc in jobs])
    con.commit()
    con.close()
    assert dict(_stats_summary(test_db)["resolutions"]) == {"1080p": 1, "SD": 1, "Unknown": 1, "720p": 1}


@pytest.mark.asyncio
async def test_add_to_queue_sorts_sources_as_the_dashboard_does(test_db, monkeypatch):
    import sys
    from backend.queue import JobQueue, QueueWorker
    from backend.routes.jobs import EstimateRequest, estimate_jobs, init_job_routes
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    init_job_routes(QueueWorker(test_db), JobQueue(test_db))
    rows = [("/media/Movies/The Nest (2020)/BDMV/index.bdmv", "bdmv"),
            ("/media/Movies/Amelie (2001)/VIDEO_TS/VIDEO_TS.IFO", "dvd"),
            ("/media/Movies/Heat (1995)/rz0u.iso", "bdmv"),
            # Remux before Blu-ray, as the Dashboard has it (Add to Queue said Blu-ray).
            ("/media/Movies/Mute (2018)/Mute.2018.1080p.BluRay.Remux.mkv", None),
            ("/media/Movies/Ran (1985)/Ran.1985.1080p.BluRay.x264.mkv", None)]
    async with aiosqlite.connect(test_db) as db:
        await db.executemany(
            "INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, video_width, "
            "video_height, duration, audio_tracks_json, subtitle_tracks_json, scan_timestamp, disc_type) "
            "VALUES (?, ?, 'h264', 1, 1920, 1080, 600, '[]', '[]', '2026-01-01', ?)",
            [(path, 10 * GB, disc) for path, disc in rows])
        await db.commit()
    estimate = await estimate_jobs(EstimateRequest(file_paths=[path for path, _ in rows]))
    assert estimate["by_source"] == {"Disc / ISO": 3, "Remux": 1, "Blu-ray": 1}
