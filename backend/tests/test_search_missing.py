"""v0.10.0 (SC-10): "Search missing" asked /wanted/missing for each selected
series with a seriesId filter that endpoint doesn't have, so every selected
series triggered a search for the whole library's missing episodes."""
import httpx
import pytest

import backend.arr as arr

EPISODES = [
    {"id": 1, "seriesId": 7, "monitored": True, "hasFile": False, "airDateUtc": "2020-01-01T00:00:00Z"},  # missing
    {"id": 2, "seriesId": 7, "monitored": True, "hasFile": True, "airDateUtc": "2020-01-08T00:00:00Z"},   # have it
    {"id": 3, "seriesId": 7, "monitored": False, "hasFile": False, "airDateUtc": "2020-01-15T00:00:00Z"}, # unmonitored
    {"id": 4, "seriesId": 7, "monitored": True, "hasFile": False, "airDateUtc": "2099-01-01T00:00:00Z"},  # not aired
    {"id": 5, "seriesId": 7, "monitored": True, "hasFile": False, "airDateUtc": None},                     # TBA
]


@pytest.mark.asyncio
async def test_search_missing_only_searches_the_selected_series(monkeypatch):
    commands = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 7, "title": "Show", "path": "/tv/Show (2010)"},
                                             {"id": 8, "title": "Other", "path": "/tv/Other"}])
        if path == "/api/v3/episode":
            assert request.url.params["seriesId"] == "7"
            return httpx.Response(200, json=EPISODES)
        if path == "/api/v3/command":
            commands.append(request.read())
            return httpx.Response(201, json={})
        if path == "/api/v3/wanted/missing":
            raise AssertionError("library-wide missing list must not be used")
        return httpx.Response(404)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(arr.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(arr, "_sonarr_cache", {})

    async def settings():
        return {"sonarr_url": "http://sonarr", "sonarr_api_key": "k", "sonarr_path_mapping": "/media/TV=/tv"}

    monkeypatch.setattr(arr, "_get_arr_settings", settings)
    result = await arr.search_missing_episodes(["/media/TV/Show (2010)/Season 01/Show - S01E02.mkv"])

    assert result["success"] and result["total_episode_ids"] == 1
    assert len(commands) == 1 and b'"episodeIds":[1]' in commands[0].replace(b" ", b"")
