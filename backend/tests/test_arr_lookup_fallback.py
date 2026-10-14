"""A file under a folder no Sonarr/Radarr path mapping covers ("/media/TV2"
when only "/media/TV" is mapped) still finds its series or movie: by the id
in its folder's name, else by the folder's name (v0.10.0). Request
replacement said "No matching Sonarr series for path"."""
import time

import pytest
import pytest_asyncio

from backend import arr

SERIES = [{"id": 1, "path": "/tv/Love Island (US)", "tvdbId": 364107},
          {"id": 2, "path": "/tv/Northbound", "tvdbId": 5},
          {"id": 3, "path": "/tv2/Love Island (UK)", "tvdbId": 7}]
MOVIES = [{"id": 10, "path": "/movies/The Matrix (1999)", "tmdbId": 603, "imdbId": "tt0133093"},
          {"id": 11, "path": "/movies/Heat (1995)", "tmdbId": 949, "imdbId": "tt0113277"}]


@pytest.fixture(autouse=True)
def lists(monkeypatch):
    monkeypatch.setitem(arr._sonarr_cache, "data", SERIES)
    monkeypatch.setitem(arr._sonarr_cache, "fetched_at", time.monotonic())
    monkeypatch.setitem(arr._radarr_cache, "data", MOVIES)
    monkeypatch.setitem(arr._radarr_cache, "fetched_at", time.monotonic())


@pytest.mark.asyncio
@pytest.mark.parametrize("path, series_id", [
    ("/tv/Northbound/Season 1/Northbound - S01E01.mkv", 2),  # its own path, as before
    ("/media/TV2/RealityTV/Love Island (US) (2019) [tvdb-364107]/Season 5/Love Island USA - S05E16.mkv", 1),
    ("/media/TV2/Northbound/Season 1/Northbound - S01E02.mkv", 2),  # the folder's name
    ("/media/TV2/Unknown Show/Season 1/x.mkv", None),
    ("/media/TV2/Show [tvdb-999]/Season 1/x.mkv", None),
])
async def test_a_series_is_found_without_a_path_mapping(path, series_id):
    found = await arr._find_sonarr_series_for_path(None, "http://sonarr", "k", path)
    assert (found or {}).get("id") == series_id


@pytest.mark.asyncio
@pytest.mark.parametrize("path, movie_id", [
    ("/media/Films/The Matrix (1999) {tmdb-603}/The Matrix.mkv", 10),
    ("/media/Films/Heat (1995) [tt0113277]/Heat.mkv", 11),
    ("/media/Films/Heat (1995)/Heat.mkv", 11),
    ("/media/Films/Other (2001)/Other.mkv", None),
])
async def test_a_movie_is_found_without_a_path_mapping(path, movie_id):
    found = await arr._find_radarr_movie_for_path(None, "http://radarr", "k", path)
    assert (found or {}).get("id") == movie_id


FILE = ("/media/TV2/RealityTV/Love Island (US) (2019) [tvdb-364107]/Season 5/"
        "Love Island USA - S05E16 - Episode 16 - 1080p WEBRip AAC 2.0 x265-Sonarr.mkv")
SONARR_DIR = "/RealityTV/Love Island (US) (2019) [tvdb-364107]"


def _sonarr(monkeypatch, files_before: list[str], files_after: list[str], queue: list | None = None):
    """A Sonarr whose series folder lists `files_before` until it's rescanned."""
    import httpx
    monkeypatch.setattr(arr, "_COMMAND_POLL", 0)
    monkeypatch.setitem(arr._sonarr_cache, "data", [{"id": 1, "path": SONARR_DIR, "tvdbId": 364107}])
    state = {"rescanned": False, "calls": []}

    def handler(request):
        path, method = request.url.path, request.method
        state["calls"].append((method, path))
        names = files_after if state["rescanned"] else files_before
        if path == "/api/v3/episodefile" and method == "GET":
            return httpx.Response(200, json=[{"id": 100 + n, "path": f"{SONARR_DIR}/Season 5/{name}"}
                                             for n, name in enumerate(names)])
        if path == "/api/v3/episode":
            return httpx.Response(200, json=[{"id": 7, "seasonNumber": 5, "episodeNumber": 16,
                                              "episodeFileId": 100 if names else 0}])
        if path == "/api/v3/command" and method == "POST":
            if request.read() and b"RescanSeries" in request.content:
                state["rescanned"] = True
                return httpx.Response(201, json={"id": 55})
            state["searched"] = b"EpisodeSearch" in request.content
            return httpx.Response(201, json={"id": 56})
        if path == "/api/v3/queue/details":
            return httpx.Response(200, json=queue or [])
        if path == "/api/v3/command/55":
            return httpx.Response(200, json={"id": 55, "status": "completed"})
        if path == "/api/v3/history":
            return httpx.Response(200, json={"records": [{"id": 9, "eventType": "grabbed"}]})
        if path.startswith("/api/v3/history/failed/") or method == "DELETE":
            return httpx.Response(200, json={})
        return httpx.Response(404)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    return state


@pytest_asyncio.fixture
async def sonarr_settings(test_db):
    import aiosqlite
    async with aiosqlite.connect(test_db) as db:
        await db.executemany("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", [
            ("sonarr_url", "http://sonarr"), ("sonarr_api_key", "k"),
            ("sonarr_path_mapping", "/media/TV2/RealityTV=/RealityTV")])
        await db.commit()


@pytest.mark.asyncio
async def test_a_stale_sonarr_record_is_rescanned_then_replaced(sonarr_settings, monkeypatch):
    """Sonarr still had the episode under its old name: rescanning the
    series makes its record this file's, and the replacement goes ahead."""
    state = _sonarr(monkeypatch, ["Love Island USA - S05E16 - Episode 16 - 1080p WEBRip x264.mkv"],
                    [FILE.rsplit("/", 1)[1]])
    result = await arr.research_sonarr_file(FILE, delete_file=True)
    assert result["success"] and result["deleted"] and result["searched"] and result["blocklisted"], result
    assert ("DELETE", "/api/v3/episodefile/100") in state["calls"]


@pytest.mark.asyncio
async def test_sonarrs_other_file_for_the_episode_is_left_alone(sonarr_settings, monkeypatch):
    state = _sonarr(monkeypatch, ["Love Island USA - S05E16 - Other.mkv"], ["Love Island USA - S05E16 - Other.mkv"])
    result = await arr.research_sonarr_file(FILE, delete_file=True)
    assert result == {"success": False, "error": "Sonarr's file for S05E16 is a different one: "
                                                 "Love Island USA - S05E16 - Other.mkv"}
    assert not any(method == "DELETE" for method, _ in state["calls"])  # nothing of Sonarr's is deleted


@pytest.mark.asyncio
async def test_an_episode_sonarr_shows_missing_is_searched_for(sonarr_settings, monkeypatch):
    """Sonarr has no record of this file — the episode shows as missing, even
    after a rescan: the replacement is its search. Nothing to blocklist, and
    this file isn't Sonarr's to delete (it said "Sonarr has no file", which
    read as "no releases")."""
    state = _sonarr(monkeypatch, [], [])
    result = await arr.research_sonarr_file(FILE, delete_file=True)
    assert result["success"] and result["searched"] and result["untracked"], result
    assert not result["deleted"] and not result["blocklisted"] and result["episode_ids"] == [7]
    assert not any(method == "DELETE" for method, _ in state["calls"])
    assert state["searched"]


@pytest.mark.asyncio
@pytest.mark.parametrize("files", [[FILE.rsplit("/", 1)[1]], []], ids=["its-file", "no-file"])
async def test_an_episode_already_downloading_says_so(sonarr_settings, monkeypatch, files):
    """A download of the episode already in Sonarr's queue: said, and
    nothing blocklisted, deleted or searched."""
    state = _sonarr(monkeypatch, files, files,
                    queue=[{"episodeId": 7, "title": "Love.Island.US.S05E16.1080p.WEB.h264-EDITH"}])
    result = await arr.research_sonarr_file(FILE, delete_file=True)
    assert result == {"success": False, "error": "S05E16 is already downloading in Sonarr "
                      "(Love.Island.US.S05E16.1080p.WEB.h264-EDITH); this file is replaced when it's imported"}
    assert not [c for c in state["calls"] if c[0] in ("DELETE",) or c[1].startswith("/api/v3/history/failed")]
