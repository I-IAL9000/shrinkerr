"""IMDb ids from TMDB (v0.10.0).

The IMDb link and the IMDb rating came only from a [ttN] in the folder name,
so a library named without ids linked to an IMDb search and showed TMDB's
rating labelled "IMDb". Now the poster cache stores each title's IMDb id —
the folder's, else TMDB's — and titles cached before are looked up once."""
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
    monkeypatch.setattr(imdb_ratings, "_ratings", {"tt0000005": {"rating": 7.9, "votes": 1234},
                                                   "tt0000001": {"rating": 8.2, "votes": 99}})
    monkeypatch.setenv("SHRINKERR_TMDB_API_KEY", "test-key")

    async def no_image(*a, **kw):
        return None
    monkeypatch.setattr(posters, "_download_image", no_image)
    return posters


async def _rows(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT folder_path, imdb_id, rating FROM poster_cache ORDER BY folder_path") as cur:
            return [tuple(r) for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_tmdb_gives_movies_and_shows_their_imdb_id(posters, monkeypatch):
    asked = []

    async def get(client, url, params):
        asked.append((url.split("/3/")[1], params.get("append_to_response")))
        if url.endswith("/tv/2"):
            return httpx.Response(200, json={"networks": [{"name": "HBO"}], "status": "Ended",
                                             "external_ids": {"imdb_id": "tt0000002"}})
        if url.endswith("/movie/3/external_ids"):
            return httpx.Response(200, json={"imdb_id": None})
        if url.endswith("/movie/1/external_ids"):
            return httpx.Response(200, json={"imdb_id": "tt0000001"})
        return httpx.Response(404, json={})
    monkeypatch.setattr(posters, "_tmdb_get", get)
    assert await posters._tmdb_details(1, "movie", "k") == {"imdb_id": "tt0000001"}
    assert await posters._tmdb_details(2, "tv", "k") == {"network": "HBO", "status": "Ended", "imdb_id": "tt0000002"}
    assert await posters._tmdb_details(3, "movie", "k") == {"imdb_id": ""}  # TMDB has none
    assert await posters._tmdb_details(4, "movie", "k") == {}  # failed: not "none"
    assert asked[:2] == [("movie/1/external_ids", None), ("tv/2", "external_ids")]  # one request each


@pytest.mark.asyncio
async def test_a_folder_named_without_an_id_gets_tmdbs(posters, test_db, monkeypatch):
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')", (posters._V116_PURGE_FLAG,))
        await db.commit()

    async def search(title, year, key, media_type_hint=None):
        return "https://img/x.jpg", "tmdb", {"media_type": "movie", "tmdb_id": 5, "rating": 6.1}

    async def details(tmdb_id, media_type, key):
        return {"imdb_id": f"tt000000{tmdb_id}"}
    monkeypatch.setattr(posters, "_resolve_tmdb_search", search)
    monkeypatch.setattr(posters, "_tmdb_details", details)
    path = "/m/movies/Film (2001)"
    got = (await posters.resolve_posters(posters.ResolveRequest(paths=[path])))[path]
    assert (got["imdb_id"], got["rating"], got["votes"], got["rating_source"]) == ("tt0000005", 7.9, 1234, "imdb")
    assert await _rows(test_db) == [(path, "tt0000005", 7.9)]
    cached = (await posters.resolve_posters(posters.ResolveRequest(paths=[path])))[path]
    assert (cached["imdb_id"], cached["rating"], cached["rating_source"]) == ("tt0000005", 7.9, "imdb")


@pytest.mark.asyncio
async def test_a_tmdb_rating_is_labelled_tmdb(posters, test_db):
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')", (posters._V116_PURGE_FLAG,))
        await db.executemany(
            "INSERT INTO poster_cache (folder_path, title, source, media_type, rating, image_data, imdb_id) "
            "VALUES (?, 'x', 'tmdb', 'movie', ?, 'aW1n', ?)",
            [("/m/movies/No Id", 6.1, ""), ("/m/movies/Named [tt0000001]", 8.0, None)])
        await db.commit()
    got = await posters.resolve_posters(posters.ResolveRequest(paths=["/m/movies/No Id", "/m/movies/Named [tt0000001]"]))
    assert (got["/m/movies/No Id"]["rating"], got["/m/movies/No Id"]["rating_source"]) == (6.1, "tmdb")
    assert got["/m/movies/No Id"]["imdb_id"] is None
    named = got["/m/movies/Named [tt0000001]"]  # cached before the id was stored: the name's
    assert (named["imdb_id"], named["rating"], named["rating_source"]) == ("tt0000001", 8.2, "imdb")


@pytest.mark.asyncio
async def test_titles_cached_before_are_looked_up_once(posters, test_db, monkeypatch):
    async with aiosqlite.connect(test_db) as db:
        await db.executemany(
            "INSERT INTO poster_cache (folder_path, title, year, source, media_type, rating) VALUES (?, ?, ?, ?, ?, 5.0)",
            [("/m/movies/Film (2001)", "Film", "2001", "tmdb", "movie"),
             ("/m/movies/Named [tt0000001]", "Named", None, "tmdb", "movie"),
             ("/m/movies/Picked", "The Pick", "1999", "tmdb-manual", "movie"),
             ("/m/movies/Unknown (2010)", "Unknown", "2010", "tmdb", "movie"),
             ("/m/movies/Flaky (2011)", "Flaky", "2011", "tmdb", "movie"),
             ("/m/movies/Nothing", "Nothing", None, "placeholder", None)])
        await db.commit()
    searched = []
    flaky = {"fail": True}

    async def search(title, year, key, media_type_hint=None):
        searched.append(title)
        return "u", "tmdb", {"media_type": "movie", "tmdb_id": {"Film": 5, "The Pick": 7, "Unknown": 8, "Flaky": 9}[title]}

    async def details(tmdb_id, media_type, key):
        if tmdb_id == 9 and flaky["fail"]:
            return {}  # rate-limited
        return {"imdb_id": {5: "tt0000005", 7: "tt0000007"}.get(tmdb_id, "")}
    monkeypatch.setattr(posters, "_resolve_tmdb_search", search)
    monkeypatch.setattr(posters, "_tmdb_details", details)
    assert await posters._backfill_details("test-key", lambda p: None) == 4
    assert await _rows(test_db) == [
        ("/m/movies/Film (2001)", "tt0000005", 7.9),     # IMDb's rating replaces TMDB's
        ("/m/movies/Flaky (2011)", None, 5.0),           # failed: tried again next time
        ("/m/movies/Named [tt0000001]", "tt0000001", 8.2),  # the folder's id, TMDB not asked
        ("/m/movies/Nothing", None, 5.0),                # no match, nothing to look up
        ("/m/movies/Picked", "tt0000007", 5.0),          # the user's pick, by its own title
        ("/m/movies/Unknown (2010)", "", 5.0),           # TMDB has none: not asked again
    ]
    assert sorted(searched) == ["Film", "Flaky", "The Pick", "Unknown"]
    flaky["fail"] = False
    searched.clear()
    assert await posters._backfill_details("test-key", lambda p: None) == 1
    assert searched == ["Flaky"]


@pytest.mark.asyncio
async def test_a_manual_match_stores_its_imdb_id(posters, test_db, monkeypatch):
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('tmdb_api_key', 'k')")
        await db.commit()
    asked = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append((request.url.path, request.url.params.get("append_to_response")))
        return httpx.Response(200, json={"name": "Show", "first_air_date": "2001-01-01", "networks": [{"name": "BBC"}],
                                         "status": "Ended", "vote_average": 6.0,
                                         "external_ids": {"imdb_id": "tt0000005"}})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    got = await posters.override_poster(posters.OverrideRequest(folder_path="/m/tv/Show", tmdb_id=12, media_type="tv"))
    assert asked == [("/3/tv/12", "external_ids")]
    assert (got["imdb_id"], got["rating"], got["rating_source"]) == ("tt0000005", 7.9, "imdb")
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT imdb_id, network, status, rating FROM poster_cache") as cur:
            assert await cur.fetchall() == [("tt0000005", "BBC", "Ended", 7.9)]


@pytest.mark.asyncio
async def test_the_tree_carries_the_titles_imdb_ids(test_db, monkeypatch):
    import backend.routes.scan as scan_route
    monkeypatch.setattr(scan_route, "DB_PATH", test_db)
    async with aiosqlite.connect(test_db) as db:
        await db.executemany(
            "INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, 1, '2026-10-10')",
            [("/m/movies/Film (2001)/film.mkv",), ("/m/tv/Show [tvdb-1]/Season 1/e1.mkv",),
             ("/m/movies/Unknown/u.mkv",)])
        await db.executemany(
            "INSERT INTO poster_cache (folder_path, title, imdb_id) VALUES (?, 'x', ?)",
            [("/m/movies/Film (2001)", "tt0000005"), ("/m/tv/Show [tvdb-1]", "tt0000002"),
             ("/m/movies/Unknown", ""), ("/m/movies/Gone", "tt0000009")])
        await db.commit()
    tree = await scan_route.get_scan_tree()
    assert tree["imdb_ids"] == {"/m/movies/Film (2001)": "tt0000005", "/m/tv/Show [tvdb-1]": "tt0000002"}
