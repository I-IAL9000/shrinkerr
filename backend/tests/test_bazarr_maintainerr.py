"""Bazarr and Maintainerr (v0.10.0).

- Bazarr: an imported file waits N minutes before it's converted, so its
  subtitles arrive first (and are merged in) — jobs carry `not_before`,
  which the local worker and remote workers both respect.
- Maintainerr: titles in its collections are about to be removed; the
  queue leaves them out (like hardlinked files) and a filter shows them."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiosqlite
import httpx
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def env(test_db, monkeypatch):
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
        for path in ("/m/Movies/Film (2020)/film.mkv", "/m/TV/Show/S01/e1.mkv", "/m/TV/Other/S01/e1.mkv"):
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, duration, video_codec, needs_conversion, "
                "audio_tracks_json, scan_timestamp) VALUES (?, 4000000000, 3600, 'h264', 1, '[]', '2026-10-10')", (path,))
        await db.commit()
    return test_db


async def _set(db_path, **values):
    async with aiosqlite.connect(db_path) as db:
        for k, v in values.items():
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()


async def _jobs(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path, not_before FROM jobs ORDER BY file_path") as cur:
            return await cur.fetchall()


# ── Bazarr: wait after an import ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_imports_wait_explicit_adds_dont(env):
    from backend.queue import JobQueue
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan, queue_new_files
    await _set(env, import_delay_minutes="30")
    before = datetime.now(timezone.utc)
    await queue_new_files(["/m/TV/Show/S01/e1.mkv"], 0, JobQueue(env))
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=["/m/Movies/Film (2020)/film.mkv"]))
    jobs = dict(await _jobs(env))
    assert jobs["/m/Movies/Film (2020)/film.mkv"] is None
    waits = datetime.fromisoformat(jobs["/m/TV/Show/S01/e1.mkv"]) - before
    assert timedelta(minutes=29) < waits < timedelta(minutes=31)


@pytest.mark.asyncio
async def test_no_wait_by_default(env):
    from backend.queue import JobQueue
    from backend.routes.jobs import queue_new_files
    await queue_new_files(["/m/TV/Show/S01/e1.mkv"], 0, JobQueue(env))
    assert await _jobs(env) == [("/m/TV/Show/S01/e1.mkv", None)]


@pytest.mark.asyncio
async def test_the_workers_wait_for_it(env, monkeypatch):
    from backend.queue import JobQueue
    import backend.nodes as nodes
    import backend.routes.nodes as nodes_route
    q = JobQueue(env)
    later = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    [waiting] = await q.add_jobs_bulk([{"file_path": "/m/a.mkv", "job_type": "convert", "not_before": later}])
    assert await q.get_next_job() is None
    assert await q.get_next_job(exclude_ids=[999]) is None

    async def no_token(*a, **kw):
        return None
    monkeypatch.setattr(nodes_route, "_require_node_token", no_token)
    nm = nodes.NodeManager()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(node_manager=nm)))
    async with aiosqlite.connect(env) as db:
        await db.execute("INSERT INTO worker_nodes (id, name, capabilities, status, registered_at) "
                         "VALUES ('n1', 'n1', '[\"libx265\"]', 'online', 'x')")
        await db.commit()
    assert (await nodes_route.request_job(nodes_route.RequestJobBody(node_id="n1"), request))["job"] is None

    async with aiosqlite.connect(env) as db:  # its time has come
        await db.execute("UPDATE jobs SET not_before = ? WHERE id = ?",
                         ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), waiting))
        await db.commit()
    assert (await q.get_next_job())["id"] == waiting
    assert (await nodes_route.request_job(nodes_route.RequestJobBody(node_id="n1"), request))["job"]["id"] == waiting


# ── Maintainerr ──────────────────────────────────────────────────────────

PLEX_ITEMS = """<MediaContainer>
  <Video ratingKey="11"><Media><Part file="/data/movies/Film (2020)/film.mkv"/></Media></Video>
  <Directory ratingKey="22"><Location path="/data/tv/Show"/></Directory>
</MediaContainer>"""


@pytest.fixture
def maintainerr(env, monkeypatch):
    """Maintainerr and Plex, faked: what was asked of each."""
    import backend.plex as plex
    asked = []

    async def plex_settings():
        return "http://plex", "tok", "/m/Movies=/data/movies;/m/TV=/data/tv"
    monkeypatch.setattr(plex, "_get_plex_settings", plex_settings)
    state = {"down": False}

    def handler(request):
        url = str(request.url)
        asked.append(url)
        if state["down"]:
            return httpx.Response(500)
        if url == "http://maintainerr:6246/api/collections":
            return httpx.Response(200, json=[{"id": 1, "title": "Leaving Soon", "isActive": True, "mediaServerType": "plex"},
                                             {"id": 2, "title": "Off", "isActive": False}])
        if url == "http://maintainerr:6246/api/collections/media?collectionId=1":
            return httpx.Response(200, json=[{"mediaServerId": "11"}, {"plexId": 22}])  # 22: before Maintainerr 3
        if url == "http://plex/library/metadata/11,22":
            return httpx.Response(200, text=PLEX_ITEMS)
        return httpx.Response(404)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    return asked, state


async def _leaving(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT folder_path, collection FROM maintainerr_media ORDER BY folder_path") as cur:
            return await cur.fetchall()


@pytest.mark.asyncio
async def test_its_collections_are_read(env, maintainerr):
    from backend.maintainerr import sync_maintainerr
    asked, state = maintainerr
    assert await sync_maintainerr() is None  # not set up
    await _set(env, maintainerr_url="http://maintainerr:6246/")
    assert await sync_maintainerr() == 2
    assert await _leaving(env) == [("/m/Movies/Film (2020)/", "Leaving Soon"), ("/m/TV/Show/", "Leaving Soon")]
    assert "collectionId=2" not in " ".join(asked)  # inactive
    state["down"] = True
    with pytest.raises(httpx.HTTPStatusError):
        await sync_maintainerr()
    assert len(await _leaving(env)) == 2  # kept
    await _set(env, maintainerr_url="")
    assert await sync_maintainerr() is None and await _leaving(env) == []  # removed: cleared


@pytest.mark.asyncio
async def test_the_queue_leaves_them_out(env, maintainerr):
    from backend.maintainerr import sync_maintainerr
    from backend.routes.jobs import BulkQueueFromScanRequest, EstimateRequest, add_jobs_from_scan, estimate_jobs
    from backend.routes.scan import _paths_matching
    await _set(env, maintainerr_url="http://maintainerr:6246")
    await sync_maintainerr()
    paths = ["/m/Movies/Film (2020)/film.mkv", "/m/TV/Show/S01/e1.mkv", "/m/TV/Other/S01/e1.mkv"]
    assert sorted(await _paths_matching("leaving_soon")) == sorted(paths[:2])
    est = await estimate_jobs(EstimateRequest(file_paths=paths))
    assert (est["leaving_soon"], est["total_files"]) == (2, 1)
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=paths))
    assert [p for p, _ in await _jobs(env)] == ["/m/TV/Other/S01/e1.mkv"]
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=paths, override_rules=True))  # "include ignored"
    assert len(await _jobs(env)) == 3


@pytest.mark.asyncio
async def test_turned_off_they_are_queued(env, maintainerr):
    from backend.maintainerr import sync_maintainerr
    from backend.routes.jobs import BulkQueueFromScanRequest, add_jobs_from_scan
    await _set(env, maintainerr_url="http://maintainerr:6246", maintainerr_skip="false")
    await sync_maintainerr()
    await add_jobs_from_scan(BulkQueueFromScanRequest(file_paths=["/m/TV/Show/S01/e1.mkv"]))
    assert len(await _jobs(env)) == 1


@pytest.mark.asyncio
async def test_jellyfin_collections(env, monkeypatch):
    import backend.jellyfin as jellyfin
    from backend.maintainerr import _jellyfin_folders

    async def settings():
        return {"jellyfin_url": "http://jf", "jellyfin_api_key": "k", "jellyfin_user_id": "u",
                "jellyfin_path_mapping": "/m=/jf"}
    monkeypatch.setattr(jellyfin, "_get_jellyfin_settings", settings)

    def handler(request):
        assert request.url.params["Ids"] == "a,b"
        return httpx.Response(200, json={"Items": [{"Type": "Movie", "Path": "/jf/Movies/Film/film.mkv"},
                                                   {"Type": "Series", "Path": "/jf/TV/Show"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await _jellyfin_folders(client, ["a", "b"]) == ["/m/Movies/Film", "/m/TV/Show"]
