"""The first-run default encoder follows what the host can run, in the same
order jobs are translated in (Intel / AMD hosts started on the CPU), and the
NZBGet / SABnzbd webhook queues with the configured encoder (it was always
NVENC)."""
import aiosqlite
import pytest


async def _setting(db_path, key):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


@pytest.mark.asyncio
@pytest.mark.parametrize("caps, expected", [
    (["libx265", "nvenc", "qsv"], "nvenc"),
    (["libx265", "qsv", "vaapi"], "qsv"),
    (["libx265", "vaapi"], "vaapi"),
    (["libx265", "videotoolbox"], "videotoolbox"),
    (["libx265"], "libx265"),
])
async def test_the_first_default_encoder_is_the_hosts_best(test_db, monkeypatch, caps, expected):
    import backend.nodes as nodes
    monkeypatch.setattr(nodes, "DB_PATH", test_db)
    nm = nodes.NodeManager()

    async def none(*a, **kw):
        return None

    async def detect(gpu_name=None):
        return list(caps), None
    monkeypatch.setattr(nodes.NodeManager, "_detect_gpu", staticmethod(none))
    monkeypatch.setattr(nodes.NodeManager, "_detect_driver_version", staticmethod(none))
    monkeypatch.setattr(nodes.NodeManager, "_detect_ffmpeg_version", staticmethod(none))
    monkeypatch.setattr(nodes.NodeManager, "_detect_capabilities", staticmethod(detect))
    await nm.register_local_node()
    assert await _setting(test_db, "default_encoder") == expected


@pytest.mark.asyncio
async def test_a_chosen_encoder_is_kept(test_db, monkeypatch):
    import backend.nodes as nodes
    monkeypatch.setattr(nodes, "DB_PATH", test_db)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO settings (key, value) VALUES ('default_encoder', 'libx265')")
        await db.commit()

    async def none(*a, **kw):
        return None

    async def detect(gpu_name=None):
        return ["libx265", "qsv"], None
    for name in ("_detect_gpu", "_detect_driver_version", "_detect_ffmpeg_version"):
        monkeypatch.setattr(nodes.NodeManager, name, staticmethod(none))
    monkeypatch.setattr(nodes.NodeManager, "_detect_capabilities", staticmethod(detect))
    await nodes.NodeManager().register_local_node()
    assert await _setting(test_db, "default_encoder") == "libx265"


@pytest.mark.asyncio
async def test_the_webhook_queues_with_the_default_encoder(test_db, tmp_path, monkeypatch):
    import sys
    import backend.scanner as scanner
    from backend.queue import JobQueue, QueueWorker
    from backend.routes.jobs import init_job_routes
    from backend.routes.webhooks import WebhookQueueRequest, webhook_queue
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    worker = QueueWorker(test_db)
    monkeypatch.setattr(worker, "start_if_idle", lambda *a, **kw: False)
    init_job_routes(worker, JobQueue(test_db))
    media = tmp_path / "media"
    media.mkdir()
    film = media / "Film (2020).mkv"
    film.write_bytes(b"x")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO media_dirs (path) VALUES (?)", (str(media),))
        await db.execute("INSERT INTO settings (key, value) VALUES ('default_encoder', 'qsv')")
        await db.commit()

    async def fake_probe(path, *a, **kw):
        return {"video_codec": "h264", "video_width": 1920, "video_height": 1080,
                "audio_tracks": [], "subtitle_tracks": [], "duration": 60, "file_size": 1}
    monkeypatch.setattr(scanner, "probe_file", fake_probe)
    await webhook_queue(WebhookQueueRequest(paths=[str(film)]))
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT encoder FROM jobs") as cur:
            assert await cur.fetchall() == [("qsv",)]
