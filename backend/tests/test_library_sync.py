"""The media servers' and Sonarr / Radarr's data is read on its own, not only
after a full scan: Not watched in and Sonarr / Radarr showed 0 until a full
scan (hours on a big library) had run."""
import asyncio

import pytest

from backend import library_sync


def _fakes(monkeypatch, calls, fail=()):
    import backend.arr as arr
    import backend.emby as emby
    import backend.jellyfin as jellyfin
    import backend.maintainerr as maintainerr
    import backend.plex as plex
    import backend.routes.posters as posters

    def fake(name, result=None):
        async def run():
            calls.append(name)
            if name in fail:
                raise RuntimeError(f"{name} is down")
            return result if result is not None else {}
        return run
    monkeypatch.setattr(plex, "sync_plex_metadata_cache", fake("plex", {"watched": 3, "unwatched": 1}))
    monkeypatch.setattr(jellyfin, "sync_jellyfin_metadata_cache", fake("jellyfin"))
    monkeypatch.setattr(emby, "sync_emby_metadata_cache", fake("emby"))
    monkeypatch.setattr(arr, "sync_arr_file_status", fake("arr"))
    monkeypatch.setattr(maintainerr, "sync_maintainerr", fake("maintainerr"))
    monkeypatch.setattr(posters, "start_prefetch", fake("prefetch"))


@pytest.mark.asyncio
async def test_it_reads_them_all_and_one_failing_leaves_the_rest(monkeypatch):
    calls = []
    _fakes(monkeypatch, calls, fail=("plex",))
    await library_sync.sync_all()
    assert calls == ["plex", "jellyfin", "emby", "arr", "maintainerr"]  # Plex first: the others restore theirs after it


@pytest.mark.asyncio
async def test_the_loop_reads_after_start_up_and_every_few_hours_but_not_during_a_scan(monkeypatch):
    import backend.routes.scan as scan_routes
    calls, sleeps = [], []
    _fakes(monkeypatch, calls)
    scanning = iter([False, True, False])
    monkeypatch.setattr(scan_routes, "scan_is_actively_running", lambda: next(scanning))

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 3:
            raise asyncio.CancelledError
    monkeypatch.setattr(library_sync.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await library_sync.library_sync_loop()
    assert sleeps == [library_sync.STARTUP_DELAY, library_sync.INTERVAL, library_sync.INTERVAL, library_sync.INTERVAL]
    assert calls.count("arr") == 2  # the second round was skipped: a scan was running
    # Then the poster prefetch (v0.10.0): posters for new titles, and every
    # title's country, genres, network and show status.
    assert calls == ["plex", "jellyfin", "emby", "arr", "maintainerr", "prefetch"] * 2


@pytest.mark.asyncio
async def test_a_failing_prefetch_leaves_the_loop_running(monkeypatch):
    import backend.routes.scan as scan_routes
    calls, sleeps = [], []
    _fakes(monkeypatch, calls, fail=("prefetch",))
    monkeypatch.setattr(scan_routes, "scan_is_actively_running", lambda: False)

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 2:
            raise asyncio.CancelledError
    monkeypatch.setattr(library_sync.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await library_sync.library_sync_loop()
    assert calls.count("prefetch") == 2


@pytest.mark.asyncio
async def test_a_full_scan_reads_them_too(monkeypatch):
    """The scan's own read goes through the same function (one at a time)."""
    import inspect

    import backend.routes.scan as scan_routes
    source = inspect.getsource(scan_routes)
    assert "library_sync import sync_all" in source
    assert "sync_arr_file_status" not in source  # not a second copy of the list
