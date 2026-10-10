"""Sonarr / Radarr Connect → Webhook (v0.10.0): each imported file is queued
as Add to Queue would queue it — the path for torrent users, who have no
NZBGet / SABnzbd script."""
import shutil
import subprocess

import aiosqlite
import pytest

from backend.routes.webhooks import arr_import_paths, webhook_arr

SONARR = {"eventType": "Download", "isUpgrade": False,
          "series": {"title": "Show", "path": "/tv/Show"},
          "episodes": [{"seasonNumber": 1, "episodeNumber": 1}],
          "episodeFile": {"relativePath": "Season 01/Show - S01E01.mkv", "path": "/tv/Show/Season 01/Show - S01E01.mkv"}}
RADARR = {"eventType": "Download", "movie": {"title": "Film", "folderPath": "/movies/Film (2020)"},
          "movieFile": {"relativePath": "Film (2020).mkv"}}


def test_the_files_an_import_carries():
    assert arr_import_paths(SONARR) == ("sonarr", ["/tv/Show/Season 01/Show - S01E01.mkv"])
    assert arr_import_paths(RADARR) == ("radarr", ["/movies/Film (2020)/Film (2020).mkv"])  # from relativePath
    pack = {**SONARR, "episodeFiles": [SONARR["episodeFile"], {"path": "/tv/Show/Season 01/Show - S01E02.mkv"}]}
    assert arr_import_paths(pack)[1] == ["/tv/Show/Season 01/Show - S01E01.mkv", "/tv/Show/Season 01/Show - S01E02.mkv"]
    assert arr_import_paths({"eventType": "Download"}) == (None, [])


@pytest.fixture
def queued(test_db, monkeypatch):
    import backend.routes.jobs as jobs
    calls = []

    async def queue_files_by_path(paths, payload, **kw):
        calls.append((paths, kw["source"]))
        return len(paths), []
    monkeypatch.setattr(jobs, "queue_files_by_path", queue_files_by_path)
    monkeypatch.setattr(jobs, "_queue", object())
    return calls


@pytest.mark.asyncio
async def test_imports_are_queued_through_the_path_mapping(test_db, queued):
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO settings (key, value) VALUES ('sonarr_path_mapping', '/media/TV=/tv')")
        await db.commit()
    assert await webhook_arr({"eventType": "Test"}) == {"status": "ok"}
    assert (await webhook_arr({**SONARR, "eventType": "Grab"}))["status"] == "ignored"
    got = await webhook_arr(SONARR)
    assert (got["status"], got["service"], got["added"]) == ("queued", "sonarr", 1)
    got = await webhook_arr(RADARR)  # no Radarr mapping: its own path
    assert queued == [(["/media/TV/Show/Season 01/Show - S01E01.mkv"], "SONARR"),
                      (["/movies/Film (2020)/Film (2020).mkv"], "RADARR")]


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
async def test_a_real_import(test_db, tmp_path, monkeypatch):
    """Stored in the Scanner and queued; a file outside the media folders isn't."""
    import sys
    import backend.config
    import backend.routes.jobs as jobs
    import backend.scanner as scanner
    from backend.queue import JobQueue, QueueWorker
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    monkeypatch.setattr(scanner, "settings", backend.config.settings)
    worker = QueueWorker(test_db)
    monkeypatch.setattr(worker, "start", lambda *a, **kw: None)
    jobs.init_job_routes(worker, JobQueue(test_db))
    import backend.media_paths as media_paths
    media_paths.invalidate_media_dir_cache()
    tv = tmp_path / "media" / "TV" / "Show" / "Season 01"
    tv.mkdir(parents=True)
    clip = tv / "Show - S01E01 1080p x264.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=1",
                    "-c:v", "libx264", str(clip)], check=True)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO media_dirs (path, label) VALUES (?, 'TV')", (str(tmp_path / "media" / "TV"),))
        await db.execute("INSERT INTO settings (key, value) VALUES ('sonarr_path_mapping', ?)",
                         (f"{tmp_path / 'media' / 'TV'}=/tv",))
        await db.commit()
    got = await webhook_arr({**SONARR, "episodeFile": {"path": "/tv/Show/Season 01/Show - S01E01 1080p x264.mkv"}})
    assert (got["added"], got["errors"]) == (1, [])
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT file_path FROM jobs") as cur:
            assert [r[0] for r in await cur.fetchall()] == [str(clip)]
        async with db.execute("SELECT COUNT(*) FROM scan_results WHERE file_path = ?", (str(clip),)) as cur:
            assert (await cur.fetchone())[0] == 1
    outside = await webhook_arr({**SONARR, "episodeFile": {"path": "/etc/passwd"}})
    assert outside["added"] == 0 and outside["errors"][0].startswith("Outside media dirs")
