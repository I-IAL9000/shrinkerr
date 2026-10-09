"""v0.10.0 (SC-11): the watcher and the Scanner walk alike.

The Scanner skips a source whose converted (x265 / h265) sibling is already
there, and a disc whose output exists; the watcher didn't, so it re-added
them every cycle (and auto-queue converted them again). Both walkers also
skipped every file in a folder holding VIDEO_TS / BDMV, so a video next to
a disc never showed up.
"""
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from backend.scanner import already_converted, folder_candidates
from backend.tests.test_disc_release_folder import _dvd

EXT = {".mkv", ".mp4", ".iso"}


def test_a_video_next_to_a_disc_is_a_candidate(tmp_path):
    title = tmp_path / "Fast-Walking (1982)"
    marker = _dvd(title)
    (title / "Fast-Walking (1982) 480p DVDRip AC3 2.0 x265.mkv").write_bytes(b"x")
    dirs = ["VIDEO_TS", "AUDIO_TS", "Extras"]
    got = folder_candidates(title, dirs, [p.name for p in title.iterdir() if p.is_file()], EXT)
    assert sorted(p.name for p in got) == ["Fast-Walking (1982) 480p DVDRip AC3 2.0 x265.mkv", "VIDEO_TS.IFO"]
    assert dirs == ["AUDIO_TS", "Extras"]  # no descent into the disc itself
    assert str(marker) in already_converted(got, log=False)  # its output is there


def test_a_source_with_a_converted_sibling_is_skipped(tmp_path):
    src = tmp_path / "Show - S01E01 - 1080p WEB h264.mkv"
    out = tmp_path / "Show - S01E01 - 1080p WEB x265.mkv"
    other = tmp_path / "Show - S01E02 - 1080p WEB h264.mkv"
    assert already_converted([src, out, other], log=False) == {str(src)}


@pytest.mark.asyncio
async def test_the_watcher_skips_what_the_scanner_skips(test_db, tmp_path):
    from backend.watcher import FileWatcher
    media = tmp_path / "TV"
    (media / "Show").mkdir(parents=True)
    import os
    import time
    for name in ("Show - S01E01 - 1080p WEB h264.mkv", "Show - S01E01 - 1080p WEB x265.mkv"):
        (media / "Show" / name).write_bytes(b"x")
        old = time.time() - 3600  # past the watcher's "still being copied" delay
        os.utime(media / "Show" / name, (old, old))
    known = media / "Show" / "Show - S01E00 - Pilot.mkv"  # the watcher only watches scanned folders
    known.write_bytes(b"x")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO media_dirs (path, auto_scan) VALUES (?, 1)", (str(media),))
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) "
                         "VALUES (?, 1, '2026-10-08T00:00:00')", (str(known),))
        await db.commit()

    with patch("backend.scanner.probe_file", new_callable=AsyncMock) as probe:
        probe.return_value = None  # nothing gets registered; we only look at what was tried
        await FileWatcher(test_db, interval_minutes=5).check_once()

    probed = sorted(Path(c.args[0]).name for c in probe.await_args_list)
    assert probed == ["Show - S01E01 - 1080p WEB x265.mkv"]


@pytest.mark.asyncio
async def test_a_moved_file_keeps_its_row(test_db, tmp_path):
    """SC-20: a rename or move was a delete plus a fresh "New" row — the
    manual match, track edits, health result and converted flag were lost."""
    import os
    import time
    from backend.watcher import FileWatcher
    media = tmp_path / "Movies"
    old = media / "Film (2009)" / "Film (2009) 1080p WEB h264.mkv"
    new = media / "Film (2009) [tt1]" / "Film (2009) 1080p WEB h264.mkv"
    new.parent.mkdir(parents=True)
    with open(new, "wb") as f:
        f.truncate(12 * 1024 * 1024)  # sparse: a real size without the disk use
    stamp = time.time() - 3600
    os.utime(new, (stamp, stamp))
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO media_dirs (path, auto_scan) VALUES (?, 1)", (str(media),))
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, scan_timestamp, native_language, language_source, converted) "
            "VALUES (?, ?, '2026-10-08T00:00:00', 'ice', 'manual', 1)", (str(old), 12 * 1024 * 1024))
        await db.commit()

    with patch("backend.scanner.probe_file", new_callable=AsyncMock) as probe:
        await FileWatcher(test_db, interval_minutes=5).check_once()

    assert probe.await_count == 0  # not treated as a new file
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT file_path, native_language, language_source, converted FROM scan_results") as cur:
            rows = await cur.fetchall()
    assert rows == [(str(new), "ice", "manual", 1)]
