"""Content type detection and resolution-aware quality used to change only the
queue estimate: jobs always encoded at the rule's or the global CQ (v0.10.0).
They now set a job's CQ, the same one the estimate shows; a rule or the Add to
Queue dialog still wins. Existing installs get content detection switched off
once, so their output doesn't change by surprise."""
import aiosqlite
import pytest
import pytest_asyncio

from backend.content_detect import smart_cq, smart_cq_settings, smart_quality

ANIME = "/m/anime/[SubsPlease] Frieren - 01 (1080p).mkv"
SCOPE = "/m/movies/Scope (2010)/Scope.mkv"
PLAIN = "/m/movies/Plain (2010)/Plain.mkv"


def _settings(**values):
    return smart_cq_settings({k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in values.items()})


def test_content_type_then_resolution_then_global():
    both = _settings(content_type_detection=True, resolution_aware_cq=True, resolution_cq_1080p=21)
    assert smart_cq(ANIME, 1920, 1080, both) == (22, "anime")        # the anime table
    assert smart_cq(SCOPE, 1920, 800, both) == (21, None)            # 1080p by width (SC-22)
    content_only = _settings(content_type_detection=True, resolution_aware_cq=False)
    assert smart_cq(PLAIN, 1920, 1080, content_only) == (None, None)  # global CQ
    off = _settings(content_type_detection=False, resolution_aware_cq=False)
    assert smart_cq(ANIME, 1920, 1080, off) == (None, None)


def test_a_job_gets_cq_and_matching_crf():
    s = _settings(content_type_detection=True)
    assert smart_quality(ANIME, 1920, 1080, s) == (22, 24)
    assert smart_quality(PLAIN, 1920, 1080, s) == (None, None)


@pytest_asyncio.fixture
async def jobs_env(test_db, monkeypatch):
    import sys
    from backend.queue import JobQueue, QueueWorker
    from backend.routes.jobs import init_job_routes
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    worker = QueueWorker(test_db)
    # Adding jobs auto-starts the worker; it must not try to encode the fakes.
    monkeypatch.setattr(worker, "start", lambda *a, **kw: None)
    init_job_routes(worker, JobQueue(test_db))
    async with aiosqlite.connect(test_db) as db:
        for key, value in (("content_type_detection", "true"), ("resolution_aware_cq", "true"),
                           ("resolution_cq_1080p", "21"), ("nvenc_cq", "27")):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        for path, width, height in ((ANIME, 1920, 1080), (SCOPE, 1920, 800), (PLAIN, 1280, 720)):
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, "
                "video_width, video_height, duration, audio_tracks_json, subtitle_tracks_json, scan_timestamp) "
                "VALUES (?, 1000000000, 'h264', 1, ?, ?, 3600, '[]', '[]', '2026-01-01')",
                (path, width, height))
        await db.commit()
    return test_db


async def _job_quality(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path, nvenc_cq, libx265_crf FROM jobs ORDER BY file_path") as cur:
            return {r[0]: (r[1], r[2]) for r in await cur.fetchall()}


@pytest.mark.asyncio
async def test_add_to_queue_gives_each_job_its_cq(jobs_env):
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=[ANIME, SCOPE, PLAIN]))
    assert await _job_quality(jobs_env) == {
        ANIME: (22, 24),     # content type
        SCOPE: (21, 23),     # resolution-aware, 1080p
        PLAIN: (18, 20),     # resolution-aware, 720p default
    }


@pytest.mark.asyncio
async def test_the_dialogs_quality_wins(jobs_env):
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=[ANIME], nvenc_cq_override=30))
    assert await _job_quality(jobs_env) == {ANIME: (30, None)}


@pytest.mark.asyncio
async def test_the_estimate_shows_the_jobs_cq(jobs_env):
    from backend.routes.jobs import EstimateRequest, estimate_jobs
    for path, cq in ((ANIME, 22), (SCOPE, 21)):
        est = await estimate_jobs(EstimateRequest(file_paths=[path]))
        assert est["cq"] == cq, (path, est.get("cq"))


@pytest.mark.asyncio
async def test_add_by_path_reads_its_settings(jobs_env, tmp_path, monkeypatch):
    """add-by-path read its settings with `async with connect_db()`, which
    always failed: the saved encoder and source codecs were ignored."""
    import backend.scanner as scanner
    from backend.routes.jobs import AddByPathRequest, add_jobs_by_path
    media = tmp_path / "media"
    media.mkdir()
    film = media / "[SubsPlease] Frieren - 02 (1080p).mkv"
    film.write_bytes(b"x")
    async with aiosqlite.connect(jobs_env) as db:
        await db.execute("INSERT INTO media_dirs (path) VALUES (?)", (str(media),))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('default_encoder', 'libx265')")
        await db.commit()

    async def fake_probe(path, *a, **kw):
        return {"video_codec": "h264", "video_width": 1920, "video_height": 1080,
                "audio_tracks": [], "subtitle_tracks": [], "duration": 1440, "file_size": 1}
    monkeypatch.setattr(scanner, "probe_file", fake_probe)
    await add_jobs_by_path(AddByPathRequest(file_paths=[str(film)]))
    async with aiosqlite.connect(jobs_env) as db:
        async with db.execute("SELECT encoder, nvenc_cq, libx265_crf FROM jobs") as cur:
            assert await cur.fetchall() == [("libx265", 22, 24)]


@pytest.mark.asyncio
async def test_the_webhook_gives_jobs_their_cq(jobs_env, tmp_path, monkeypatch):
    import backend.scanner as scanner
    from backend.routes.webhooks import WebhookQueueRequest, webhook_queue
    media = tmp_path / "media"
    media.mkdir()
    film = media / "[SubsPlease] Frieren - 03 (1080p).mkv"
    film.write_bytes(b"x")
    async with aiosqlite.connect(jobs_env) as db:
        await db.execute("INSERT INTO media_dirs (path) VALUES (?)", (str(media),))
        await db.commit()

    async def fake_probe(path, *a, **kw):
        return {"video_codec": "h264", "video_width": 1920, "video_height": 1080,
                "audio_tracks": [], "subtitle_tracks": [], "duration": 1440, "file_size": 1}
    monkeypatch.setattr(scanner, "probe_file", fake_probe)
    await webhook_queue(WebhookQueueRequest(paths=[str(film)]))
    assert await _job_quality(jobs_env) == {str(film): (22, 24)}


@pytest_asyncio.fixture
async def seed(test_db, monkeypatch):
    import backend.routes.settings as settings_route
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    return settings_route


async def _value(db_path, key):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


@pytest.mark.asyncio
async def test_existing_installs_get_content_detection_off_once(seed, test_db):
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO media_dirs (path) VALUES ('/media')")
        # A development build already ran the first v0.10 group, and a Settings
        # save stored the old default.
        await db.execute("INSERT INTO settings (key, value) VALUES ('defaults_v010_seeded', '1')")
        await db.execute("INSERT INTO settings (key, value) VALUES ('content_type_detection', 'true')")
        await db.commit()
    await seed.seed_v010_defaults()
    assert await _value(test_db, "content_type_detection") == "false"
    # Turned back on by the user: stays on.
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE settings SET value = 'true' WHERE key = 'content_type_detection'")
        await db.commit()
    await seed.seed_v010_defaults()
    assert await _value(test_db, "content_type_detection") == "true"


@pytest.mark.asyncio
async def test_new_installs_keep_content_detection_on(seed, test_db):
    await seed.seed_v010_defaults()
    assert await _value(test_db, "content_type_detection") is None  # the default: on
    enc = await seed.get_encoding_settings()
    assert enc["content_type_detection"] is True


@pytest.mark.asyncio
async def test_auto_queue_gives_jobs_their_cq(jobs_env):
    from backend.models import ScannedFile
    from backend.watcher import FileWatcher
    async with aiosqlite.connect(jobs_env) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('auto_queue_new', 'true')")
        await db.commit()

    def scanned(path, width, height):
        return ScannedFile(
            file_path=path, file_name=path.rsplit("/", 1)[1], folder_name="x", file_size=1,
            file_size_gb=0.0, video_codec="h264", needs_conversion=True, audio_tracks=[],
            native_language="eng", has_removable_tracks=False, estimated_savings_bytes=0,
            estimated_savings_gb=0.0, video_width=width, video_height=height)

    await FileWatcher(jobs_env)._auto_queue_new_files(
        [scanned(ANIME, 1920, 1080), scanned(SCOPE, 1920, 800)])
    q = await _job_quality(jobs_env)
    assert q[ANIME] == (22, 24) and q[SCOPE] == (21, 23)
