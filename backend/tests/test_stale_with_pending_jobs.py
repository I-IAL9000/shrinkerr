"""Files moved or deleted while they wait in the queue.

The watcher and the Scanner's orphan cleanup kept a vanished file listed
while it had a pending job, as they do for a running one (a conversion
replaces its file). A pending job hasn't touched its file: moving a queued
show to another share left every episode listed twice, and queued, until the
queue reached each job and failed it."""
import os
import time
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from backend.queue import JobQueue


async def _seed(db_path, media, rows):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO media_dirs (path, auto_scan) VALUES (?, 1)", (str(media),))
        for p in rows:
            await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) "
                             "VALUES (?, 0, '2026-10-11T00:00:00Z')", (str(p),))
        await db.commit()


async def _queue(db_path, pending, running=()):
    q = JobQueue(db_path)
    for p in [*pending, *running]:
        await q.add_job(str(p), "convert", encoder="libx265")
    async with aiosqlite.connect(db_path) as db:
        for p in running:
            await db.execute("UPDATE jobs SET status = 'running' WHERE file_path = ?", (str(p),))
        await db.commit()


async def _state(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path FROM scan_results ORDER BY file_path") as cur:
            rows = [r[0] for r in await cur.fetchall()]
        async with db.execute("SELECT file_path, status FROM jobs ORDER BY file_path") as cur:
            jobs = [tuple(r) for r in await cur.fetchall()]
    return rows, jobs


@pytest.mark.asyncio
async def test_the_watcher_drops_a_queued_file_that_was_moved_away(test_db, tmp_path):
    from backend.watcher import FileWatcher
    media = tmp_path / "TV1"
    other = media / "Other Show (2001)"
    other.mkdir(parents=True)
    kept = [other / f"Other Show - S01E0{i}.mkv" for i in range(1, 6)]
    for p in kept:
        p.write_bytes(b"x")
    moved = media / "Top Chef (2006)" / "Season 5"  # moved to another share: gone
    queued = [moved / f"Top Chef - S05E0{i}.mkv" for i in range(1, 4)]
    converting = moved / "Top Chef - S05E04.mkv"  # a running job replaces its file: keep
    await _seed(test_db, media, [*kept, *queued, converting])
    await _queue(test_db, queued, running=[converting])

    with patch("backend.scanner.probe_file", new_callable=AsyncMock):
        await FileWatcher(test_db, interval_minutes=5).check_once()

    rows, jobs = await _state(test_db)
    assert rows == sorted([*map(str, kept), str(converting)])
    assert jobs == [(str(converting), "running")]  # the pending jobs went with their files


@pytest.mark.asyncio
async def test_a_moved_file_takes_its_pending_job_along(test_db, tmp_path):
    from backend.watcher import FileWatcher
    media = tmp_path / "Movies"
    old = media / "Film (2009)" / "Film (2009) 1080p WEB h264.mkv"
    new = media / "Film (2009) [tt1]" / "Film (2009) 1080p WEB h264.mkv"
    new.parent.mkdir(parents=True)
    with open(new, "wb") as f:
        f.truncate(12 * 1024 * 1024)  # sparse: a real size without the disk use
    stamp = time.time() - 3600
    os.utime(new, (stamp, stamp))
    await _seed(test_db, media, [])
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) "
                         "VALUES (?, ?, '2026-10-11T00:00:00Z')", (str(old), 12 * 1024 * 1024))
        await db.commit()
    await _queue(test_db, [old])

    with patch("backend.scanner.probe_file", new_callable=AsyncMock) as probe:
        probe.return_value = None  # a new file instead of a move: not registered
        await FileWatcher(test_db, interval_minutes=5).check_once()

    assert await _state(test_db) == ([str(new)], [(str(new), "pending")])


def test_a_rescan_drops_a_queued_file_that_was_moved_away(test_db, tmp_path):
    import asyncio
    from backend.routes.scan import _scan_worker_process
    media = tmp_path / "TV1"
    season = media / "Top Chef (2006)" / "Season 5"
    season.mkdir(parents=True)  # emptied: the episodes were moved to another share
    queued = [season / f"Top Chef - S05E0{i}.mkv" for i in range(1, 3)]
    converting = season / "Top Chef - S05E03.mkv"
    asyncio.run(_seed(test_db, media, [*queued, converting]))
    asyncio.run(_queue(test_db, queued, running=[converting]))

    _scan_worker_process([str(season)], test_db, str(tmp_path / "progress.json"), str(tmp_path / "cancel"))

    assert asyncio.run(_state(test_db)) == ([str(converting)], [(str(converting), "running")])


async def _approve(db_path, path):
    """An approved review: a pending job whose output waits in .shrinkerr-review/."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE jobs SET review_approved = 1 WHERE file_path = ?", (str(path),))
        await db.commit()


@pytest.mark.asyncio
async def test_an_empty_mountpoint_keeps_its_rows_and_queue(test_db, tmp_path):
    """A share that isn't mounted yet (Docker started first) leaves an empty
    folder that walks clean: its rows and queued jobs stay. A smaller share
    than half the library slipped past the watcher's other checks, and with
    pending jobs no longer keeping rows, the whole share's queue went."""
    from backend.watcher import FileWatcher
    tv = tmp_path / "TV"
    show = tv / "Show (2001)"
    show.mkdir(parents=True)
    episodes = [show / f"Show - S01E{i:02}.mkv" for i in range(1, 11)]
    for p in episodes:
        p.write_bytes(b"x")
    movies = tmp_path / "Movies"
    movies.mkdir()  # the mountpoint, without the share
    films = [movies / f"Film {i} (2010)" / f"Film {i} (2010).mkv" for i in range(1, 4)]
    await _seed(test_db, tv, episodes)
    await _seed(test_db, movies, films)
    await _queue(test_db, films[:2])

    with patch("backend.scanner.probe_file", new_callable=AsyncMock):
        await FileWatcher(test_db, interval_minutes=5).check_once()

    rows, jobs = await _state(test_db)
    assert rows == sorted(map(str, [*episodes, *films]))
    assert jobs == [(str(f), "pending") for f in films[:2]]


@pytest.mark.asyncio
async def test_the_watcher_keeps_an_approved_review(test_db, tmp_path):
    """Its encoded output waits in .shrinkerr-review/: the job puts it in
    place, or deletes it when the original is gone. Dropped, the output
    stayed in the hidden folder (Clear pending keeps these too)."""
    from backend.watcher import FileWatcher
    media = tmp_path / "TV1"
    other = media / "Other Show (2001)"
    other.mkdir(parents=True)
    kept = [other / f"Other Show - S01E0{i}.mkv" for i in range(1, 6)]
    for p in kept:
        p.write_bytes(b"x")
    gone = media / "Top Chef (2006)" / "Season 5"
    queued, approved = gone / "Top Chef - S05E01.mkv", gone / "Top Chef - S05E02.mkv"
    await _seed(test_db, media, [*kept, queued, approved])
    await _queue(test_db, [queued, approved])
    await _approve(test_db, approved)

    with patch("backend.scanner.probe_file", new_callable=AsyncMock):
        await FileWatcher(test_db, interval_minutes=5).check_once()

    assert (await _state(test_db))[1] == [(str(approved), "pending")]


def test_a_rescan_keeps_an_approved_review(test_db, tmp_path):
    import asyncio
    from backend.routes.scan import _scan_worker_process
    media = tmp_path / "TV1"
    season = media / "Top Chef (2006)" / "Season 5"
    season.mkdir(parents=True)
    queued, approved = season / "Top Chef - S05E01.mkv", season / "Top Chef - S05E02.mkv"
    asyncio.run(_seed(test_db, media, [queued, approved]))
    asyncio.run(_queue(test_db, [queued, approved]))
    asyncio.run(_approve(test_db, approved))

    _scan_worker_process([str(season)], test_db, str(tmp_path / "progress.json"), str(tmp_path / "cancel"))

    assert asyncio.run(_state(test_db))[1] == [(str(approved), "pending")]
