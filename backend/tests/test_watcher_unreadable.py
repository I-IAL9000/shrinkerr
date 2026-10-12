"""A new file ffprobe can't read. A full scan lists it as corrupt (Status →
Health → Corrupt, where it can be replaced); the watcher set it aside and
tried again every 30 minutes, forever, so it never showed up anywhere
("Xtro 2 … DVD -RoT.mkv"). It's listed as a scan lists it once it has failed
twice and hasn't changed for an hour: a file still being copied fails too."""
import os
import time
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from backend import watcher as watcher_mod
from backend.watcher import FileWatcher

HOUR = 3600


async def _library(db_path, tmp_path, *, age):
    media = tmp_path / "Movies"
    known = media / "Film A (2020)" / "Film A (2020).mkv"
    broken = media / "Xtro 2 (1990)" / "Xtro 2 (1990) DVD -RoT.mkv"
    for p in (known, broken):
        p.parent.mkdir(parents=True)
        p.write_bytes(b"x" * 1000)
    stamp = time.time() - age
    os.utime(broken, (stamp, stamp))
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO media_dirs (path, auto_scan) VALUES (?, 1)", (str(media),))
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) "
                         "VALUES (?, 1, '2026-10-12T00:00:00')", (str(known),))
        await db.commit()
    return str(broken)


async def _row(db_path, path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT probe_status, video_codec, needs_conversion, file_size "
                              "FROM scan_results WHERE file_path = ?", (path,)) as cur:
            return await cur.fetchone()


async def _cycle(w, probe_result=None):
    with patch("backend.scanner.probe_file", new_callable=AsyncMock, return_value=probe_result):
        await w.check_once()


def _cooldown_passes(w, path):
    w._probe_failures[path] -= watcher_mod._PROBE_RETRY_COOLDOWN_S + 1


@pytest.mark.asyncio
async def test_a_file_that_never_reads_is_listed_as_corrupt(test_db, tmp_path):
    broken = await _library(test_db, tmp_path, age=2 * HOUR)
    w = FileWatcher(test_db, interval_minutes=5)
    await _cycle(w)
    assert await _row(test_db, broken) is None  # once: maybe still being copied
    await _cycle(w)
    assert await _row(test_db, broken) is None  # set aside for 30 minutes, as before
    _cooldown_passes(w, broken)
    await _cycle(w)
    assert await _row(test_db, broken) == ("corrupt", "unknown", 0, 1000)  # as a full scan lists it
    assert broken not in w._probe_failures and broken not in w._probe_failure_counts


@pytest.mark.asyncio
async def test_a_file_still_changing_isnt(test_db, tmp_path):
    broken = await _library(test_db, tmp_path, age=5 * 60)  # written 5 minutes ago
    w = FileWatcher(test_db, interval_minutes=5)
    for _ in range(3):
        await _cycle(w)
        _cooldown_passes(w, broken)
    assert await _row(test_db, broken) is None


@pytest.mark.asyncio
async def test_a_file_that_reads_the_second_time_is_listed_normally(test_db, tmp_path):
    broken = await _library(test_db, tmp_path, age=2 * HOUR)
    w = FileWatcher(test_db, interval_minutes=5)
    await _cycle(w)
    _cooldown_passes(w, broken)
    probe = {"video_codec": "h264", "video_width": 720, "video_height": 480, "audio_tracks": [],
             "subtitle_tracks": [], "duration": 60, "file_size": 1000}
    await _cycle(w, probe)
    assert (await _row(test_db, broken))[:2] == ("ok", "h264")
    assert broken not in w._probe_failure_counts


@pytest.mark.asyncio
async def test_a_disc_that_doesnt_read_is_still_tried_again(test_db, tmp_path):
    """Disc folders read slowly over the network and fail now and then: they
    keep being retried."""
    media = tmp_path / "Movies"
    disc = media / "Disc (1999)" / "VIDEO_TS"
    disc.mkdir(parents=True)
    (disc / "VIDEO_TS.IFO").write_bytes(b"x")
    (disc / "VTS_01_1.VOB").write_bytes(b"x")
    known = media / "Film A (2020)" / "Film A (2020).mkv"
    known.parent.mkdir(parents=True)
    known.write_bytes(b"x")
    marker = str(disc / "VIDEO_TS.IFO")
    stamp = time.time() - 2 * HOUR
    for p in (disc / "VIDEO_TS.IFO", disc / "VTS_01_1.VOB"):
        os.utime(p, (stamp, stamp))
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO media_dirs (path, auto_scan) VALUES (?, 1)", (str(media),))
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) "
                         "VALUES (?, 1, '2026-10-12T00:00:00')", (str(known),))
        await db.commit()
    w = FileWatcher(test_db, interval_minutes=5)
    for _ in range(3):
        await _cycle(w)
        if marker in w._probe_failures:
            _cooldown_passes(w, marker)
    assert await _row(test_db, marker) is None


@pytest.mark.asyncio
async def test_a_full_scan_lists_it_the_same_way(test_db, tmp_path):
    from backend.scanner import scan_directory
    folder = tmp_path / "media" / "Xtro 2 (1990)"
    folder.mkdir(parents=True)
    (folder / "Xtro 2 (1990) DVD -RoT.mkv").write_bytes(b"not a video" * 100)
    (folder / "Xtro 2 (1990) DVD -RoT.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    with patch("backend.scanner.probe_file", new_callable=AsyncMock, return_value=None):
        results = await scan_directory(str(tmp_path / "media"))
    (row,) = results
    assert (row.probe_status, row.video_codec, row.needs_conversion, row.file_size) == ("corrupt", "unknown", False, 1100)
    assert row.has_external_subs and [t.language for t in row.subtitle_tracks] == ["eng"]
