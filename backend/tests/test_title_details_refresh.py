"""Every matched title's country, genres and year from its own TMDB record
(v0.10.0, DETAILS_VERSION 2), for the Scanner's Title lists.

Countries came only from TMDB's search results, which give a TV show's but
not a film's; titles matched through Plex got no details at all; and titles
cached before were never looked at again. Now the details request (one per
title) gives films and shows their country, genres and year, the poster
prefetch (every few hours) fetches them once for every matched title, and
TMDB's combined TV genres are split: "Action & Adventure" is Action and
Adventure, as the films' genres name them."""
import aiosqlite
import httpx
import pytest


@pytest.fixture
def posters(test_db, monkeypatch):
    import backend.imdb_ratings as imdb_ratings
    import backend.media_paths as media_paths
    import backend.routes.posters as posters
    monkeypatch.setattr(posters, "DB_PATH", test_db)
    monkeypatch.setattr(media_paths, "DB_PATH", test_db)
    media_paths.invalidate_media_dir_cache()
    monkeypatch.setattr(imdb_ratings, "_ratings", {"tt0000001": {"rating": 8.2, "votes": 99}})
    monkeypatch.setenv("SHRINKERR_TMDB_API_KEY", "test-key")

    async def no_image(*a, **kw):
        return None
    monkeypatch.setattr(posters, "_download_image", no_image)
    return posters


MOVIE = {"imdb_id": "tt0000001", "release_date": "1999-03-31", "origin_country": ["US", "LU"],
         "production_countries": [{"iso_3166_1": "LU", "name": "Luxembourg"},
                                  {"iso_3166_1": "US", "name": "United States of America"}],
         "genres": [{"id": 28, "name": "Action"}, {"id": 878, "name": "Science Fiction"},
                    {"id": 10759, "name": "Action & Adventure"}]}
SHOW = {"first_air_date": "2008-01-20", "origin_country": ["US"], "networks": [{"name": "AMC"}], "status": "Ended",
        "genres": [{"id": 10765, "name": "Sci-Fi & Fantasy"}, {"id": 18, "name": "Drama"},
                   {"id": 10768, "name": "War & Politics"}],
        "external_ids": {"imdb_id": "tt0000003"}}


@pytest.mark.asyncio
async def test_a_title_record_gives_its_country_genres_and_year(posters, monkeypatch):
    asked = []
    records = {
        "movie/1": MOVIE, "tv/3": SHOW,
        # No origin country: its production countries, named as TMDB names
        # them when the code isn't one we name. Its IMDb id in external_ids.
        "movie/2": {"production_countries": [{"iso_3166_1": "GB", "name": "United Kingdom"},
                                             {"iso_3166_1": "XE", "name": "Elbonia"},
                                             {"iso_3166_1": "FR", "name": "France"}],
                    "genres": [{"id": 99999, "name": "Mockumentary"}], "external_ids": {"imdb_id": "tt0000002"}},
        "movie/4": {},
        # All of a title's genres, not the first three.
        "movie/7": {"genres": [{"id": g} for g in (18, 35, 10749, 10402, 10759)]},
    }

    async def get(client, url, params):
        path = url.split("/3/")[1]
        asked.append((path, params.get("append_to_response")))
        if path == "movie/6":
            return httpx.Response(404, json={"status_code": 34})
        return httpx.Response(200, json=records[path]) if path in records else httpx.Response(429, json={})
    monkeypatch.setattr(posters, "_tmdb_get", get)
    assert await posters._tmdb_details(1, "movie", "k") == {
        "imdb_id": "tt0000001", "country": "United States, Luxembourg", "genres": "Action, Sci-Fi, Adventure",
        "year": "1999"}
    assert await posters._tmdb_details(2, "movie", "k") == {
        "imdb_id": "tt0000002", "country": "Great Britain, Elbonia", "genres": "Mockumentary", "year": ""}
    assert await posters._tmdb_details(3, "tv", "k") == {
        "network": "AMC", "status": "Ended", "imdb_id": "tt0000003", "country": "United States",
        "genres": "Sci-Fi, Fantasy, Drama, War, Politics", "year": "2008"}
    # TMDB has none of them: "" (stored, not asked again). Failed: {} (asked again).
    assert await posters._tmdb_details(4, "movie", "k") == {"imdb_id": "", "country": "", "genres": "", "year": ""}
    assert await posters._tmdb_details(5, "movie", "k") == {}
    # Not found (a deleted or merged entry): none, for good — not asked every few hours.
    assert await posters._tmdb_details(6, "movie", "k") == {"imdb_id": "", "country": "", "genres": "", "year": ""}
    assert (await posters._tmdb_details(7, "movie", "k"))["genres"] == "Drama, Comedy, Romance, Music, Action, Adventure"
    assert asked[:3] == [("movie/1", "external_ids"), ("movie/2", "external_ids"), ("tv/3", "external_ids")]


@pytest.mark.asyncio
async def test_a_lookup_tmdb_didnt_answer_isnt_no_match(posters, monkeypatch):
    """The backfill marks a title TMDB doesn't find as done: a lookup that
    was rate-limited (or TMDB was down) mustn't look like one."""
    status = {"code": 429}

    async def get(client, url, params):
        return httpx.Response(status["code"], json={"results": [], "movie_results": [], "tv_results": []})
    monkeypatch.setattr(posters, "_tmdb_get", get)
    for lookup in (lambda: posters._resolve_tmdb("tt0000001", "k"), lambda: posters._resolve_tmdb_tvdb("1", "k"),
                   lambda: posters._resolve_tmdb_search("Film", None, "k", media_type_hint="movie")):
        status["code"] = 429
        with pytest.raises(RuntimeError):
            await lookup()
        status["code"] = 200
        assert await lookup() == (None, "placeholder", {})


@pytest.mark.asyncio
async def test_the_poster_grid_still_tries_the_next_way(posters, test_db, monkeypatch):
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')", (posters._V116_PURGE_FLAG,))
        await db.commit()

    async def get(client, url, params):
        if "/find/" in url:
            return httpx.Response(429, json={})
        if "/search/" in url:
            return httpx.Response(200, json={"results": [{"id": 5, "title": "Film", "release_date": "2001-01-01"}]})
        return httpx.Response(200, json=MOVIE)
    monkeypatch.setattr(posters, "_tmdb_get", get)
    path = "/m/movies/Film (2001) [tt0000001]"
    got = (await posters.resolve_posters(posters.ResolveRequest(paths=[path])))[path]
    assert (got["source"], got["country"]) == ("tmdb", "United States, Luxembourg")


def test_search_results_store_split_genres_too():
    from backend.routes.posters import _extract_tmdb_meta
    meta = _extract_tmdb_meta({"id": 7, "genre_ids": [10759, 28, 10765, 99], "origin_country": ["IS"]}, "tv", "k")
    assert (meta["genres"], meta["country"]) == ("Action, Adventure, Sci-Fi, Fantasy, Documentary", "Iceland")
    assert _extract_tmdb_meta({"id": 8, "genre_ids": [12345]}, "movie", "k")["genres"] is None


@pytest.mark.asyncio
async def test_details_fill_in_a_match_and_say_so(posters, monkeypatch):
    calls = []

    async def details(tmdb_id, media_type, key):
        calls.append(tmdb_id)
        return {1: {"imdb_id": "", "country": "", "genres": "Drama", "year": "2001"},
                2: {"imdb_id": "tt0000009", "country": "France", "genres": "", "year": ""}}.get(tmdb_id, {})
    monkeypatch.setattr(posters, "_tmdb_details", details)
    # A film named with its IMDb id gets its details too; the folder's id wins.
    film = await posters._with_details({"media_type": "movie", "tmdb_id": 2}, "k", "tt0000001")
    assert (film["imdb_id"], film["country"], film["details_v"]) == ("tt0000001", "France", posters.DETAILS_VERSION)
    # The search result's country isn't replaced by none.
    show = await posters._with_details({"media_type": "tv", "tmdb_id": 1, "country": "Iceland"}, "k")
    assert (show["country"], show["genres"], show["year"]) == ("Iceland", "Drama", "2001")
    failed = await posters._with_details({"media_type": "movie", "tmdb_id": 3}, "k", "tt0000001")
    assert "details_v" not in failed and failed["imdb_id"] == "tt0000001"
    assert calls == [2, 1, 3]


async def _rows(db_path, columns="country, genres, year, details_v"):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(f"SELECT folder_path, {columns} FROM poster_cache ORDER BY folder_path") as cur:
            return [tuple(r) for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_new_matches_store_their_details(posters, test_db, monkeypatch):
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')", (posters._V116_PURGE_FLAG,))
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES "
                         "('/m/movies/Prefetched/film.mkv', 1, '2026-10-12')")
        await db.commit()

    async def search(title, year, key, media_type_hint=None, **kw):
        return "https://img/x.jpg", "tmdb", {"media_type": "movie", "tmdb_id": {"Film": 1, "Prefetched": 2}[title]}

    async def details(tmdb_id, media_type, key):
        return {1: {"imdb_id": "", "country": "Iceland", "genres": "Drama", "year": "2004"}}.get(tmdb_id, {})
    monkeypatch.setattr(posters, "_resolve_tmdb_search", search)
    monkeypatch.setattr(posters, "_tmdb_details", details)
    got = (await posters.resolve_posters(posters.ResolveRequest(paths=["/m/movies/Film"])))["/m/movies/Film"]
    assert (got["country"], got["genres"], got["year"]) == ("Iceland", "Drama", "2004")  # no year in its name: TMDB's

    async def no_backfill(*a):
        return 0
    monkeypatch.setattr(posters, "_backfill_details", no_backfill)
    await posters._run_prefetch()
    # Details that failed: 0, for the backfill to fetch.
    assert await _rows(test_db) == [("/m/movies/Film", "Iceland", "Drama", "2004", posters.DETAILS_VERSION),
                                    ("/m/movies/Prefetched", None, None, None, 0)]


@pytest.mark.asyncio
async def test_a_manual_match_stores_its_details(posters, test_db, monkeypatch):
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('tmdb_api_key', 'k')")
        await db.commit()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"name": "Show", **SHOW}))))
    got = await posters.override_poster(posters.OverrideRequest(folder_path="/m/tv/Show", tmdb_id=3, media_type="tv"))
    assert (got["genres"], got["country"]) == ("Sci-Fi, Fantasy, Drama, War, Politics", "United States")
    assert await _rows(test_db) == [("/m/tv/Show", "United States", "Sci-Fi, Fantasy, Drama, War, Politics", "2008",
                                     posters.DETAILS_VERSION)]


@pytest.mark.asyncio
async def test_titles_cached_before_get_their_details_once(posters, test_db, monkeypatch):
    rows = [
        # folder, title, year, source, media_type, country, genres, rating, details_v
        ("/m/tv/Show/Season 1", "Show", None, "tmdb", "tv", None, "Action & Adventure", 7.0, 0),
        ("/m/tv/Show/Season 2", "Show", None, "tmdb", "tv", None, "Action & Adventure", 7.0, 0),
        ("/m/movies/Film (1995) [tt0000001]", "Film", "1995", "tmdb", "movie", "France", "Drama", 5.0, 0),
        ("/m/movies/Plexed (2005)", "Plexed", "2005", "plex", "movie", None, None, None, None),
        ("/m/movies/Unknown (2010)", "Unknown", "2010", "tmdb", "movie", None, None, None, 0),
        ("/m/movies/Flaky (2011)", "Flaky", "2011", "tmdb", "movie", None, None, None, 0),
        ("/m/movies/Broken (2012)", "Broken", "2012", "tmdb", "movie", None, None, None, 0),
        ("/m/movies/Done (2000)", "Done", "2000", "tmdb", "movie", "Spain", "Drama", None, posters.DETAILS_VERSION),
        ("/m/movies/Nothing", "Nothing", None, "placeholder", None, None, None, None, 0),
    ]
    async with aiosqlite.connect(test_db) as db:
        await db.executemany(
            "INSERT INTO poster_cache (folder_path, title, year, source, media_type, country, genres, rating, "
            "details_v, image_data, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'aW1n', '2026-01-01')", rows)
        await db.commit()
    searched, found, asked = [], [], []
    flaky = {"fail": True}

    async def search(title, year, key, media_type_hint=None, **kw):
        searched.append(title)
        if title == "Broken":
            raise RuntimeError("TMDB returned 503")
        ids = {"Show": 3, "Plexed": 5, "Flaky": 6}
        if title not in ids:
            return None, "placeholder", {}  # TMDB doesn't know it
        return "u", "tmdb", {"media_type": media_type_hint or "movie", "tmdb_id": ids[title], "rating": 6.5}

    async def find(imdb_id, key):
        found.append(imdb_id)
        return "u", "tmdb", {"media_type": "movie", "tmdb_id": 1}

    async def details(tmdb_id, media_type, key):
        asked.append(tmdb_id)
        if tmdb_id == 6 and flaky["fail"]:
            return {}  # rate-limited
        return {1: {"imdb_id": "tt0000001", "country": "", "genres": "", "year": "1996"},
                3: {"imdb_id": "", "country": "United States", "genres": "Action, Adventure", "year": "2008",
                    "network": "AMC", "status": "Ended"},
                5: {"imdb_id": "tt0000005", "country": "Germany", "genres": "Comedy", "year": "2005"},
                6: {"imdb_id": "", "country": "Japan", "genres": "Horror", "year": "2011"}}[tmdb_id]
    monkeypatch.setattr(posters, "_resolve_tmdb_search", search)
    monkeypatch.setattr(posters, "_resolve_tmdb", find)
    monkeypatch.setattr(posters, "_tmdb_details", details)

    assert await posters._backfill_details("test-key", lambda p: None) == 5
    assert sorted(searched) == ["Broken", "Flaky", "Plexed", "Show", "Unknown"]  # the show once, for both seasons
    assert found == ["tt0000001"] and sorted(asked) == [1, 3, 5, 6]
    v = posters.DETAILS_VERSION
    assert await _rows(test_db, "country, genres, year, network, status, rating, details_v, image_data") == [
        # Never replaced by none; the year in its name kept; the IMDb rating.
        ("/m/movies/Broken (2012)", None, None, "2012", None, None, None, 0, "aW1n"),     # failed: next time
        ("/m/movies/Done (2000)", "Spain", "Drama", "2000", None, None, None, v, "aW1n"),  # done before
        ("/m/movies/Film (1995) [tt0000001]", "France", "Drama", "1995", None, None, 8.2, v, "aW1n"),
        ("/m/movies/Flaky (2011)", None, None, "2011", None, None, None, 0, "aW1n"),      # failed: next time
        ("/m/movies/Nothing", None, None, None, None, None, None, 0, "aW1n"),             # no match
        ("/m/movies/Plexed (2005)", "Germany", "Comedy", "2005", None, None, 6.5, v, "aW1n"),  # TMDB's rating
        ("/m/movies/Unknown (2010)", None, None, "2010", None, None, None, v, "aW1n"),   # not found: done
        ("/m/tv/Show/Season 1", "United States", "Action, Adventure", "2008", "AMC", "Ended", 7.0, v, "aW1n"),
        ("/m/tv/Show/Season 2", "United States", "Action, Adventure", "2008", "AMC", "Ended", 7.0, v, "aW1n"),
    ]
    flaky["fail"] = False
    searched.clear()
    assert await posters._backfill_details("test-key", lambda p: None) == 1
    assert sorted(searched) == ["Broken", "Flaky"]
    assert (await _rows(test_db))[3] == ("/m/movies/Flaky (2011)", "Japan", "Horror", "2011", v)


@pytest.mark.asyncio
async def test_a_fix_match_keeps_its_own_imdb_id(posters, test_db, monkeypatch):
    """A Fix match made before its details were stored: found by the IMDb id
    the user picked (else its title) — never the folder's, often why it was
    fixed."""
    async with aiosqlite.connect(test_db) as db:
        await db.executemany(
            "INSERT INTO poster_cache (folder_path, title, year, source, media_type, imdb_id) "
            "VALUES (?, ?, ?, 'tmdb-manual', 'movie', ?)",
            [("/m/movies/Wrong Name (1999) [tt0000099]", "The Pick", "1999", "tt0000007"),
             ("/m/movies/Other Name [tt0000098]", "Another Pick", "2001", None)])
        await db.commit()
    found, searched = [], []

    async def find(imdb_id, key):
        found.append(imdb_id)
        return "u", "tmdb", {"media_type": "movie", "tmdb_id": 7} if imdb_id == "tt0000007" else {}

    async def search(title, year, key, media_type_hint=None, **kw):
        searched.append((title, year, media_type_hint))
        return "u", "tmdb", {"media_type": "movie", "tmdb_id": 8}

    async def details(tmdb_id, media_type, key):
        return {"imdb_id": f"tt000000{tmdb_id}", "country": "France", "genres": "Drama", "year": ""}
    monkeypatch.setattr(posters, "_resolve_tmdb", find)
    monkeypatch.setattr(posters, "_resolve_tmdb_search", search)
    monkeypatch.setattr(posters, "_tmdb_details", details)
    assert await posters._backfill_details("test-key", lambda p: None) == 2
    assert found == ["tt0000007"] and searched == [("Another Pick", "2001", "movie")]
    assert await _rows(test_db, "imdb_id, country") == [("/m/movies/Other Name [tt0000098]", "tt0000008", "France"),
                                                        ("/m/movies/Wrong Name (1999) [tt0000099]", "tt0000007",
                                                         "France")]


@pytest.mark.asyncio
async def test_a_title_changed_while_the_backfill_runs_keeps_its_change(posters, test_db, monkeypatch):
    """The first run takes a while: a Fix match, or a title found again,
    meanwhile isn't overwritten with what was looked up before."""
    async with aiosqlite.connect(test_db) as db:
        await db.executemany(
            "INSERT INTO poster_cache (folder_path, title, source, media_type, resolved_at) "
            "VALUES (?, ?, 'tmdb', 'movie', '2026-01-01')",
            [("/m/movies/Fixed", "Fixed"), ("/m/movies/Again", "Again"), ("/m/movies/Same", "Same")])
        await db.commit()
    changed = []

    async def search(title, year, key, media_type_hint=None, **kw):
        return "u", "tmdb", {"media_type": "movie", "tmdb_id": len(title)}

    async def details(tmdb_id, media_type, key):
        if not changed:  # the rows were read: now the user fixes one, the Poster grid finds another again
            changed.append(1)
            async with aiosqlite.connect(test_db) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO poster_cache (folder_path, title, source, media_type, country, "
                    "details_v, resolved_at) VALUES ('/m/movies/Fixed', 'Picked', 'tmdb-manual', 'movie', 'Spain', "
                    "?, '2026-10-12')", (posters.DETAILS_VERSION,))
                await db.execute(
                    "INSERT OR REPLACE INTO poster_cache (folder_path, title, source, media_type, resolved_at) "
                    "VALUES ('/m/movies/Again', 'Again', 'tmdb', 'movie', '2026-10-12')")
                await db.commit()
        return {"imdb_id": "", "country": "Iceland", "genres": "Drama", "year": ""}
    monkeypatch.setattr(posters, "_resolve_tmdb_search", search)
    monkeypatch.setattr(posters, "_tmdb_details", details)
    assert await posters._backfill_details("test-key", lambda p: None) == 1
    assert await _rows(test_db, "source, country, details_v") == [
        ("/m/movies/Again", "tmdb", None, 0), ("/m/movies/Fixed", "tmdb-manual", "Spain", posters.DETAILS_VERSION),
        ("/m/movies/Same", "tmdb", "Iceland", posters.DETAILS_VERSION)]


async def _library(db_path, rows, plex=False):
    """A file in each folder of `rows` (folder, source, poster_url, image_data,
    days since it was resolved), and their poster_cache rows; "New" has none."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(db_path) as db:
        await db.executemany("INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, 1, 'x')",
                             [(f"/m/movies/{r[0]}/a.mkv",) for r in [("New",), *rows]])
        await db.executemany(
            "INSERT INTO poster_cache (folder_path, title, source, poster_url, image_data, country, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, 'France', ?)",
            [(f"/m/movies/{name}", name, source, url, image, (now - timedelta(days=days)).isoformat())
             for name, source, url, image, days in rows])
        if plex:
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('plex_url', 'http://plex'), "
                             "('plex_token', 't')")
        # Plex matches made before were looked up again once (test_title_matching).
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('_v0_10_0_plex_poster_rows_purged', '1')")
        await db.commit()


PREFETCH_ROWS = [
    ("Old Miss", "placeholder", None, None, 8), ("Fresh Miss", "placeholder", None, None, 1),
    ("Picked", "tmdb-manual", None, None, 0), ("Plexed", "plex", None, None, 0),
    ("Home Video", "other-skipped", None, None, 0), ("Imaged", "tmdb", "https://img/i.jpg", "aW1n", 0),
    ("Posterless", "tmdb", None, None, 0), ("Unloaded", "tmdb", "https://img/u.jpg", None, 0),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled,plex,expected", [
    # Every few hours: a user's Fix match, an Other folder's row, a Plex match,
    # a match TMDB has no poster for and a title not found in the last week
    # are left alone — and with Plex, every title not found: the Poster grid
    # tries those again after a week, Plex first.
    (True, False, ["New", "Old Miss"]),
    (True, True, ["New"]),
    # Refresh Posters (and a scan's): every title without a poster, as before.
    (False, False, ["Fresh Miss", "New", "Old Miss", "Posterless"]),
    (False, True, ["Fresh Miss", "New", "Old Miss", "Posterless"]),
])
async def test_what_the_prefetch_resolves(posters, test_db, monkeypatch, scheduled, plex, expected):
    await _library(test_db, PREFETCH_ROWS, plex=plex)
    searched, downloaded = [], []

    async def search(title, year, key, media_type_hint=None, **kw):
        searched.append(title)
        return "https://img/x.jpg", "tmdb", {"media_type": "movie", "tmdb_id": 1}

    async def details(tmdb_id, media_type, key):
        return {"imdb_id": "", "country": "Iceland", "genres": "", "year": ""}

    async def download(url, plex_url="", plex_token=""):
        downloaded.append(url)
        return "dW5s" if url == "https://img/u.jpg" else None
    backfills = []

    async def backfill(*a):
        backfills.append(a)
        return 0
    plexed = []  # asked for each title's poster, as the Poster grid does: Plex has none of them

    async def plex_search(self, query, scope):
        plexed.append(query)
        return []

    async def plex_libraries(url, token):
        return []
    import backend.plex as plex_mod
    monkeypatch.setattr(plex_mod, "get_plex_libraries", plex_libraries)
    monkeypatch.setattr(posters._PlexLookup, "search", plex_search)
    monkeypatch.setattr(posters, "_resolve_tmdb_search", search)
    monkeypatch.setattr(posters, "_tmdb_details", details)
    monkeypatch.setattr(posters, "_download_image", download)
    monkeypatch.setattr(posters, "_backfill_details", backfill)
    await posters._run_prefetch(scheduled=scheduled)
    assert sorted(searched) == expected
    assert sorted(set(plexed)) == (expected if plex else [])
    assert len(backfills) == 1
    rows = {r[0].rsplit("/", 1)[1]: r[1:] for r in await _rows(test_db, "source, country, image_data")}
    # A match whose image didn't download gets only its image, not looked up again.
    assert "https://img/u.jpg" in downloaded and rows["Unloaded"] == ("tmdb", "France", "dW5s")
    for name, source, _, image, _ in PREFETCH_ROWS:
        if name not in expected and name != "Unloaded":
            assert rows[name] == (source, "France", image), name
    for name in expected:
        assert rows[name][:2] == ("tmdb", "Iceland"), name


@pytest.mark.asyncio
async def test_a_lookup_that_fails_writes_nothing(posters, test_db, monkeypatch):
    """A rate limit (or TMDB down) turned a matched title into a placeholder
    for a week, its country, genres and IMDb id gone."""
    await _library(test_db, [("Posterless", "tmdb", None, None, 0), ("Old Miss", "placeholder", None, None, 8)])
    before = await _rows(test_db, "source, country, resolved_at")

    async def search(title, year, key, media_type_hint=None, **kw):
        raise RuntimeError("TMDB returned 503")

    async def backfill(*a):
        return 0
    monkeypatch.setattr(posters, "_resolve_tmdb_search", search)
    monkeypatch.setattr(posters, "_backfill_details", backfill)
    await posters._run_prefetch()
    assert await _rows(test_db, "source, country, resolved_at") == before  # "New" too: no row, tried next time


@pytest.mark.asyncio
async def test_a_prefetch_asked_for_while_one_runs_runs_once_more(posters, monkeypatch):
    """A scan's new titles waited up to 3 hours when the scheduled run was
    going as the scan ended."""
    import asyncio
    runs, release = [], asyncio.Event()

    async def run(scheduled=False):
        runs.append(scheduled)
        if len(runs) == 1:
            await release.wait()
    monkeypatch.setattr(posters, "_run_prefetch", run)
    monkeypatch.setattr(posters, "_prefetch_task", None)
    monkeypatch.setattr(posters, "_prefetch_again", None)
    assert (await posters.start_prefetch(scheduled=True))["status"] == "started"
    await asyncio.sleep(0)
    assert (await posters.start_prefetch())["status"] == "already_running"  # the scan's
    assert (await posters.start_prefetch(scheduled=True))["status"] == "already_running"
    release.set()
    await posters._prefetch_task
    assert runs == [True, False]  # once more, with Refresh's rules (a scheduled run's skip less)
    assert (await posters.start_prefetch(scheduled=True))["status"] == "started"
    await posters._prefetch_task
    assert runs == [True, False, True]


@pytest.mark.asyncio
async def test_shutdown_stops_the_prefetch(posters, monkeypatch):
    import asyncio
    from pathlib import Path
    started = asyncio.Event()

    async def run(scheduled=False):
        started.set()
        await asyncio.sleep(3600)
    monkeypatch.setattr(posters, "_run_prefetch", run)
    monkeypatch.setattr(posters, "_prefetch_task", None)
    monkeypatch.setattr(posters, "_prefetch_again", None)
    await posters.start_prefetch()
    await started.wait()
    await posters.start_prefetch()  # one more waiting
    posters.stop_prefetch()
    with pytest.raises(asyncio.CancelledError):
        await posters._prefetch_task
    assert posters._prefetch_again is None
    main = (Path(posters.__file__).parents[1] / "main.py").read_text()
    assert "stop_prefetch()" in main.split("yield", 1)[1]  # at shutdown


@pytest.mark.asyncio
async def test_without_a_tmdb_key_the_prefetch_stores_nothing(posters, test_db, monkeypatch):
    """It can't find anything without one (it doesn't ask Plex): placeholders
    would keep the Poster grid from asking Plex for a week."""
    monkeypatch.delenv("SHRINKERR_TMDB_API_KEY")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES ('/m/a/b.mkv', 1, 'x')")
        await db.commit()
    backfills = []

    async def backfill(*a):
        backfills.append(a)
        return 0
    monkeypatch.setattr(posters, "_backfill_details", backfill)
    await posters._run_prefetch()
    assert await _rows(test_db) == [] and backfills == []
    assert posters._prefetch_progress["status"] == "done"


def test_combined_genres_are_single_genres_in_the_filters():
    """Rows cached before, and the media servers' genres, split at once."""
    from backend.scan_filters import FACETS, parse_filter
    ctx = {"title_meta": {"/m/TV/Show": (None, None, "Action & Adventure, Drama", None, None, None),
                          "/m/Films/Film": (None, None, "Science Fiction", None, None, None)},
           "server_genres": {"/m/Films/Other/": {"sci-fi & fantasy", "war & politics"}}}
    show, film, other = ({"file_path": p} for p in ("/m/TV/Show/e1.mkv", "/m/Films/Film/f.mkv",
                                                     "/m/Films/Other/o.mkv"))
    assert set(FACETS["genre"].values(dict(show), ctx)) == {"action", "adventure", "drama"}
    assert set(FACETS["genre"].values(dict(film), ctx)) == {"sci-fi"}
    assert set(FACETS["genre"].values(dict(other), ctx)) == {"sci-fi", "fantasy", "war", "politics"}
    action, scifi = parse_filter("genre:action"), parse_filter("genre:sci-fi")
    assert action.post_filter(dict(show), ctx) and not action.post_filter(dict(film), ctx)
    assert scifi.post_filter(dict(film), ctx) and scifi.post_filter(dict(other), ctx)


def test_saved_genre_conditions_read_genres_as_they_are_kept():
    """A saved "genre:science fiction" or "genre:action & adventure" token,
    or an Advanced Search genre condition, matched nothing once genres were
    kept split: a combined genre is the titles with all its parts."""
    from backend.scan_filters import encode_advanced, parse_filter
    ctx = {"title_meta": {"/m/TV/Show": (None, None, "Action & Adventure, Drama", None, None, None),
                          "/m/Films/Film": (None, None, "Science Fiction", None, None, None),
                          "/m/Films/Action": (None, None, "Action", None, None, None)}}
    rows = {name: {"file_path": f"{folder}/x.mkv"} for name, folder in
            (("show", "/m/TV/Show"), ("film", "/m/Films/Film"), ("action", "/m/Films/Action"))}

    def matching(text):
        expr = parse_filter(text)
        return {name for name, row in rows.items() if expr.post_filter(dict(row), ctx)}

    def adv(op, value):
        return encode_advanced([{"property": "genre", "op": op, "value": value}])
    assert [f.id for g in parse_filter("genre:science fiction").groups for f in g] == ["genre:sci-fi"]
    assert matching("genre:science fiction") == {"film"}
    assert matching("genre:action%20%26%20adventure") == {"show"}  # as the browser encodes it
    assert matching("genre:action") == {"show", "action"}
    assert matching("genre:drama,genre:action%20%26%20adventure") == {"show"}
    assert matching("!genre:action%20%26%20adventure") == {"film", "action"}
    assert matching(adv("eq", "Science Fiction")) == {"film"}
    assert matching(adv("eq", "Action & Adventure")) == {"show"}
    assert matching(adv("ne", "Action & Adventure")) == {"film", "action"}
    assert matching(adv("in", ["Action & Adventure", "Science Fiction"])) == {"show", "film"}
    assert matching(adv("contains", "Action & Adventure")) == {"show"}
    assert matching(adv("contains", "sci")) == {"film"}
