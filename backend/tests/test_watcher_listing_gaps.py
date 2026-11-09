"""The watcher deleted a row whose file was on disk (v0.10.0). The Banquet
(2006)'s ISO, restored at 18:39:51, was left out of its folder's listing on
the SMB mount at 18:41:35: its row went, under an open Add to Queue, and
came back ten minutes later as a new file. A row whose folder was listed
without its file is kept while the file is there; what the walk leaves out
on purpose still goes. And an ISO is dated by its own modified time — it
took its media folder's, two levels up, as a disc folder's marker does."""
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

ISO_PROBE = {"video_codec": "h264", "audio_tracks": [], "subtitle_tracks": [], "duration": 7850.7,
             "file_size": 34179448832, "disc_type": "bdmv", "video_width": 1920, "video_height": 1080}


def _files(*paths: Path) -> None:
    for p in paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")


async def _seed(db_path, media: Path, rows: list[Path]) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO media_dirs (path, auto_scan) VALUES (?, 1)", (str(media),))
        for p in rows:
            await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) "
                             "VALUES (?, 1, '2026-11-09T18:39:51Z')", (str(p),))
        await db.commit()


async def _rows(db_path) -> list[str]:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path FROM scan_results ORDER BY file_path") as cur:
            return [r[0] for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_a_file_its_folders_listing_left_out_keeps_its_row(test_db, tmp_path, monkeypatch):
    import backend.watcher as watcher_mod
    media = tmp_path / "Movies2"
    iso = media / "The Banquet (2006) [tt0465676]" / "The Banquet (2006) 1080p Remux -52pt.iso"
    gone = media / "Gone (2001)" / "Gone (2001).mkv"
    films = [media / f"Film {n} (2000)" / f"Film {n} (2000).mkv" for n in range(4)]
    _files(iso, gone, *films)
    await _seed(test_db, media, [iso, gone, *films])
    gone.unlink()  # deleted: its folder is listed without it, and it isn't there
    real_walk = watcher_mod.walk_media_dir

    def walk(dir_path, unreadable):  # the listing leaves the ISO out, as the SMB mount's did
        for root, dirs, files in real_walk(dir_path, unreadable):
            yield root, dirs, [f for f in files if f != iso.name]
    monkeypatch.setattr(watcher_mod, "walk_media_dir", walk)

    with patch("backend.scanner.probe_file", new_callable=AsyncMock, return_value=None):
        result = await watcher_mod.FileWatcher(test_db, interval_minutes=5).check_once()

    assert await _rows(test_db) == sorted(str(p) for p in [iso, *films])
    assert result["removed"] == 1


@pytest.mark.asyncio
async def test_what_the_walk_leaves_out_on_purpose_still_goes(test_db, tmp_path):
    """A source whose converted version is beside it, and a backup in
    Shrinkerr's hidden folder: both on disk, neither the walk's to list."""
    from backend.watcher import FileWatcher
    media = tmp_path / "Movies"
    source = media / "Film (2020)" / "Film (2020) 1080p BluRay x264.mkv"
    converted = media / "Film (2020)" / "Film (2020) 1080p BluRay x265.mkv"
    backup = media / "Other (2019)" / ".shrinkerr_backup" / "Other (2019).mkv"
    films = [media / f"Film {n} (2000)" / f"Film {n} (2000).mkv" for n in range(4)]
    _files(source, converted, backup, *films)
    await _seed(test_db, media, [source, converted, backup, *films])

    with patch("backend.scanner.probe_file", new_callable=AsyncMock, return_value=None):
        await FileWatcher(test_db, interval_minutes=5).check_once()

    assert await _rows(test_db) == sorted(str(p) for p in [converted, *films])


def _dated(iso: Path) -> float:
    """The ISO's modified time, its media folder's a day earlier."""
    when = float(int(time.time()) - 3600)  # not in the future: that's clamped to now
    os.utime(iso, (when, when))
    os.utime(iso.parent.parent, (when - 86400, when - 86400))
    return when


@pytest.mark.asyncio
async def test_the_watcher_dates_an_iso_by_its_own_time(test_db, tmp_path, monkeypatch):
    import backend.watcher as watcher_mod
    iso = tmp_path / "Movies2" / "The Banquet (2006) [tt0465676]" / "The Banquet (2006) 1080p Remux -52pt.iso"
    _files(iso)
    when = _dated(iso)
    monkeypatch.setattr(watcher_mod, "api_original_language", AsyncMock(return_value=None))
    scanned = await watcher_mod.scanned_from_probe(str(iso), dict(ISO_PROBE), ["h264"], 23)
    assert scanned.file_mtime == when


@pytest.mark.asyncio
async def test_a_full_scan_dates_an_iso_by_its_own_time(test_db, tmp_path, monkeypatch):
    import backend.config
    import backend.scanner as scanner
    monkeypatch.setattr(scanner, "settings", backend.config.settings)
    iso = tmp_path / "Movies2" / "The Banquet (2006) [tt0465676]" / "The Banquet (2006) 1080p Remux -52pt.iso"
    _files(iso)
    when = _dated(iso)
    monkeypatch.setattr(scanner, "probe_file", AsyncMock(return_value=dict(ISO_PROBE)))
    monkeypatch.setattr(scanner, "api_original_language", AsyncMock(return_value=None))
    [scanned] = await scanner.scan_directory(str(tmp_path / "Movies2"))
    assert scanned.file_mtime == when
