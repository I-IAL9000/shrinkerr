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
YEAR = 365 * 24 * HOUR


@pytest.fixture(autouse=True)
def natives(test_db, monkeypatch):
    """The original-language lookup without the network: path → language."""
    import sys
    import backend.metadata as metadata
    from backend.media_paths import invalidate_media_dir_cache
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    invalidate_media_dir_cache()
    found: dict[str, str] = {}

    async def lookup(path):
        return found.get(path)
    monkeypatch.setattr(metadata, "lookup_original_language", lookup)
    return found


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


# "Mag Wheels (1978)" and "Xtro 2 (1990)": broken for years, listed by the
# watcher now, and shown under Added: New and Not API matched (12 Oct).

async def _is_new(db_path, path):
    """Listed by the Scanner's Added: New filter."""
    from backend.scan_filters import FILTERS
    new = FILTERS["new"]
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(f"SELECT 1 FROM scan_results WHERE file_path = ? AND {new.sql}",
                              (path, *new.params())) as cur:
            return await cur.fetchone() is not None


async def _list_as_corrupt(w, path):
    await _cycle(w)
    _cooldown_passes(w, path)
    await _cycle(w)
    assert (await _row(w.db_path, path))[0] == "corrupt"


@pytest.mark.asyncio
async def test_an_old_file_listed_as_corrupt_isnt_new(test_db, tmp_path):
    broken = await _library(test_db, tmp_path, age=40 * YEAR)
    await _list_as_corrupt(FileWatcher(test_db, interval_minutes=5), broken)
    assert not await _is_new(test_db, broken)


@pytest.mark.asyncio
async def test_a_recent_file_listed_as_corrupt_is_new(test_db, tmp_path):
    broken = await _library(test_db, tmp_path, age=2 * HOUR)
    await _list_as_corrupt(FileWatcher(test_db, interval_minutes=5), broken)
    assert await _is_new(test_db, broken)


@pytest.mark.asyncio
async def test_a_future_dated_file_listed_as_corrupt_isnt_new(test_db, tmp_path):
    """A rip dated 2036 is old, as it is to the hour-unchanged check, and its
    date is stored as now, as a readable file's is (it pinned Newest)."""
    broken = await _library(test_db, tmp_path, age=-10 * YEAR)
    before = time.time()
    await _list_as_corrupt(FileWatcher(test_db, interval_minutes=5), broken)
    assert not await _is_new(test_db, broken)
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT file_mtime FROM scan_results WHERE file_path = ?", (broken,)) as cur:
            (mtime,) = await cur.fetchone()
    assert before <= mtime <= time.time()


async def _language(db_path, path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT native_language, language_source FROM scan_results WHERE file_path = ?",
                              (path,)) as cur:
            return await cur.fetchone()


@pytest.mark.asyncio
async def test_the_watcher_looks_up_a_corrupt_files_original_language(test_db, tmp_path, natives):
    broken = await _library(test_db, tmp_path, age=40 * YEAR)
    natives[broken] = "eng"
    await _list_as_corrupt(FileWatcher(test_db, interval_minutes=5), broken)
    assert await _language(test_db, broken) == ("eng", "api")


@pytest.mark.asyncio
async def test_a_full_scan_looks_up_a_corrupt_files_original_language(test_db, tmp_path, natives):
    from backend.scanner import scan_directory
    folder = tmp_path / "media" / "Mag Wheels (1978)"
    folder.mkdir(parents=True)
    path = folder / "Mag Wheels (1978).avi"
    path.write_bytes(b"not a video" * 100)
    natives[str(path)] = "eng"
    with patch("backend.scanner.probe_file", new_callable=AsyncMock, return_value=None):
        (row,) = await scan_directory(str(tmp_path / "media"))
    assert (row.probe_status, row.native_language, row.language_source) == ("corrupt", "eng", "api")


@pytest.mark.asyncio
async def test_a_corrupt_file_in_an_other_folder_isnt_looked_up(test_db, tmp_path, natives):
    """Home videos and the like match spuriously, as for readable files."""
    from backend.scanner import scan_directory
    folder = tmp_path / "media" / "Holiday (1999)"
    folder.mkdir(parents=True)
    path = folder / "Holiday (1999).avi"
    path.write_bytes(b"not a video" * 100)
    natives[str(path)] = "eng"
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO media_dirs (path, label) VALUES (?, 'Other')", (str(tmp_path / "media"),))
        await db.commit()
    with patch("backend.scanner.probe_file", new_callable=AsyncMock, return_value=None):
        (row,) = await scan_directory(str(tmp_path / "media"))
    assert (row.native_language, row.language_source) == ("und", "heuristic")


@pytest.mark.asyncio
async def test_the_follow_up_lookup_gets_the_files_that_were_added(test_db, tmp_path):
    """It got the first N of the files found, N being how many were added:
    files that didn't read (or were skipped) took the places of ones added."""
    media = tmp_path / "Movies"
    known = media / "Known (2020)" / "Known (2020).mkv"
    readable = [media / f"Good {i} (2001)" / f"Good {i} (2001).mkv" for i in range(2)]
    unreadable = [media / f"Bad {i} (2001)" / f"Bad {i} (2001).mkv" for i in range(6)]
    for p in (known, *readable, *unreadable):
        p.parent.mkdir(parents=True)
        p.write_bytes(b"x" * 1000)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO media_dirs (path, auto_scan) VALUES (?, 1)", (str(media),))
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) "
                         "VALUES (?, 1, '2026-10-12T00:00:00')", (str(known),))
        await db.commit()
    probe = {"video_codec": "h264", "video_width": 720, "video_height": 480, "audio_tracks": [],
             "subtitle_tracks": [], "duration": 60, "file_size": 1000}
    readable_paths = {str(p) for p in readable}

    async def probe_file(path, *a, **kw):
        return probe if path in readable_paths else None
    w = FileWatcher(test_db, interval_minutes=5)
    with patch("backend.scanner.probe_file", new=probe_file), \
            patch.object(w, "_refresh_metadata_for_files", new_callable=AsyncMock) as refresh:
        await w.check_once()
    (paths,), _ = refresh.call_args
    assert sorted(paths) == sorted(readable_paths)
