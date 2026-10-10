"""Sonarr / Radarr cutoff and monitoring (v0.10.0), for the Scanner's
"Below cutoff" and "Unmonitored" filters: converting a file Sonarr or Radarr
will replace (below the quality cutoff) is wasted work."""
import aiosqlite
import httpx
import pytest

import backend.arr as arr


def _client(routes: dict) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path + ("?" + request.url.query.decode() if request.url.query else "")
        return httpx.Response(200, json=routes[key])
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_radarr_movies_carry_their_file():
    async with _client({"/api/v3/movie": [
        {"monitored": True, "movieFile": {"path": "/movies/A/a.mkv", "qualityCutoffNotMet": True}},
        {"monitored": False, "movieFile": {"path": "/movies/B/b.mkv", "qualityCutoffNotMet": False}},
        {"monitored": True, "hasFile": False},  # nothing on disk
    ]}) as client:
        assert await arr._radarr_file_status(client, "http://radarr", "k") == [
            ("/movies/A/a.mkv", True, True), ("/movies/B/b.mkv", False, False)]


@pytest.mark.asyncio
async def test_sonarr_files_are_monitored_by_series_and_episode():
    async with _client({
        "/api/v3/series": [{"id": 1, "monitored": True}, {"id": 2, "monitored": False}],
        "/api/v3/episodefile?seriesId=1": [{"id": 10, "path": "/tv/S/e1.mkv", "qualityCutoffNotMet": True},
                                           {"id": 11, "path": "/tv/S/e2.mkv", "qualityCutoffNotMet": False}],
        "/api/v3/episode?seriesId=1": [{"episodeFileId": 10, "monitored": True},
                                       {"episodeFileId": 11, "monitored": False}],
        "/api/v3/episodefile?seriesId=2": [{"id": 20, "path": "/tv/T/e1.mkv", "qualityCutoffNotMet": False}],
        "/api/v3/episode?seriesId=2": [{"episodeFileId": 20, "monitored": True}],
    }) as client:
        assert sorted(await arr._sonarr_file_status(client, "http://sonarr", "k")) == [
            ("/tv/S/e1.mkv", True, True), ("/tv/S/e2.mkv", False, False),
            ("/tv/T/e1.mkv", False, False)]  # its series isn't monitored


def test_paths_come_back_through_the_mapping():
    mapping = "/media/Movies=/movies;/media/TV=/tv"
    assert arr._from_arr_path("/movies/A/a.mkv", mapping) == "/media/Movies/A/a.mkv"
    assert arr._from_arr_path("/tv//S/e1.mkv", mapping) == "/media/TV/S/e1.mkv"
    assert arr._from_arr_path("/elsewhere/x.mkv", mapping) == "/elsewhere/x.mkv"


@pytest.mark.asyncio
async def test_the_sync_stores_them_and_keeps_them_when_a_service_is_down(test_db, monkeypatch):
    async with aiosqlite.connect(test_db) as db:
        for key, value in (("radarr_url", "http://radarr"), ("radarr_api_key", "k"),
                           ("radarr_path_mapping", "/media/Movies=/movies"),
                           ("sonarr_url", "http://sonarr"), ("sonarr_api_key", "k")):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

    async def radarr(client, url, key):
        return [("/movies/A/a.mkv", True, True)]

    async def sonarr(client, url, key):
        return [("/tv/S/e1.mkv", False, False)]
    monkeypatch.setattr(arr, "_radarr_file_status", radarr)
    monkeypatch.setattr(arr, "_sonarr_file_status", sonarr)
    assert await arr.sync_arr_file_status() == {"sonarr": 1, "radarr": 1}

    async def rows():
        async with aiosqlite.connect(test_db) as db:
            async with db.execute("SELECT file_path, service, monitored, cutoff_unmet FROM arr_file_status "
                                  "ORDER BY file_path") as cur:
                return await cur.fetchall()
    assert await rows() == [("/media/Movies/A/a.mkv", "radarr", 1, 1), ("/tv/S/e1.mkv", "sonarr", 0, 0)]

    async def down(client, url, key):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(arr, "_sonarr_file_status", down)
    assert await arr.sync_arr_file_status() == {"radarr": 1}
    assert len(await rows()) == 2  # Sonarr's rows kept
