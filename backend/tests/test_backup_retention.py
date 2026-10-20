"""v0.10.0: "keep originals for N days" backups.

H3: shutil.move keeps the source's mtime and expiry compared mtimes, so a
backup of a file downloaded long ago was deleted by the next sweep, minutes
after it was made.
Disc backups are folders (BDMV/, VIDEO_TS/, release folders), and the sweep
only deleted files: they never expired and the space was never freed.
"""
import os
import shutil
import subprocess
import time

import aiosqlite
import pytest

import backend.queue as queue_mod
from backend.converter import _dispose_disc_source

OLD = time.time() - 400 * 86400


def _age(path, when=OLD):
    os.utime(path, (when, when))


@pytest.mark.asyncio
async def test_disc_backup_is_dated_when_made(tmp_path):
    title = tmp_path / "Movie (2009)"
    (title / "BDMV").mkdir(parents=True)
    (title / "BDMV" / "index.bdmv").write_bytes(b"INDX0200")
    _age(title / "BDMV")
    backup = await _dispose_disc_source(title / "BDMV" / "index.bdmv", title, "bdmv", 7, False, "")
    assert time.time() - os.stat(backup).st_mtime < 60


def _has_libx265() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "libx265" in out


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_libx265(), reason="needs ffmpeg with libx265")
async def test_file_backup_is_dated_when_made(test_db, tmp_path):
    from backend.converter import convert_file
    src = tmp_path / "Show - S01E01 - 1080p WEB h264.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=2",
                    "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)
    _age(src)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('backup_original_days', '7')")
        await db.commit()
    result = await convert_file(str(src), "libx265", 2.0, override_libx265_preset="ultrafast")
    assert result["success"], result.get("error")
    assert time.time() - os.stat(result["backup_path"]).st_mtime < 60


@pytest.mark.asyncio
async def test_sweep_expires_folders_as_well_as_files(test_db, tmp_path, monkeypatch):
    import backend.database as database
    monkeypatch.setattr(database, "DB_PATH", test_db)
    monkeypatch.setattr(queue_mod, "_last_backup_cleanup", 0)
    media = tmp_path / "media"
    backups = media / "Movie (2009)" / ".shrinkerr_backup"
    (backups / "BDMV" / "STREAM").mkdir(parents=True)
    (backups / "BDMV" / "STREAM" / "00000.m2ts").write_bytes(b"x")
    (backups / "old.mkv").write_bytes(b"x")
    (backups / "new.mkv").write_bytes(b"x")
    _age(backups / "BDMV")
    _age(backups / "old.mkv")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('backup_original_days', '7')")
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('disc_backups_redated', '1')")
        await db.execute("INSERT INTO media_dirs (path) VALUES (?)", (str(media),))
        await db.commit()

    await queue_mod._cleanup_expired_backups()

    assert sorted(p.name for p in backups.iterdir()) == ["new.mkv"]


@pytest.mark.asyncio
async def test_delete_backups_by_age_works(test_db, tmp_path, monkeypatch):
    """F8: "Delete backups" (all / by age) called list_backups(), which a later
    route function of the same name had replaced, and always failed."""
    pytest.importorskip("apscheduler")
    import backend.routes.settings as settings_route
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    media = tmp_path / "media"
    backups = media / "Movie (2009)" / ".shrinkerr_backup"
    backups.mkdir(parents=True)
    (backups / "old.mkv").write_bytes(b"x")
    (backups / "new.mkv").write_bytes(b"x")
    _age(backups / "old.mkv")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO media_dirs (path) VALUES (?)", (str(media),))
        await db.commit()

    res = await settings_route.delete_backups(settings_route.DeleteBackupsRequest(older_than_days=7))

    assert res["deleted"] == 1
    assert sorted(p.name for p in backups.iterdir()) == ["new.mkv"]


@pytest.mark.asyncio
async def test_expiry_runs_hourly_in_the_background(monkeypatch):
    """Not after each job, where its walk of every media folder held the job
    up and loaded the share (approving 61 reviews took 234 s); an error
    doesn't stop it."""
    import asyncio
    runs = []

    async def sweep():
        runs.append(1)
        if len(runs) == 1:
            raise OSError("share not mounted")
    async def nothing_held():
        return 0
    monkeypatch.setattr(queue_mod, "_cleanup_expired_backups", sweep)
    # Not the real one: it opened a database connection (whichever database an
    # earlier test left), and cancelled while opening it, the connection was
    # never closed — the test run then couldn't exit.
    monkeypatch.setattr(queue_mod, "expire_held_larger", nothing_held)
    task = asyncio.create_task(queue_mod.backup_expiry_loop(first_delay=0, every=0.01))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(runs) >= 3


@pytest.mark.asyncio
async def test_listing_backups_doesnt_freeze_the_app(test_db, tmp_path, monkeypatch):
    """Delete all backups lists them first, walking every media folder on the
    share: minutes on a big library, with the whole app frozen meanwhile
    (the walk ran on the event loop). It runs off it now, and deleting what
    was listed doesn't walk the library again (it walked it twice more)."""
    import asyncio
    pytest.importorskip("apscheduler")
    import backend.routes.settings as settings_route
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    media = tmp_path / "media"
    backups = media / "Movie (2009)" / ".shrinkerr_backup"
    backups.mkdir(parents=True)
    (backups / "a.mkv").write_bytes(b"xx")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO media_dirs (path) VALUES (?)", (str(media),))
        await db.commit()
    real_walk, walks = os.walk, []

    def slow_walk(top, *a, **kw):
        walks.append(top)
        time.sleep(0.3)  # a big share
        yield from real_walk(top, *a, **kw)
    monkeypatch.setattr(os, "walk", slow_walk)

    ticks = []

    async def ticker():
        for _ in range(5):
            await asyncio.sleep(0.02)
            ticks.append(1)
    tick = asyncio.create_task(ticker())
    listed = await settings_route.list_backups()
    assert len(ticks) >= 3, "the app was frozen while backups were listed"
    await tick
    assert [b["name"] for b in listed["backups"]] == ["a.mkv"] and listed["total_size"] == 2

    walks.clear()
    res = await settings_route.delete_backups(settings_route.DeleteBackupsRequest(paths=[b["path"] for b in listed["backups"]]))
    assert (res["deleted"], res["freed"], walks) == (1, 2, [])
    assert not (backups / "a.mkv").exists()
