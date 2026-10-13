"""Which title a file's poster and metadata belong to (v0.10.0), the same for
the Poster grid, the poster prefetch and the Title lists.

- A DVD / Blu-ray rip's rows are kept under its VIDEO_TS / BDMV folder, and
  TMDB was searched for "VIDEO_TS" / "BDMV": the title is its parent's.
- A file loose in a media folder is its own card in the Poster grid (keyed
  by its path), but the prefetch and the Title lists took the media folder
  for its title: TMDB was searched for the library's name ("Movies") and
  every loose file got that match's metadata.
Rows cached under those names are looked up again, once."""
import aiosqlite
import pytest

from backend.routes.posters import parse_folder_name
from backend.scan_filters import title_key


def test_a_disc_folder_is_named_after_its_parent():
    for path in ("/m/Movies/Heat (1995)/VIDEO_TS", "/m/Movies/Heat (1995)/BDMV", "/m/Movies/Heat (1995)/video_ts",
                 "/m/Movies/Heat (1995)/Bdmv/"):
        parsed = parse_folder_name(path, walk_files=False)
        assert (parsed["title"], parsed["year"]) == ("Heat", "1995"), path
    assert parse_folder_name("/m/Movies/Heat [tt0113277]/BDMV", walk_files=False)["imdb_id"] == "tt0113277"
    assert parse_folder_name("/m/TV/Show (2001)/Season 1/VIDEO_TS", walk_files=False)["title"] == "Show"
    # Only the folder itself: a title that starts with the word is its own.
    assert parse_folder_name("/m/Movies/VIDEO_TS Explained (2020)", walk_files=False)["year"] == "2020"


def test_title_keys_are_the_poster_grids():
    roots = frozenset({"/m/Movies", "/m/TV"})
    assert title_key("/m/Movies/Heat (1995).mkv", roots) == "/m/Movies/Heat (1995).mkv"  # loose: its own card
    assert title_key("/m/Movies/Heat (1995)/Heat.mkv", roots) == "/m/Movies/Heat (1995)"
    assert title_key("/m/Movies/Heat (1995)/VIDEO_TS/VIDEO_TS.IFO", roots) == "/m/Movies/Heat (1995)/VIDEO_TS"
    assert title_key("/m/TV/Show [tvdb-1]/Season 1/e1.mkv", roots) == "/m/TV/Show [tvdb-1]"
    assert title_key("/m/TV/Show/Season 1/e1.mkv", roots) == "/m/TV/Show/Season 1"
    assert title_key("/m/Movies/Heat (1995).mkv") == "/m/Movies"  # no media folders known: the folder
    # A media folder named with an id is a title (the grid's card for it).
    shows = roots | {"/m/Show [tvdb-1]"}
    assert title_key("/m/Show [tvdb-1]/e1.mkv", shows) == "/m/Show [tvdb-1]"
    assert title_key("/m/Show [tvdb-1]/Season 1/e1.mkv", shows) == "/m/Show [tvdb-1]"


@pytest.fixture
def posters(test_db, monkeypatch):
    import backend.imdb_ratings as imdb_ratings
    import backend.media_paths as media_paths
    import backend.routes.posters as posters
    import backend.routes.scan as scan_route
    monkeypatch.setattr(posters, "DB_PATH", test_db)
    monkeypatch.setattr(scan_route, "DB_PATH", test_db)
    monkeypatch.setattr(media_paths, "DB_PATH", test_db)
    media_paths.invalidate_media_dir_cache()
    monkeypatch.setattr(imdb_ratings, "_ratings", {})
    monkeypatch.setenv("SHRINKERR_TMDB_API_KEY", "test-key")
    searched = []

    async def search(title, year, key, media_type_hint=None, imdb_id=None):
        searched.append((title, year))
        return "https://img/x.jpg", "tmdb", {"media_type": "movie", "tmdb_id": len(searched)}

    async def details(tmdb_id, media_type, key):
        return {"imdb_id": "", "country": f"Country {tmdb_id}", "genres": "Drama", "year": ""}

    async def no_image(*a, **kw):
        return None
    monkeypatch.setattr(posters, "_resolve_tmdb_search", search)
    monkeypatch.setattr(posters, "_tmdb_details", details)
    monkeypatch.setattr(posters, "_download_image", no_image)
    return posters, searched


DISC = "/m/Movies/Heat (1995)/VIDEO_TS/VIDEO_TS.IFO"
LOOSE = "/m/Movies/Alien (1979).mkv"
FILES = [DISC, LOOSE, "/m/Movies/Dune (2021)/Dune.mkv"]


async def _library(db_path, rows=()):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO media_dirs (path, label) VALUES ('/m/Movies/', 'Movies'), "
                         "('/m/Show [tvdb-1]', 'TV')")
        await db.executemany("INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, 1, 'x')",
                             [(f,) for f in FILES])
        await db.executemany("INSERT INTO poster_cache (folder_path, title, source, country, image_data) "
                             "VALUES (?, ?, ?, ?, 'aW1n')", rows)
        await db.commit()


async def _rows(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT folder_path, title, source, country FROM poster_cache ORDER BY folder_path") as cur:
            return [tuple(r) for r in await cur.fetchall()]


async def _matching(db_path, text):
    """The files a filter lists, as the tree finds them."""
    from backend.routes.scan import _build_enrichment_context
    from backend.scan_filters import PY_COLUMNS, parse_filter
    expr = parse_filter(text)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        ctx = await _build_enrichment_context(db, titles=True)
        async with db.execute(f"SELECT {PY_COLUMNS}{expr.select_sql} FROM scan_results "
                              f"WHERE removed_from_list = 0{expr.where_sql}",
                              [*expr.select_params, *expr.where_params]) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return sorted(r["file_path"] for r in rows if expr.post_filter(r, ctx))


async def _no_backfill(*a):
    return 0


@pytest.mark.asyncio
async def test_the_prefetch_keys_titles_as_the_poster_grid_does(posters, test_db, monkeypatch):
    posters_mod, searched = posters
    monkeypatch.setattr(posters_mod, "_backfill_details", _no_backfill)
    await _library(test_db)
    await posters_mod._run_prefetch()
    # Never the media folder ("Movies"); the disc by its folder's name.
    assert sorted(searched) == [("Alien", "1979"), ("Dune", "2021"), ("Heat", "1995")]
    assert [r[:3] for r in await _rows(test_db)] == [
        ("/m/Movies/Alien (1979).mkv", "Alien", "tmdb"), ("/m/Movies/Dune (2021)", "Dune", "tmdb"),
        ("/m/Movies/Heat (1995)/VIDEO_TS", "Heat", "tmdb")]
    # The Title lists find each title's row: the disc's, the loose file's.
    from backend.scan_filters import facet_token
    country = {r[0]: r[3] for r in await _rows(test_db)}
    assert await _matching(test_db, facet_token("country", country["/m/Movies/Heat (1995)/VIDEO_TS"])) == [DISC]
    assert await _matching(test_db, facet_token("country", country["/m/Movies/Alien (1979).mkv"])) == [LOOSE]
    assert await _matching(test_db, "year:1970s") == [LOOSE]
    assert await _matching(test_db, "year:1990s") == [DISC]


@pytest.mark.asyncio
async def test_the_poster_grid_finds_a_disc_by_its_folders_name(posters, test_db):
    posters_mod, searched = posters
    await _library(test_db)
    path = "/m/Movies/Heat (1995)/VIDEO_TS"  # the grid's key: the disc marker's folder
    got = (await posters_mod.resolve_posters(posters_mod.ResolveRequest(paths=[path])))[path]
    assert searched == [("Heat", "1995")] and (got["title"], got["year"]) == ("Heat", "1995")
    from backend.scan_filters import facet_token
    assert await _matching(test_db, facet_token("country", got["country"])) == [DISC]


@pytest.mark.asyncio
async def test_rows_cached_under_the_wrong_name_are_looked_up_again_once(posters, test_db, monkeypatch):
    posters_mod, searched = posters
    monkeypatch.setattr(posters_mod, "_backfill_details", _no_backfill)
    await _library(test_db, [
        ("/m/Movies", "Movies", "tmdb", "Narnia"),                        # the library's name, found
        ("/m/Movies/Heat (1995)/VIDEO_TS", "VIDEO_TS", "tmdb", "Narnia"),  # "VIDEO_TS", found
        ("/m/Movies/Old (1950)/BDMV", "BDMV", "placeholder", None),
        ("/m/Movies/Mine (2001)/VIDEO_TS", "Mine", "tmdb-manual", "Spain"),  # a Fix match: kept
        ("/m/Movies/Dune (2021)", "Dune", "tmdb", "United States"),        # right: kept
        ("/m/Show [tvdb-1]", "Show", "tmdb", "Iceland"),                   # a media folder that's a title: kept
    ])
    await posters_mod._run_prefetch()
    rows = {r[0]: r[1:] for r in await _rows(test_db)}
    assert "/m/Movies" not in rows and "/m/Movies/Old (1950)/BDMV" not in rows
    assert rows["/m/Movies/Heat (1995)/VIDEO_TS"][:2] == ("Heat", "tmdb")
    assert rows["/m/Movies/Mine (2001)/VIDEO_TS"] == ("Mine", "tmdb-manual", "Spain")
    assert rows["/m/Movies/Dune (2021)"] == ("Dune", "tmdb", "United States")
    assert rows["/m/Show [tvdb-1]"] == ("Show", "tmdb", "Iceland")
    assert sorted(searched) == [("Alien", "1979"), ("Heat", "1995")]
    # Once: a row stored under such a name later stays (the Fix match modal, say).
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO poster_cache (folder_path, title, source, image_data) "
                         "VALUES ('/m/Movies/Odd/BDMV', 'Odd', 'tmdb', 'aW1n')")
        await db.commit()
    await posters_mod.resolve_posters(posters_mod.ResolveRequest(paths=["/m/Movies/Dune (2021)"]))
    await posters_mod._run_prefetch()
    assert "/m/Movies/Odd/BDMV" in {r[0] for r in await _rows(test_db)}


@pytest.mark.asyncio
async def test_the_poster_grid_purges_them_too(posters, test_db):
    posters_mod, searched = posters
    await _library(test_db, [("/m/Movies/Heat (1995)/VIDEO_TS", "VIDEO_TS", "tmdb", "Narnia")])
    path = "/m/Movies/Heat (1995)/VIDEO_TS"
    got = (await posters_mod.resolve_posters(posters_mod.ResolveRequest(paths=[path])))[path]
    assert searched == [("Heat", "1995")] and got["title"] == "Heat"


def test_a_loose_files_title_metadata_is_its_own():
    """Not the media folder's (a row cached for "Movies", or none)."""
    from backend.scan_filters import row_title
    ctx = {"title_meta": {"/m/Movies": ("2005", 3.0, "Horror", None, None, "Narnia"),
                          "/m/Movies/Alien (1979).mkv": ("1979", 8.5, "Sci-Fi", None, None, "United Kingdom")},
           "media_roots": frozenset({"/m/Movies"})}
    alien = row_title({"file_path": "/m/Movies/Alien (1979).mkv"}, ctx)
    assert (alien["year"], alien["countries"], alien["genres"]) == (1979, ["United Kingdom"], {"sci-fi"})
    other = row_title({"file_path": "/m/Movies/Heat (1995).mkv"}, ctx)  # not looked up yet: its name's year
    assert (other["year"], other["countries"], other["genres"]) == (1995, [], set())
