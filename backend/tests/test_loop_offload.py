"""Chapter 4 (0.11 "fast & steady"): slow NAS work must not run on the event
loop — one stalled mount used to freeze the whole app (requests, live
progress, and the conversion's database writes: "database is locked")."""
import asyncio
import time

import pytest


async def _max_loop_gap(coro, tick=0.01):
    """Run `coro` while measuring the longest the event loop went without
    running a 10 ms ticker; returns (result, longest gap in seconds)."""
    gaps = []
    stop = False

    async def ticker():
        last = time.monotonic()
        while not stop:
            await asyncio.sleep(tick)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    t = asyncio.create_task(ticker())
    try:
        result = await coro
    finally:
        stop = True
        await t
    return result, max(gaps or [0])


@pytest.mark.asyncio
async def test_a_stalled_mount_does_not_freeze_the_dashboard(monkeypatch, tmp_path):
    """F4: the dashboard's disk-space card stat()ed every media folder on the
    event loop, every 10 s."""
    import backend.routes.stats as stats
    stalled = {"calls": 0}

    def slow_disk_usage(path):
        stalled["calls"] += 1
        time.sleep(1.0)  # a NAS mount that hangs
        raise OSError("stalled")
    monkeypatch.setattr(stats.shutil, "disk_usage", slow_disk_usage)
    monkeypatch.setattr(stats, "_DISK_WAIT", 0.2)
    monkeypatch.setattr(stats, "_disk_cache", {"at": 0.0, "dirs": None, "info": []})
    monkeypatch.setattr(stats, "_disk_task", None)

    info, gap = await _max_loop_gap(stats._disk_info([str(tmp_path)]))
    assert info == [] and gap < 0.15            # answered after 0.2 s; the loop kept running
    # A second poll while the first is still stuck doesn't start another.
    await stats._disk_info([str(tmp_path)])
    assert stalled["calls"] == 1
    await asyncio.sleep(1.0)  # let the stuck thread finish before teardown


@pytest.mark.asyncio
async def test_disk_space_is_cached_between_polls(monkeypatch, tmp_path):
    import backend.routes.stats as stats
    calls = []
    real = stats.shutil.disk_usage
    monkeypatch.setattr(stats.shutil, "disk_usage", lambda p: calls.append(p) or real(p))
    monkeypatch.setattr(stats, "_disk_cache", {"at": 0.0, "dirs": None, "info": []})
    monkeypatch.setattr(stats, "_disk_task", None)
    first = await stats._disk_info([str(tmp_path)])
    second = await stats._disk_info([str(tmp_path)])
    assert first == second and len(first) == 1 and len(calls) == 1


@pytest.mark.asyncio
async def test_disc_languages_are_read_off_the_loop_and_cached(tmp_path, monkeypatch):
    """SC-07: a disc's IFO / mpls / ISO language parse (pycdlib, bsdtar,
    libbluray) ran on the event loop at every disc probe."""
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    import backend.disc_metadata as disc_metadata
    import backend.scanner as scanner
    from backend.tests.test_disc_release_folder import _dvd
    marker = _dvd(tmp_path / "Film (1999)")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=720x480:rate=25:duration=2",
                    "-f", "lavfi", "-i", "sine=duration=2", "-c:v", "mpeg2video", "-c:a", "ac3",
                    "-f", "vob", str(marker.parent / "VTS_01_1.VOB")], check=True)
    calls = []

    def slow_parse(folder, kind):
        calls.append(folder)
        time.sleep(0.5)  # hundreds of playlists over a NAS
        return {"audio": ["eng"], "subtitle": []}
    monkeypatch.setattr(disc_metadata, "parse_disc_languages", slow_parse)
    monkeypatch.setattr(scanner, "_DISC_LANG_CACHE", type(scanner._DISC_LANG_CACHE)())

    probe, gap = await _max_loop_gap(scanner.probe_file(str(marker)))
    assert probe["audio_tracks"][0]["language"] == "eng"
    assert gap < 0.3                    # the 0.5 s parse didn't hold the loop
    await scanner.probe_file(str(marker))
    assert len(calls) == 1              # the second probe of the same disc is cached


@pytest.mark.asyncio
async def test_poster_downloads_dont_hold_the_write_lock(test_db, monkeypatch):
    """SC-25: the poster image backfill wrote each UPDATE between downloads
    (15 s timeouts), holding the database write lock across the network —
    a running conversion's progress write then failed "database is locked"."""
    import sqlite3
    import aiosqlite
    import backend.routes.posters as posters
    monkeypatch.setattr(posters, "DB_PATH", test_db)
    paths = [f"/m/movies/Film {i} (200{i})" for i in range(4)]
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')", (posters._V116_PURGE_FLAG,))
        for p in paths:
            await db.execute(
                "INSERT INTO poster_cache (folder_path, title, year, poster_url, source, media_type, resolved_at) "
                "VALUES (?, 'Film', '2001', 'https://image.tmdb.org/t/p/w300/x.jpg', 'tmdb', 'movie', '2026-01-01')", (p,))
        await db.commit()
    blocked = []

    async def fake_download(url, plex_url="", plex_token=""):
        await asyncio.sleep(0.05)
        # Meanwhile, a conversion writes its progress (no waiting on a lock).
        con = sqlite3.connect(test_db, timeout=0)
        try:
            con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('progress_probe', '1')")
            con.commit()
        except sqlite3.OperationalError as exc:
            blocked.append(str(exc))
        finally:
            con.close()
        return "aW1n"
    monkeypatch.setattr(posters, "_download_image", fake_download)
    result = await posters.resolve_posters(posters.ResolveRequest(paths=paths))
    assert blocked == []
    assert all(result[p]["poster_url"] == "data:image/jpeg;base64,aW1n" for p in paths)


@pytest.mark.asyncio
async def test_media_folder_labels_come_from_one_cached_index(test_db, tmp_path, monkeypatch):
    """SC-25 / SC-26: the folder-type check opened a connection and resolved
    paths on the NAS for every file."""
    import aiosqlite
    import backend.media_paths as media_paths
    monkeypatch.setattr(media_paths, "DB_PATH", test_db)
    media_paths.invalidate_media_dir_cache()
    real = tmp_path / "real"
    (real / "Other").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO media_dirs (path, label) VALUES (?, 'Movies')", (str(link),))
        await db.execute("INSERT INTO media_dirs (path, label) VALUES (?, 'Other')", (str(link / "Other"),))
        await db.commit()
    connects = []
    real_connect = media_paths.aiosqlite.connect
    monkeypatch.setattr(media_paths.aiosqlite, "connect", lambda *a, **kw: connects.append(1) or real_connect(*a, **kw))
    assert await media_paths.media_dir_label_for(f"{link}/Film (2001)/film.mkv") == "Movies"
    assert await media_paths.is_other_typed_dir(f"{link}/Other/home video.mkv") is True     # deepest wins
    assert await media_paths.media_dir_label_for(f"{real}/Film (2001)/film.mkv") == "Movies"  # resolved form
    assert await media_paths.media_dir_label_for("/elsewhere/film.mkv") is None
    assert len(connects) == 1  # one load for all four lookups
    media_paths.invalidate_media_dir_cache()
    await media_paths.media_dir_label_for(f"{link}/x.mkv")
    assert len(connects) == 2
