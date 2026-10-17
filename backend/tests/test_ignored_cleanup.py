"""Ignored files get their audio and subtitle clean-up, never a conversion
(v0.10.0) — as an "ignore" rule does. Add to Queue converted them (its
override was on for them) and the watcher left new files in ignored
folders out entirely. Override in Add to Queue still converts them."""
import json

import aiosqlite
import pytest
import pytest_asyncio

WORK = json.dumps([{"stream_index": 1, "language": "eng", "codec": "aac", "channels": 2, "keep": True},
                   {"stream_index": 2, "language": "ger", "codec": "aac", "channels": 2, "keep": False}])
NO_WORK = json.dumps([{"stream_index": 1, "language": "eng", "codec": "aac", "channels": 2, "keep": True}])
FILES = {  # all need converting
    "/m/Ignored (2001)/i.mkv": WORK,
    "/m/Nothing To Clean (2002)/n.mkv": NO_WORK,
    "/m/In Ignored Folder (2003)/f.mkv": WORK,
    "/m/Plain (2004)/p.mkv": WORK,
}


@pytest_asyncio.fixture
async def library(test_db, monkeypatch):
    import sys
    from backend.queue import JobQueue, QueueWorker
    from backend.routes.jobs import init_job_routes
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    worker = QueueWorker(test_db)
    monkeypatch.setattr(worker, "start", lambda *a, **kw: None)
    init_job_routes(worker, JobQueue(test_db))
    async with aiosqlite.connect(test_db) as db:
        for path, audio in FILES.items():
            await db.execute("INSERT INTO scan_results (file_path, file_size, needs_conversion, video_codec, "
                             "audio_tracks_json, subtitle_tracks_json, has_removable_tracks_flag, scan_timestamp) "
                             "VALUES (?, 1, 1, 'h264', ?, '[]', ?, 't')", (path, audio, int(audio == WORK)))
        await db.executemany("INSERT INTO ignored_files (file_path, reason, ignored_at) VALUES (?, 'manual', 't')",
                             [("/m/Ignored (2001)/i.mkv",), ("/m/Nothing To Clean (2002)/n.mkv",),
                              ("/m/In Ignored Folder (2003)/",)])
        await db.commit()
    return test_db


async def _queue(db_path, **request):
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM jobs")
        await db.commit()
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=list(FILES), **request))
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path, job_type FROM jobs") as cur:
            return dict(await cur.fetchall())


@pytest.mark.asyncio
async def test_ignored_files_get_only_their_cleanup(library):
    assert await _queue(library) == {"/m/Ignored (2001)/i.mkv": "audio", "/m/In Ignored Folder (2003)/f.mkv": "audio",
                                     "/m/Plain (2004)/p.mkv": "combined"}


@pytest.mark.asyncio
async def test_override_converts_them(library):
    assert await _queue(library, override_rules=True) == {
        "/m/Ignored (2001)/i.mkv": "combined", "/m/Nothing To Clean (2002)/n.mkv": "convert",
        "/m/In Ignored Folder (2003)/f.mkv": "combined", "/m/Plain (2004)/p.mkv": "combined"}


@pytest.mark.asyncio
async def test_the_estimate_says_the_same(library):
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    plain = await estimate_jobs(EstimateRequest(file_paths=list(FILES)))
    assert (plain["by_type"], plain["ignored_files"]) == ({"convert": 0, "audio": 2, "combined": 1}, 3)
    override = await estimate_jobs(EstimateRequest(file_paths=list(FILES), override_rules=True))
    assert override["by_type"] == {"convert": 1, "audio": 0, "combined": 3}


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="needs ffmpeg")
@pytest.mark.asyncio
async def test_new_files_in_an_ignored_folder_are_cleaned_up_automatically(test_db, tmp_path, monkeypatch):
    """The watcher left them out of auto-queue entirely; they still aren't
    counted as new."""
    import sys
    from backend.tests.test_same_name_conversion import _mkv
    from backend.watcher import FileWatcher
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    folder = tmp_path / "Movies" / "Kept As Is (2001)"
    folder.mkdir(parents=True)
    film = folder / "Kept As Is (2001).mkv"
    _mkv(film, ["eng", "ger"])
    async with aiosqlite.connect(test_db) as db:
        for key, value in (("auto_queue_new", "true"), ("always_keep_languages", '["eng"]')):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.execute("INSERT INTO ignored_files (file_path, reason, ignored_at) VALUES (?, 'manual', 't')",
                         (f"{folder}/",))
        await db.commit()

    added, _ = await FileWatcher(test_db)._scan_new_files([str(film)], [f"{folder}/"])

    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT file_path, job_type FROM jobs") as cur:
            assert dict(await cur.fetchall()) == {str(film): "audio"}
    assert added == []
