"""The Dashboard's source cards (Source Types, Avg Reduction by Source,
Encoding Efficiency by Source): a converted disc or ISO is a source of its
own, "Disc / ISO" as the Scanner's filter calls it. They were Other: the
source was read from the file's name, and a disc's is index.bdmv,
VIDEO_TS.IFO or the .iso's own."""
import sqlite3

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
