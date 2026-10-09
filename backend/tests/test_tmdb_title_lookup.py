"""SC-18: original-language lookups without an id in the path searched the
wrong title (a disc's "BDMV", a season's "Staffel 1", a flat layout's media
folder), took TMDB's first result without comparing titles, cached a rate
limit as "no match" for a day and logged a missing key for every file."""
import aiosqlite
import httpx
import pytest
import pytest_asyncio

import backend.metadata as metadata
from backend.metadata import title_search_name
from backend.routes.posters import parse_folder_name, pick_tmdb_match


@pytest.mark.parametrize("path, roots, folder, tv", [
    ("/m/movies/Saw II (2005)/BDMV/index.bdmv", ["/m/movies"], "/m/movies/Saw II (2005)", False),
    ("/m/movies/Saw II (2005)/VIDEO_TS/VIDEO_TS.IFO", ["/m/movies"], "/m/movies/Saw II (2005)", False),
    ("/m/movies/Saw II (2005)/Disc 1/VIDEO_TS/VIDEO_TS.IFO", ["/m/movies"], "/m/movies/Saw II (2005)", False),
    ("/m/movies/Saw II (2005)/Extras/Making of.mkv", ["/m/movies"], "/m/movies/Saw II (2005)", False),
    ("/m/tv/Dark/S01/Dark.S01E02.mkv", ["/m/tv"], "/m/tv/Dark", True),
    ("/m/tv/Dark/Staffel 1/Dark.S01E02.mkv", ["/m/tv"], "/m/tv/Dark", True),
    ("/m/tv/Dark/Season 01/episode two.mkv", ["/m/tv"], "/m/tv/Dark", True),
    # Flat layout: the media folder is not a title, the file name is.
    ("/m/movies/Saw.II.2005.1080p.BluRay.x264.mkv", ["/m/movies/"], "/m/movies/Saw.II.2005.1080p.BluRay.x264", False),
    ("/m/movies/Saw II (2005).iso", ["/m/movies"], "/m/movies/Saw II (2005)", False),
    # A disc dumped straight into the media folder has no title at all.
    ("/m/movies/VIDEO_TS/VIDEO_TS.IFO", ["/m/movies"], None, False),
])
def test_the_title_comes_from_the_title_folder(path, roots, folder, tv):
    assert title_search_name(path, roots) == (folder, tv)


def test_the_searched_title_is_the_movie_not_the_folder_structure():
    folder, _ = title_search_name("/m/movies/Saw II (2005)/BDMV/index.bdmv", ["/m/movies"])
    assert parse_folder_name(folder, walk_files=False)["title"] == "Saw II"
    folder, _ = title_search_name("/m/movies/Saw.II.2005.1080p.BluRay.x264.mkv", ["/m/movies"])
    meta = parse_folder_name(folder, walk_files=False)
    assert (meta["title"], meta["year"]) == ("Saw II", "2005")


def test_the_match_must_carry_the_title():
    results = [
        {"title": "Saw III", "release_date": "2006-10-27", "original_language": "fr"},
        {"title": "Saw II", "release_date": "2005-10-28", "original_language": "en"},
    ]
    assert pick_tmdb_match(results, "Saw II", "2005")["original_language"] == "en"
    assert pick_tmdb_match(results[:1], "Saw II", "2005") is None
    assert pick_tmdb_match([{"title": "Something Else", "release_date": "2005"}], "Saw II", None) is None


class FakeTMDB:
    """Stands in for httpx.AsyncClient; `answer(url, params)` gives (status, json)."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        status, body = self.answer(url, params or {})
        return httpx.Response(status, json=body, request=httpx.Request("GET", url))


SAW_RESULTS = {"results": [
    {"title": "Saw III", "release_date": "2006-10-27", "original_language": "fr"},
    {"title": "Saw II", "release_date": "2005-10-28", "original_language": "en"},
]}


@pytest_asyncio.fixture
async def lookup_db(test_db, monkeypatch):
    monkeypatch.setattr(metadata, "DB_PATH", test_db)
    monkeypatch.delenv("SHRINKERR_TMDB_API_KEY", raising=False)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO media_dirs (path) VALUES ('/m/movies')")
        await db.execute("INSERT INTO media_dirs (path) VALUES ('/m/tv')")
        await db.execute("INSERT INTO settings (key, value) VALUES ('tmdb_api_key', 'k')")
        await db.commit()
    return test_db


async def _cache(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT id_type, media_id, original_language FROM metadata_cache") as cur:
            return [tuple(r) for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_a_disc_is_looked_up_by_its_title_and_the_match_is_checked(lookup_db, monkeypatch):
    tmdb = FakeTMDB(lambda url, params: (200, SAW_RESULTS))
    monkeypatch.setattr(httpx, "AsyncClient", tmdb)
    lang = await metadata.lookup_original_language("/m/movies/Saw II (2005)/BDMV/index.bdmv")
    assert lang == "eng"  # not Saw III's "fr", which TMDB listed first
    assert tmdb.calls[0][1]["query"] == "Saw II" and tmdb.calls[0][1]["year"] == "2005"
    assert await _cache(lookup_db) == [("title", "saw ii:2005", "eng")]


@pytest.mark.asyncio
async def test_a_flat_file_is_looked_up_by_its_name(lookup_db, monkeypatch):
    tmdb = FakeTMDB(lambda url, params: (200, SAW_RESULTS))
    monkeypatch.setattr(httpx, "AsyncClient", tmdb)
    assert await metadata.lookup_original_language("/m/movies/Saw.II.2005.1080p.BluRay.x264.mkv") == "eng"
    assert tmdb.calls[0][1]["query"] == "Saw II"


@pytest.mark.asyncio
async def test_a_season_folder_searches_the_show_as_tv(lookup_db, monkeypatch):
    tmdb = FakeTMDB(lambda url, params: (200, {"results": [
        {"name": "Dark", "first_air_date": "2017-12-01", "original_language": "de"}]}))
    monkeypatch.setattr(httpx, "AsyncClient", tmdb)
    assert await metadata.lookup_original_language("/m/tv/Dark/Staffel 1/Dark.S01E02.mkv") == "ger"
    url, params = tmdb.calls[0]
    assert url.endswith("/search/tv") and params["query"] == "Dark"


@pytest.mark.asyncio
async def test_no_matching_title_means_no_language(lookup_db, monkeypatch):
    tmdb = FakeTMDB(lambda url, params: (200, {"results": [
        {"title": "Saw III", "release_date": "2006-10-27", "original_language": "fr"}]}))
    monkeypatch.setattr(httpx, "AsyncClient", tmdb)
    assert await metadata.lookup_original_language("/m/movies/Saw II (2005)/movie.mkv") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 503])
async def test_a_rate_limit_or_outage_is_not_cached_as_no_match(lookup_db, monkeypatch, status):
    answers = [(status, {"status_message": "slow down"})]
    tmdb = FakeTMDB(lambda url, params: answers.pop(0) if answers else (200, SAW_RESULTS))
    monkeypatch.setattr(httpx, "AsyncClient", tmdb)
    path = "/m/movies/Saw II (2005)/movie.mkv"
    assert await metadata.lookup_original_language(path) is None
    assert await _cache(lookup_db) == []
    assert await metadata.lookup_original_language(path) == "eng"  # asked again, not a cached miss


@pytest.mark.asyncio
async def test_a_rate_limited_id_lookup_is_not_cached(lookup_db, monkeypatch):
    tmdb = FakeTMDB(lambda url, params: (429, {}))
    monkeypatch.setattr(httpx, "AsyncClient", tmdb)
    assert await metadata.lookup_original_language("/m/movies/Saw II (2005) [tt0432348]/movie.mkv") is None
    assert len(tmdb.calls) == 1  # no title search while rate-limited
    assert await _cache(lookup_db) == []


@pytest.mark.asyncio
async def test_a_missing_key_is_logged_once(lookup_db, monkeypatch, capsys):
    async with aiosqlite.connect(lookup_db) as db:
        await db.execute("DELETE FROM settings WHERE key = 'tmdb_api_key'")
        await db.commit()
    monkeypatch.setattr(metadata, "_no_key_logged", False)
    for name in ("a", "b", "c"):
        assert await metadata.lookup_original_language(f"/m/movies/{name} (2001)/x.mkv") is None
    assert capsys.readouterr().out.count("No TMDB API key") == 1


@pytest.mark.asyncio
async def test_old_title_matches_are_looked_up_again(lookup_db, monkeypatch):
    rows = [
        ("/m/movies/Saw II (2005)/BDMV/index.bdmv", "api"),        # title search: recheck
        ("/m/movies/Saw II (2005) [tt0432348]/movie.mkv", "api"),  # id: keep
        ("/m/movies/Mine (2016)/movie.mkv", "manual"),             # the user's choice: keep
    ]
    async with aiosqlite.connect(lookup_db) as db:
        for path, source in rows:
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, native_language, language_source, scan_timestamp) "
                "VALUES (?, 1, 'fre', ?, '2026-01-01')", (path, source))
        await db.execute(
            "INSERT INTO metadata_cache (id_type, media_id, original_language, looked_up_at) "
            "VALUES ('title', 'bdmv:', 'fre', '2026-01-01T00:00:00+00:00'), "
            "('imdb', 'tt0432348', 'eng', '2026-01-01T00:00:00+00:00')")
        await db.commit()

    assert await metadata.recheck_title_matches() == 1
    async with aiosqlite.connect(lookup_db) as db:
        async with db.execute("SELECT file_path, native_language, language_source FROM scan_results ORDER BY id") as cur:
            got = [tuple(r) for r in await cur.fetchall()]
    assert got == [
        (rows[0][0], "fre", "heuristic"),
        (rows[1][0], "fre", "api"),
        (rows[2][0], "fre", "manual"),
    ]
    assert await _cache(lookup_db) == [("imdb", "tt0432348", "eng")]
    assert await metadata.recheck_title_matches() == 0  # once
