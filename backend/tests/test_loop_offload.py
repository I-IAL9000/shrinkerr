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
    await asyncio.sleep(0)  # let the ticker start before the work does
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
    import backend.media_paths as media_paths
    import backend.routes.posters as posters
    monkeypatch.setattr(posters, "DB_PATH", test_db)
    monkeypatch.setattr(media_paths, "DB_PATH", test_db)
    media_paths.invalidate_media_dir_cache()
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


def test_plex_sections_match_on_a_folder_boundary():
    """SC-26: the Plex section lookup resolve()d the file and every library
    path on the event loop; it also matched "/media/Movies2" to a
    "/media/Movies" library."""
    from backend.plex import find_section_for_path
    libs = [{"id": "1", "paths": ["/media/Movies"]}, {"id": "2", "paths": ["/media/Movies2/"]},
            {"id": "3", "paths": ["/media"]}]
    assert find_section_for_path("/media/Movies/Film (2001)/film.mkv", libs) == ("1", "/media/Movies")
    assert find_section_for_path("/media/Movies2/Film/film.mkv", libs) == ("2", "/media/Movies2")
    assert find_section_for_path("/media/TV/x.mkv", libs) == ("3", "/media")
    assert find_section_for_path("/elsewhere/x.mkv", libs) is None
    # No "/media/Movies2" library: its files belong to "/media", not "/media/Movies".
    assert find_section_for_path("/media/Movies2/x.mkv", [libs[0], libs[2]]) == ("3", "/media")


@pytest.mark.asyncio
async def test_a_slow_rename_does_not_freeze_the_app(monkeypatch, tmp_path):
    import backend.rename as rename

    def slow_rename(src, dst):
        time.sleep(0.3)  # an SMB share taking its time
    monkeypatch.setattr(rename, "_rename_no_overwrite", slow_rename)
    plan = rename.RenamePlan(old_path=str(tmp_path / "a.mkv"), new_path=str(tmp_path / "b.mkv"))
    result, gap = await _max_loop_gap(rename.apply_plan(plan))
    assert result["applied"] and gap < 0.2


@pytest.mark.asyncio
async def test_the_subtitle_reconcile_runs_off_the_loop(test_db, monkeypatch, tmp_path):
    """SC-26: the watcher's external-subtitle reconcile is pure CPU (1.8 s
    per cycle for 30k subbed videos) and ran on the event loop."""
    import aiosqlite
    import backend.scanner as scanner
    from backend.watcher import FileWatcher
    folder = tmp_path / "Film (2001)"
    folder.mkdir()
    video = str(folder / "film.mkv")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, native_language, subtitle_tracks_json, scan_timestamp) "
                         "VALUES (?, 1, 'eng', '[]', '2026-01-01')", (video,))
        await db.commit()
    real = scanner.detect_external_subtitles

    def slow_detect(path, siblings=None):
        time.sleep(0.3)
        return real(path, siblings=siblings)
    monkeypatch.setattr(scanner, "detect_external_subtitles", slow_detect)
    siblings = [folder / "film.mkv", folder / "film.eng.srt"]
    (folder / "film.eng.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
    updated, gap = await _max_loop_gap(
        FileWatcher(test_db)._reconcile_external_subs({str(folder): siblings}, {video}))
    assert updated == 1 and gap < 0.2


@pytest.mark.asyncio
async def test_the_worker_reads_settings_once_per_pass(test_db, monkeypatch):
    """M8: each worker-loop iteration opened a database connection per
    setting (parallel jobs, quiet hours, stream pauses, nice, ...)."""
    import aiosqlite
    import backend.queue as queue_mod
    from backend.queue import QueueWorker
    async with aiosqlite.connect(test_db) as db:
        for key, value in (("parallel_jobs", "3"), ("quiet_hours_parallel", "1"),
                           ("quiet_hours_enabled", "false")):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()
    w = QueueWorker(test_db)
    connects = []
    real = queue_mod.aiosqlite.connect
    monkeypatch.setattr(queue_mod.aiosqlite, "connect", lambda *a, **kw: connects.append(1) or real(*a, **kw))
    assert await w._get_parallel_limit() == 3
    assert await w._is_quiet_hours() is False
    assert await w._get_quiet_hours_parallel() == 1
    assert await w._should_use_nice() is False
    assert await w._should_pause_for_plex() is False
    assert await w._should_pause_for_jellyfin() is False
    assert len(connects) == 1
    async with aiosqlite.connect(test_db) as db:  # a change in Settings...
        await db.execute("UPDATE settings SET value = '5' WHERE key = 'parallel_jobs'")
        await db.commit()
    w._settings_cache_at -= w._SETTINGS_TTL  # ...reaches the worker within a few seconds
    assert await w._get_parallel_limit() == 5
