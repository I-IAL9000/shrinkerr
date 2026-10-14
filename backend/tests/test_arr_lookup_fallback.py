"""A file under a folder no Sonarr/Radarr path mapping covers ("/media/TV2"
when only "/media/TV" is mapped) still finds its series or movie: by the id
in its folder's name, else by the folder's name (v0.10.0). Request
replacement said "No matching Sonarr series for path"."""
import time

import pytest

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
