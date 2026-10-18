"""Telling Sonarr / Radarr about a converted file (v0.10.0): the series /
movie list is kept for 5 minutes, and listing a big library takes them
10-20 s — every placement after it expired waited for a new one (approving
270 reviews took 10+ minutes). An expired list answers while a new one is
fetched in the background; only a file it doesn't know waits for the new
list (a series added since)."""
import asyncio
import time

import pytest

OLD = [{"id": 1, "path": "/tv/Show (2010)"}]
NEW = OLD + [{"id": 2, "path": "/tv/New Show (2026)"}]


@pytest.fixture
def sonarr(monkeypatch):
    import backend.arr as arr

    async def settings():
        return {"sonarr_url": "http://sonarr", "sonarr_api_key": "k", "sonarr_path_mapping": "/media/TV=/tv"}

    listed, fetches, commands = asyncio.Event(), [], []

    async def fetch(client, service, url, key):
        fetches.append(service)
        await listed.wait()
        arr._sonarr_cache.update(data=NEW, fetched_at=time.monotonic())
        return NEW

    class Response:
        def raise_for_status(self):
            pass

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            commands.append(json)
            return Response()

    monkeypatch.setattr(arr, "_get_arr_settings", settings)
    monkeypatch.setattr(arr, "_fetch_items", fetch)
    monkeypatch.setattr(arr.httpx, "AsyncClient", Client)
    monkeypatch.setattr(arr, "_sonarr_cache", {"data": OLD, "fetched_at": time.monotonic() - 3600})
    monkeypatch.setattr(arr, "_refreshing", {})
    monkeypatch.setattr(arr, "_recent_rescans", {})
    return arr, listed, fetches, commands


@pytest.mark.asyncio
async def test_an_expired_list_answers_at_once(sonarr):
    arr, listed, fetches, commands = sonarr
    assert await asyncio.wait_for(arr.trigger_sonarr_rescan("/media/TV/Show (2010)/Season 01/ep.mkv"), 1)
    await asyncio.sleep(0)
    assert commands == [{"name": "RescanSeries", "seriesId": 1}] and fetches == ["sonarr"]  # refreshed meanwhile
    listed.set()


@pytest.mark.asyncio
async def test_a_series_it_doesnt_know_waits_for_the_new_list(sonarr):
    arr, listed, fetches, commands = sonarr
    task = asyncio.create_task(arr.trigger_sonarr_rescan("/media/TV/New Show (2026)/Season 01/ep.mkv"))
    await asyncio.sleep(0.05)
    assert not task.done()
    listed.set()
    assert await asyncio.wait_for(task, 1) and commands == [{"name": "RescanSeries", "seriesId": 2}]
    assert fetches == ["sonarr"]  # the background refresh, not a second fetch
