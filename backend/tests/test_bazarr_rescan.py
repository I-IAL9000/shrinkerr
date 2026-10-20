"""Bazarr rescan (v0.10.0): after a file is rewritten, Bazarr is asked to
re-sync the series / movie from Sonarr / Radarr and scan its subtitles, so
it doesn't download the merged sidecars again. Remote workers' jobs now
tell Sonarr / Radarr (and Bazarr) too."""
import asyncio
import json
import shutil
import subprocess
from types import SimpleNamespace

import aiosqlite
import httpx
import pytest

import backend.arr as arr
from backend import bazarr


async def _set(db_path, **values):
    async with aiosqlite.connect(db_path) as db:
        for k, v in values.items():
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()


@pytest.fixture
def servers(monkeypatch):
    """Sonarr, Radarr and Bazarr, faked: the requests Bazarr got."""
    monkeypatch.setattr(arr, "_sonarr_cache", {})
    monkeypatch.setattr(arr, "_radarr_cache", {})
    bazarr_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "sonarr" and path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 7, "path": "/tv/Show"}])
        if host == "radarr" and path == "/api/v3/movie":
            return httpx.Response(200, json=[{"id": 9, "path": "/movies/Film (2020)"}])
        if host == "bazarr":
            bazarr_calls.append((request.method, path, dict(request.url.params), request.headers.get("x-api-key")))
            if path == "/api/system/status":
                return httpx.Response(200, json={"data": {"bazarr_version": "1.5.1"}})
            return httpx.Response(204)
        return httpx.Response(404)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    return bazarr_calls


@pytest.mark.asyncio
async def test_bazarr_rescans_the_series_or_movie(test_db, servers):
    await _set(test_db, sonarr_url="http://sonarr", sonarr_api_key="s", sonarr_path_mapping="/m/TV=/tv",
               radarr_url="http://radarr", radarr_api_key="r", radarr_path_mapping="/m/Movies=/movies")
    assert await bazarr.rescan_for_file("/m/TV/Show/Season 01/Show - S01E02.mkv") is None  # Bazarr not set up
    await _set(test_db, bazarr_url="http://bazarr:6767/", bazarr_api_key="bz")
    assert await bazarr.rescan_for_file("/m/TV/Show/Season 01/Show - S01E02.mkv") == {"ok": True, "kind": "series", "id": 7}
    assert await bazarr.rescan_for_file("/m/Movies/Film (2020)/Film (2020).mkv") == {"ok": True, "kind": "movies", "id": 9}
    assert servers == [
        ("PATCH", "/api/series", {"seriesid": "7", "action": "sync"}, "bz"),
        ("PATCH", "/api/series", {"seriesid": "7", "action": "scan-disk"}, "bz"),
        ("PATCH", "/api/movies", {"radarrid": "9", "action": "sync"}, "bz"),
        ("PATCH", "/api/movies", {"radarrid": "9", "action": "scan-disk"}, "bz"),
    ]
    assert await bazarr.rescan_for_file("/m/TV/Other/Season 01/Other - S01E01.mkv") is None  # not in Sonarr


@pytest.mark.asyncio
async def test_the_connection_test_and_the_key(test_db, servers, monkeypatch):
    import backend.routes.settings as settings_route
    from backend.models import SettingsUpdate
    monkeypatch.setattr(settings_route, "DB_PATH", test_db)
    await settings_route.update_encoding_settings(SettingsUpdate(bazarr_url="http://bazarr:6767", bazarr_api_key="secret1234"))
    got = await settings_route.get_encoding_settings()
    assert (got["bazarr_url"], got["bazarr_api_key"]) == ("http://bazarr:6767", "****1234")
    await settings_route.update_encoding_settings(SettingsUpdate(bazarr_api_key="****1234"))  # masked: kept
    assert (await settings_route.test_api_key(settings_route.TestApiRequest(service="bazarr")))["version"] == "1.5.1"
    assert servers[-1][3] == "secret1234" and "bazarr_api_key" in settings_route._SECRET_SETTINGS_KEYS



@pytest.mark.asyncio
async def test_bazarr_searches_a_titles_missing_subtitles(test_db, servers, monkeypatch):
    """The Scanner's "Search for subtitles": the file's movie or series, found
    through Sonarr / Radarr like the *arr actions. Bazarr searches for
    minutes, so the request only starts it, once per title at a time."""
    from backend.api_errors import ApiError
    from backend.routes import arr as arr_route
    episode = arr_route.BazarrSearchRequest(file_path="/m/TV/Show/Season 01/Show - S01E02.mkv")
    film = arr_route.BazarrSearchRequest(file_path="/m/Movies/Film (2020)/Film (2020).mkv")
    await _set(test_db, sonarr_url="http://sonarr", sonarr_api_key="s", sonarr_path_mapping="/m/TV=/tv",
               radarr_url="http://radarr", radarr_api_key="r", radarr_path_mapping="/m/Movies=/movies")
    with pytest.raises(ApiError) as err:
        await arr_route.bazarr_search(film)
    assert (err.value.status_code, err.value.code) == (400, "arr.bazarrNotConfigured")
    await _set(test_db, bazarr_url="http://bazarr:6767", bazarr_api_key="bz")
    searching, search = asyncio.Event(), bazarr._search_missing

    async def until_released(*args):
        await searching.wait()
        return await search(*args)
    monkeypatch.setattr(bazarr, "_search_missing", until_released)
    assert await arr_route.bazarr_search(episode) == {"ok": True, "kind": "series", "id": 7, "already_running": False}
    assert await arr_route.bazarr_search(episode) == {"ok": True, "kind": "series", "id": 7, "already_running": True}
    assert await arr_route.bazarr_search(film) == {"ok": True, "kind": "movies", "id": 9, "already_running": False}
    assert servers == []  # not waited for
    searching.set()
    assert await asyncio.gather(*bazarr._searches.values()) == [{"ok": True}, {"ok": True}]
    assert sorted(servers) == [  # (side by side)
        ("PATCH", "/api/movies", {"radarrid": "9", "action": "search-missing"}, "bz"),
        ("PATCH", "/api/series", {"seriesid": "7", "action": "search-missing"}, "bz"),
    ] and bazarr._searches == {}
    with pytest.raises(ApiError) as err:
        await arr_route.bazarr_search(arr_route.BazarrSearchRequest(file_path="/m/TV/Other/Season 01/Other - S01E01.mkv"))
    assert (err.value.status_code, err.value.code) == (404, "arr.bazarrNotFound")


@pytest.mark.asyncio
async def test_bazarrs_reason_when_the_search_fails(test_db, monkeypatch, capsys):
    from backend.routes import arr as arr_route
    monkeypatch.setattr(arr, "_radarr_cache", {})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "radarr":
            return httpx.Response(200, json=[{"id": 9, "path": "/movies/Film (2020)"}])
        return httpx.Response(500, json="Movie file not found. Path mapping issue?")
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    await _set(test_db, radarr_url="http://radarr", radarr_api_key="r", radarr_path_mapping="/m/Movies=/movies",
               bazarr_url="http://bazarr:6767", bazarr_api_key="bz")
    await arr_route.bazarr_search(arr_route.BazarrSearchRequest(file_path="/m/Movies/Film (2020)/Film (2020).mkv"))
    error = "HTTP 500: Movie file not found. Path mapping issue?"
    assert await bazarr._searches[("movies", 9)] == {"ok": False, "error": error}
    assert f"Subtitle search of movies 9 failed: {error}" in capsys.readouterr().out

def _x265() -> bool:
    return bool(shutil.which("ffmpeg")) and "libx265" in subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


@pytest.mark.skipif(not _x265(), reason="needs ffmpeg with libx265")
@pytest.mark.asyncio
async def test_a_finished_job_asks_bazarr(test_db, tmp_path, monkeypatch):
    import sys
    from backend.queue import JobQueue, QueueWorker
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    asked = []
    monkeypatch.setattr(bazarr, "schedule_rescan", lambda path, delay=60: asked.append(path))
    src = tmp_path / "media" / "TV" / "Show" / "Show - S01E01 x264.mkv"
    src.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=2",
                    "-c:v", "mpeg4", "-q:v", "1", str(src)], check=True)
    await _set(test_db, backup_original_days="0", trash_original_after_conversion="false", vmaf_analysis_enabled="false")
    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(src), "convert", encoder="libx265")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE jobs SET libx265_preset = 'ultrafast' WHERE id = ?", (job_id,))
        await db.commit()
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            job = dict(await cur.fetchone())
    await QueueWorker(test_db)._process_job(job)
    assert asked == [str(src.parent / "Show - S01E01 x265.mkv")]


@pytest.mark.asyncio
async def test_a_workers_job_tells_sonarr_and_bazarr(test_db, monkeypatch):
    import backend.nodes as nodes
    import backend.routes.nodes as nodes_route
    import backend.queue as queue_mod
    from backend.queue import JobQueue
    monkeypatch.setattr(nodes, "DB_PATH", test_db)

    async def no_token(*a, **kw):
        return None
    monkeypatch.setattr(nodes_route, "_require_node_token", no_token)

    async def refreshed(*a, **kw):
        return None
    monkeypatch.setattr(queue_mod, "refresh_converted_scan_row", refreshed)
    told = []

    async def rescan(path):
        told.append(("arr", path))
        return {}
    monkeypatch.setattr(arr, "trigger_arr_rescan", rescan)
    monkeypatch.setattr(bazarr, "schedule_rescan", lambda path, delay=60: told.append(("bazarr", path)))
    nm = nodes.NodeManager()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(node_manager=nm)))
    job_id = await JobQueue(test_db).add_job("/media/TV/Show/e1.mkv", "convert", encoder="libx265")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO worker_nodes (id, name, status, registered_at, path_mappings) VALUES "
                         "('n1', 'n1', 'online', 'x', ?)", (json.dumps([{"server": "/media", "worker": "/mnt"}]),))
        await db.execute("UPDATE jobs SET status = 'running', assigned_node_id = 'n1' WHERE id = ?", (job_id,))
        await db.commit()
    await nodes_route.report_complete(nodes_route.CompletionReport(
        node_id="n1", job_id=job_id, success=True, output_path="/mnt/TV/Show/e1 x265.mkv", space_saved=500,
        replaced_source=True), request)
    for _ in range(3):
        await asyncio.sleep(0)
    assert told == [("arr", "/media/TV/Show/e1 x265.mkv"), ("bazarr", "/media/TV/Show/e1 x265.mkv")]


@pytest.mark.asyncio
async def test_a_long_scan_is_waited_for_and_an_error_named(test_db, monkeypatch, capsys):
    """48 Hours (37 seasons): Bazarr answers a scan once it has scanned the
    series, past the 30 s Shrinkerr waited — "[BAZARR] Rescan failed for …:"
    and nothing after it, a timeout's message being empty (v0.10.0)."""
    monkeypatch.setattr(arr, "_sonarr_cache", {})
    monkeypatch.setattr(arr, "_refreshing", {})
    timeouts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "sonarr":
            return httpx.Response(200, json=[{"id": 7, "path": "/tv/Show"}])
        raise httpx.ReadTimeout("", request=request)
    real = httpx.AsyncClient

    def client(**kw):
        timeouts.append(kw.get("timeout"))
        return real(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "AsyncClient", client)
    await _set(test_db, sonarr_url="http://sonarr", sonarr_api_key="s", sonarr_path_mapping="/m/TV=/tv",
               bazarr_url="http://bazarr:6767", bazarr_api_key="bz")

    result = await bazarr.rescan_for_file("/m/TV/Show/Season 37/Show - S37E18.mkv")

    assert result == {"ok": False, "error": "ReadTimeout"}
    assert "Rescan failed for /m/TV/Show/Season 37/Show - S37E18.mkv: ReadTimeout" in capsys.readouterr().out
    assert any(isinstance(t, httpx.Timeout) and t.read is None for t in timeouts)  # Bazarr's scan is waited for
