"""Which title a folder's poster and details come from (v0.10.0).

- Plex: the first item with the folder's title was taken — whatever its
  year, file location or ids — so a remake or any same-named title in Plex
  gave the wrong poster ("Buddy (2026) [tt37281055]" showed the 1997 film).
  Now an item is this title when its files are in this folder; else when
  its ids agree with the folder's; else when its title and year (±1) do.
- TMDB: titles are compared without case, accents and punctuation ("Mission
  Impossible" is "Mission: Impossible", "Amelie" "Amélie", "&" "and"), by
  their original titles too, and a last pass takes TMDB's best-ranked
  result of the folder's year (±1) whose title is close, as Fix match
  would show first.
- A folder named with an IMDb id that TMDB's /find doesn't know (common
  for new titles) was matched by title alone: a candidate whose IMDb id is
  another title's is skipped for the next one.
- Plex matches made before are looked up again, once."""
import aiosqlite
import httpx
import pytest

from backend.routes.posters import pick_tmdb_match, rank_tmdb_matches


# --- TMDB title matching -----------------------------------------------------

def _film(title, date, **kw):
    return {"title": title, "release_date": date, **kw}


@pytest.mark.parametrize("folder_title, year, result", [
    ("Mission Impossible", "1996", _film("Mission: Impossible", "1996-05-22")),
    ("Fast and Furious", "2009", _film("Fast & Furious", "2009-04-02")),
    ("Fast & Furious", "2009", _film("Fast and Furious", "2009-04-02")),
    ("Amelie", "2001", _film("Amélie", "2001-04-25", original_title="Le Fabuleux Destin d'Amélie Poulain")),
    ("Spider Man Into the Spider Verse", "2018", _film("Spider-Man: Into the Spider-Verse", "2018-12-06")),
    # A Danish film by its Danish name: TMDB's title is the English one.
    ("Druk", "2020", _film("Another Round", "2020-09-24", original_title="Druk")),
    # A show by its original name.
    ("Forbrydelsen", "2007", {"name": "The Killing", "original_name": "Forbrydelsen", "first_air_date": "2007-01-07"}),
])
def test_titles_match_without_case_accents_and_punctuation(folder_title, year, result):
    other = _film("Something Else", f"{year}-01-01")
    assert pick_tmdb_match([other, result], folder_title, year) is result
    assert pick_tmdb_match([other, result], folder_title, None) is result  # the same title: no year needed


@pytest.mark.parametrize("folder_title, year, result", [
    # Release folders leave words out ...
    ("The Hobbit Battle of the Five Armies", "2014", _film("The Hobbit: The Battle of the Five Armies", "2014-12-10")),
    ("Harry Potter Philosophers Stone", "2001", _film("Harry Potter and the Philosopher's Stone", "2001-11-16")),
    ("The Godfather 2", "1974", _film("The Godfather Part II", "1974-12-20")),
    ("Harry Potter Deathly Hallows 2", "2011", _film("Harry Potter and the Deathly Hallows: Part 2", "2011-07-07")),
    # ... spell them differently, or number a sequel another way.
    ("Colour Out of Space", "2019", _film("Color Out of Space", "2019-09-07")),
    ("Seven", "1995", _film("Se7en", "1995-09-22")),
    ("Megan", "2022", _film("M3GAN", "2022-12-28")),
    ("Colour", "2019", _film("Color", "2019-03-01")),
    ("Mission Impossible 2", "2000", _film("Mission: Impossible II", "2000-05-24")),
    # TMDB's year a year off.
    ("Colour Out of Space", "2020", _film("Color Out of Space", "2019-09-07")),
])
def test_tmdbs_best_ranked_close_title_of_the_year_is_taken(folder_title, year, result):
    assert pick_tmdb_match([result], folder_title, year) is result


def test_close_titles_are_taken_in_tmdbs_order_and_others_not_at_all():
    film, short = _film("Color Out of Space", "2019-09-07"), _film("Color Out of Space", "2019-01-01")
    assert rank_tmdb_matches([film, short], "Colour Out of Space", "2019") == [film, short]
    assert rank_tmdb_matches([short, film], "Colour Out of Space", "2019") == [short, film]
    sequel = _film("Harry Potter and the Chamber of Secrets", "2002-11-13")  # of the year (±1), not close
    first = _film("Harry Potter and the Philosopher's Stone", "2001-11-16")
    assert rank_tmdb_matches([sequel, first], "Harry Potter Philosophers Stone", "2001") == [first]


def test_buddy_2026_is_tmdbs_first_buddy_not_a_namesake():
    buddy, comedy, gym = (_film("Buddy", "2026-04-17"), _film("Comedy Buddy", "2026-01-09"),
                          _film("Gym Buddy", "2026-02-20"))
    old = _film("Buddy", "1997-06-06")
    assert pick_tmdb_match([old, comedy, buddy, gym], "Buddy", "2026") is buddy
    # The candidates in the order they'd be tried: the 1997 film never.
    assert rank_tmdb_matches([old, comedy, buddy, gym], "Buddy", "2026") == [buddy, comedy, gym]


@pytest.mark.parametrize("folder_title, year, result", [
    ("Buddy", "2026", _film("Buddy", "1997-06-06")),                         # a namesake of another year
    ("Colour Out of Space", "2021", _film("Color Out of Space", "2019-09-07")),  # close, two years off
    ("Saw II", "2005", _film("Saw III", "2006-10-26")),                      # another part of a series
    ("Saw IV", "2007", _film("Saw V", "2008-10-23")),
    ("Frozen", "2013", _film("Frozen II", "2014-11-20")),
    ("Harry Potter and the Deathly Hallows Part 1", "2010",
     _film("Harry Potter and the Deathly Hallows: Part 2", "2011-07-07")),
    ("The Hunger Games Mockingjay Part 1", "2014", _film("The Hunger Games: Mockingjay - Part 2", "2015-11-18")),
    ("American Hustle", "2013", _film("American Sniper", "2014-12-25")),               # alike, not close
    ("The Lost City", "2022", _film("The Lost King", "2022-10-07")),
    ("Buddy", "2026", _film("Body", "2026-01-01")),                          # not close
    # One letter is much of a short title.
    ("Lucky", "2014", _film("Lucy", "2014-07-14")),
    ("Glass", "2019", _film("Class", "2019-01-01")),
    ("The Ring", "2002", _film("The King", "2002-01-01")),
    ("Taken", "2008", _film("Token", "2008-01-01")),
])
def test_no_wrong_title_is_taken(folder_title, year, result):
    assert pick_tmdb_match([result], folder_title, year) is None


@pytest.mark.parametrize("folder_title, result", [
    ("기생충", _film("Parasite", "2019-05-30", original_title="기생충")),
    ("Левиафан", _film("Leviathan", "2014-09-24", original_title="Левиафан")),
    ("ゴジラ", _film("Godzilla", "2019-05-29", original_title="ゴジラ")),
    ("Amélie", _film("Amélie", "2019-01-01")),  # macOS shares give names decomposed
])
def test_titles_in_other_scripts_match_by_their_original_title(folder_title, result):
    import unicodedata
    for name in (folder_title, unicodedata.normalize("NFD", folder_title)):
        assert pick_tmdb_match([_film("Something Else", "2019-01-01"), result], name, result["release_date"][:4]) is result
    assert pick_tmdb_match([_film("Something Else", "2019-01-01")], folder_title, None) is None  # not "" for all


def test_without_a_year_titles_match_as_before():
    buddy, comedy = _film("Buddy", "1997-06-06"), _film("Comedy Buddy", "2026-01-09")
    assert pick_tmdb_match([comedy, buddy], "Buddy", None) is buddy  # the same title first
    assert pick_tmdb_match([comedy], "Buddy", None) is comedy        # then one containing it
    assert pick_tmdb_match([_film("Something Else", "2005")], "Buddy", None) is None
    assert pick_tmdb_match([_film("Mission: Impossible", "1996")], "Mission Impossible", None) is not None
    # No year: no guess at a close title.
    assert pick_tmdb_match([_film("Color Out of Space", "2019")], "Colour Out of Space", None) is None
    assert pick_tmdb_match([{"title": "", "release_date": "2005"}], "Buddy", None) is None  # an untitled result


# --- The IMDb check on a title search ------------------------------------------

BUDDY_FOLDER = "/m/movies/Buddy (2026) [tt37281055]"
BUDDIES = [  # TMDB's /search/movie for "Buddy", 2026: two films of the year
    {"id": 11, "title": "Buddy", "release_date": "2026-02-01", "poster_path": "/wrong.jpg"},
    {"id": 12, "title": "Buddy", "release_date": "2026-04-17", "poster_path": "/right.jpg"},
]


@pytest.fixture
def posters(test_db, monkeypatch):
    import backend.imdb_ratings as imdb_ratings
    import backend.media_paths as media_paths
    import backend.routes.posters as posters
    monkeypatch.setattr(posters, "DB_PATH", test_db)
    monkeypatch.setattr(media_paths, "DB_PATH", test_db)
    media_paths.invalidate_media_dir_cache()
    monkeypatch.setattr(imdb_ratings, "_ratings", {})
    monkeypatch.setenv("SHRINKERR_TMDB_API_KEY", "test-key")

    async def no_image(*a, **kw):
        return None
    monkeypatch.setattr(posters, "_download_image", no_image)
    return posters


@pytest.fixture
def tmdb(posters, monkeypatch):
    """TMDB as it answers for BUDDY_FOLDER: /find doesn't know the IMDb id
    (a new title), the search gives BUDDIES, the first of them is another
    film (its IMDb id says so). The paths asked, in order."""
    asked = []

    async def get(client, url, params):
        path = url.split("/3/")[1]
        asked.append(path)
        if path.startswith("find/"):
            return httpx.Response(200, json={"movie_results": [], "tv_results": []})
        if path.startswith("search/"):
            return httpx.Response(200, json={"results": [dict(r) for r in BUDDIES]})
        if path == "movie/11":
            return httpx.Response(200, json={"imdb_id": "tt0000011", "release_date": "2026-02-01",
                                             "origin_country": ["FR"]})
        if path == "movie/12":
            return httpx.Response(200, json={"imdb_id": "tt37281055", "release_date": "2026-04-17",
                                             "origin_country": ["IS"]})
        return httpx.Response(404, json={})
    monkeypatch.setattr(posters, "_tmdb_get", get)
    return asked


@pytest.mark.asyncio
async def test_a_candidate_with_another_imdb_id_is_skipped(posters, tmdb):
    poster_url, source, meta = await posters._resolve_tmdb_search(
        "Buddy", "2026", "k", media_type_hint="movie", imdb_id="tt37281055")
    assert (source, meta["tmdb_id"], meta["imdb_id"], meta["country"]) == ("tmdb", 12, "tt37281055", "Iceland")
    assert poster_url.endswith("/right.jpg")
    assert tmdb == ["search/movie", "movie/11", "movie/12"]
    # Without an id to check, TMDB's first is taken, with no details asked.
    tmdb.clear()
    _, _, meta = await posters._resolve_tmdb_search("Buddy", "2026", "k", media_type_hint="movie")
    assert meta["tmdb_id"] == 11 and tmdb == ["search/movie"]


@pytest.mark.asyncio
async def test_no_candidate_with_the_folders_imdb_id_is_no_match(posters, tmdb):
    assert await posters._resolve_tmdb_search(
        "Buddy", "2026", "k", media_type_hint="movie", imdb_id="tt0000099") == (None, "placeholder", {})
    assert tmdb == ["search/movie", "movie/11", "movie/12"]  # at most three tried (two here)


@pytest.mark.asyncio
async def test_the_only_film_of_a_title_and_year_is_taken_though_its_imdb_id_differs(posters, monkeypatch):
    """TMDB didn't know the folder's IMDb id (/find empty) and its one
    "Summer of Soul" of 2021 has another: one film's old and new ids (IMDb
    merges duplicates). It was refused, so the title stayed unmatched."""
    async def get(client, url, params):
        if "/search/" in url:
            return httpx.Response(200, json={"results": [
                {"id": 776527, "title": "Summer of Soul (...Or, When the Revolution Could Not Be Televised)",
                 "original_title": "Summer of Soul (...Or, When the Revolution Could Not Be Televised)",
                 "release_date": "2021-07-02", "poster_path": "/sos.jpg"},
                {"id": 9, "title": "Summer of Love", "release_date": "2021-05-01"}]})
        return httpx.Response(200, json={"imdb_id": "tt7378922", "origin_country": ["US"]})
    monkeypatch.setattr(posters, "_tmdb_get", get)
    _, source, meta = await posters._resolve_tmdb_search(
        "Summer of Soul (...or, When the Revolution Could Not Be Televised)", "2021", "k",
        media_type_hint="movie", imdb_id="tt11422728")
    assert (source, meta["tmdb_id"]) == ("tmdb", 776527)


@pytest.mark.asyncio
async def test_a_candidate_that_couldnt_be_checked_isnt_taken(posters, tmdb, test_db, monkeypatch):
    """Rate-limited (the burst after the Plex matches were purged), the
    first namesake was taken unchecked and kept for good."""
    real_get = posters._tmdb_get

    async def limited(client, url, params):
        if "/movie/" in url:
            tmdb.append(url.split("/3/")[1])
            return httpx.Response(429, json={})
        return await real_get(client, url, params)
    monkeypatch.setattr(posters, "_tmdb_get", limited)
    with pytest.raises(RuntimeError):
        await posters._resolve_tmdb_search("Buddy", "2026", "k", media_type_hint="movie", imdb_id="tt37281055")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, 1, 'x')",
                         (BUDDY_FOLDER + "/Buddy.mkv",))
        await db.commit()
    monkeypatch.setattr(posters, "_backfill_details", _no_backfill)
    await posters._run_prefetch()
    got = (await posters.resolve_posters(posters.ResolveRequest(paths=[BUDDY_FOLDER])))[BUDDY_FOLDER]
    assert got["source"] == "placeholder"  # shown as not found this time ...
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT COUNT(*) FROM poster_cache") as cur:
            assert (await cur.fetchone())[0] == 0  # ... and looked up again next time


@pytest.mark.asyncio
async def test_a_candidate_tmdb_has_no_imdb_id_for_is_taken(posters, monkeypatch):
    async def get(client, url, params):
        if "/search/" in url:
            return httpx.Response(200, json={"results": [dict(BUDDIES[0])]})
        return httpx.Response(200, json={"imdb_id": None, "origin_country": ["FR"]})
    monkeypatch.setattr(posters, "_tmdb_get", get)
    _, _, meta = await posters._resolve_tmdb_search("Buddy", "2026", "k", media_type_hint="movie",
                                                    imdb_id="tt37281055")
    assert meta["tmdb_id"] == 11


@pytest.mark.asyncio
async def test_the_poster_grid_checks_the_imdb_id(posters, tmdb, test_db):
    got = (await posters.resolve_posters(posters.ResolveRequest(paths=[BUDDY_FOLDER])))[BUDDY_FOLDER]
    assert (got["source"], got["imdb_id"], got["country"]) == ("tmdb", "tt37281055", "Iceland")
    # The details the check fetched are the ones stored: not asked again.
    assert tmdb == ["find/tt37281055", "search/movie", "movie/11", "movie/12"]
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT poster_url, details_v FROM poster_cache") as cur:
            assert await cur.fetchall() == [("https://image.tmdb.org/t/p/w300/right.jpg", posters.DETAILS_VERSION)]


@pytest.mark.asyncio
async def test_the_prefetch_checks_the_imdb_id(posters, tmdb, test_db, monkeypatch):
    async def no_backfill(*a):
        return 0
    monkeypatch.setattr(posters, "_backfill_details", no_backfill)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, 1, 'x')",
                         (BUDDY_FOLDER + "/Buddy.mkv",))
        await db.commit()
    await posters._run_prefetch()
    assert tmdb == ["find/tt37281055", "search/movie", "movie/11", "movie/12"]
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT poster_url, imdb_id, country FROM poster_cache") as cur:
            assert await cur.fetchall() == [("https://image.tmdb.org/t/p/w300/right.jpg", "tt37281055", "Iceland")]


@pytest.mark.asyncio
async def test_the_details_backfill_checks_the_imdb_id(posters, tmdb, test_db):
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO poster_cache (folder_path, title, year, source, media_type, details_v) "
                         "VALUES (?, 'Buddy', '2026', 'tmdb', 'movie', 0)", (BUDDY_FOLDER,))
        await db.commit()
    assert await posters._backfill_details("k", lambda p: None) == 1
    assert tmdb == ["find/tt37281055", "search/movie", "movie/11", "movie/12"]
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT imdb_id, country FROM poster_cache") as cur:
            assert await cur.fetchall() == [("tt37281055", "Iceland")]


# --- Plex ------------------------------------------------------------------------

PLEX = "http://plex:32400"
LIBRARIES = [{"id": "1", "type": "movie", "paths": ["/data/misc"]},
             {"id": "2", "type": "show", "paths": ["/data/tv"]}]
MAPPING = "/media/Misc=/data/misc;/media/TV=/data/tv"
FOLDER = "/media/Misc/HD 2025/Buddy (2026) [tt37281055]"  # Plex sees /data/misc/HD 2025/...


def _movie(year, file=None, guids=(), key="100", title="Buddy"):
    item = {"ratingKey": key, "type": "movie", "title": title, "year": year,
            "thumb": f"/library/metadata/{key}/thumb/1"}
    if file:
        item["Media"] = [{"Part": [{"file": file}]}]
    if guids:
        item["Guid"] = [{"id": g} for g in guids]
    return item


@pytest.fixture(autouse=True)
def plex_logs(monkeypatch):
    """What posters logged about Plex starts over for each test."""
    import backend.routes.posters as posters
    monkeypatch.setattr(posters, "_plex_logged", set())
    monkeypatch.setattr(posters, "_plex_warned_at", None)


@pytest.fixture
def plex(monkeypatch):
    """A Plex server: `found["section"]` / `found["global"]` are what its
    library and global searches find (a list, or one per query),
    `found["metadata"]` its items' own records. The requests made, as (path,
    params), in `found["asked"]`; how often its libraries were read."""
    import backend.plex as plex_mod
    found = {"section": [], "global": [], "metadata": {}, "asked": [], "libraries_read": 0, "libraries": LIBRARIES}

    async def libraries(url, token):
        found["libraries_read"] += 1
        return found["libraries"]
    monkeypatch.setattr(plex_mod, "get_plex_libraries", libraries)

    def answer(got):
        if isinstance(got, int):
            return httpx.Response(got, json={})  # an error status
        if isinstance(got, str):
            return httpx.Response(200, text=got)  # not JSON
        return httpx.Response(200, json={"MediaContainer": {"Metadata": got}})

    def results(kind, request):
        got = found[kind]
        return got.get(request.url.params.get("query"), []) if isinstance(got, dict) else got

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        found["asked"].append((path, dict(request.url.params)))
        if path.endswith("/search") and path.startswith("/library/sections/"):
            return answer(results("section", request))
        if path == "/search":
            return answer(results("global", request))
        key = path.rsplit("/", 1)[-1]
        if path.startswith("/library/metadata/") and isinstance(found["metadata"], int):
            return httpx.Response(found["metadata"], json={})
        if path.startswith("/library/metadata/") and key in found["metadata"]:
            return httpx.Response(200, json={"MediaContainer": {"Metadata": [found["metadata"][key]]}})
        return httpx.Response(404, json={})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    return found


async def _resolve(folder=FOLDER, mapping=MAPPING):
    from backend.routes.posters import _resolve_plex, parse_folder_name
    url, source, meta = await _resolve_plex(folder, parse_folder_name(folder, walk_files=False), PLEX, "tok", mapping)
    return (url.split("%2F")[3] if url else None), source, meta  # the item's ratingKey


@pytest.mark.asyncio
async def test_a_namesake_of_another_year_isnt_this_title(plex):
    """The Buddy case: no file paths in Plex's answer, the 1997 film."""
    plex["section"] = [_movie(1997, key="100")]
    plex["global"] = [_movie(1997, key="100")]
    assert await _resolve() == (None, "placeholder", {})
    plex["section"] = [_movie(1997, key="100"), _movie(2025, key="101")]  # ±1 year: this one
    assert await _resolve() == ("101", "plex", {"media_type": "movie"})
    assert all(params.get("includeGuids") == "1" for path, params in plex["asked"] if path.endswith("search"))


@pytest.mark.asyncio
async def test_where_the_files_are_decides(plex):
    here = "/data/misc/HD 2025/Buddy (2026) [tt37281055]/Buddy.2026.1080p.mkv"
    elsewhere = "/data/misc/HD 2025/Buddy (2026) [tt37281055] copy/Buddy.mkv"
    # In this folder: this title, whatever year Plex's metadata gives it.
    plex["section"] = [_movie(2026, file=elsewhere, key="100"), _movie(2019, file=here, key="101")]
    assert await _resolve() == ("101", "plex", {"media_type": "movie"})
    # The same title and year with its files elsewhere: another copy, not this one.
    plex["section"] = [_movie(2026, file=elsewhere, key="100")]
    assert await _resolve() == (None, "placeholder", {})


@pytest.mark.asyncio
async def test_ids_decide_before_where_the_files_are(plex):
    here = "/data/misc/HD 2025/Buddy (2026) [tt37281055]/Buddy.mkv"
    # Plex took the new film for the 1997 one: its file is here, its id says it's that film.
    plex["section"] = [_movie(2026, file=here, guids=["imdb://tt0118798"], key="100")]
    assert await _resolve() == (None, "placeholder", {})
    # Another copy of this film (or one Plex hasn't scanned again): its id says it's this one.
    plex["section"] = [_movie(2026, file="/data/misc/4K/Buddy (2026)/Buddy.mkv", guids=["imdb://tt37281055"], key="101")]
    assert await _resolve() == ("101", "plex", {"media_type": "movie"})


@pytest.mark.asyncio
async def test_paths_are_compared_however_the_accents_are_written(plex):
    import unicodedata
    folder = "/media/Misc/Hrútar (2015)"  # as Shrinkerr lists it (NFC); Plex's file decomposed (NFD)
    plex_file = unicodedata.normalize("NFD", "/data/misc/Hrútar (2015)/Hrútar.mkv")
    plex["section"] = [_movie(2016, file=plex_file, key="100", title="Rams")]  # its English title
    assert await _resolve(folder) == ("100", "plex", {"media_type": "movie"})


@pytest.mark.asyncio
async def test_a_discs_title_is_its_folders(plex):
    """A disc's key is its VIDEO_TS folder; Plex has the film beside it."""
    plex["section"] = [_movie(1995, file="/data/misc/Heat (1995)/Heat (1995).mkv", key="200", title="Heat")]
    assert await _resolve("/media/Misc/Heat (1995)/VIDEO_TS") == ("200", "plex", {"media_type": "movie"})
    plex["section"] = [_movie(1995, file="/data/misc/Heat (1995) 2/Heat.mkv", key="201", title="Heat")]
    assert await _resolve("/media/Misc/Heat (1995)/BDMV") == (None, "placeholder", {})


@pytest.mark.asyncio
async def test_a_library_is_a_folder_not_a_prefix(plex):
    """/data/misc2 isn't in the library at /data/misc: Plex's paths can't be
    compared with the folder's (its title and year decide), and the
    library isn't searched."""
    plex["global"] = [_movie(2026, file="/data/misc/Buddy (2026)/Buddy.mkv", key="100")]
    assert await _resolve("/media/Misc2/Buddy (2026)", "/media/Misc2=/data/misc2") == (
        "100", "plex", {"media_type": "movie"})
    assert [path for path, _ in plex["asked"]] == ["/search"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["libraries", "search 500", "search 401", "search unreadable", "record 500"])
async def test_plex_not_answering_raises(plex, monkeypatch, failure):
    import backend.plex as plex_mod
    if failure == "libraries":
        async def unreachable(url, token):
            raise httpx.ConnectError("refused")
        monkeypatch.setattr(plex_mod, "get_plex_libraries", unreachable)
    elif failure == "search unreadable":
        plex["section"] = "<html>Plex is starting</html>"
    elif failure.startswith("search"):
        plex["section"] = int(failure.split()[1])
    else:  # a show whose ids can't tell: its record is asked for
        plex["section"] = [{"ratingKey": "7", "type": "show", "title": "Dark", "year": 2017,
                            "thumb": "/library/metadata/7/thumb/1"}]
        plex["metadata"] = 500
    with pytest.raises(Exception):
        await _resolve("/media/TV/Dark (2017)" if failure.startswith("record") else FOLDER)


@pytest.mark.asyncio
async def test_a_request_plex_refuses_for_good_is_no_match(plex, capsys):
    """A server without the legacy /search (404), a library search it
    rejects (400): no match from it, logged once — Plex isn't down."""
    plex["global"] = 404
    assert await _resolve() == (None, "placeholder", {})
    plex["section"] = 400
    assert await _resolve() == (None, "placeholder", {})
    assert await _resolve() == (None, "placeholder", {})
    out = capsys.readouterr().out
    assert out.count("Plex answers 404 to its search") == 1 and out.count("Plex answers 400 to a library search") == 1
    plex["section"] = [{"ratingKey": "7", "type": "show", "title": "Dark", "year": 2017,
                        "thumb": "/library/metadata/7/thumb/1"}]
    plex["metadata"] = 404  # a show's record Plex no longer has: where it is isn't known
    assert await _resolve("/media/TV/Dark (2017)") == ("7", "plex", {"media_type": "tv"})


@pytest.mark.asyncio
async def test_a_loose_files_title_is_its_own_file(plex):
    plex["section"] = [_movie(2026, file="/data/misc/Buddy (2026).mkv", key="100")]
    assert await _resolve("/media/Misc/Buddy (2026).mkv") == ("100", "plex", {"media_type": "movie"})


@pytest.mark.asyncio
async def test_the_folders_ids_decide_when_the_files_arent_known(plex):
    plex["section"] = [_movie(2026, guids=["imdb://tt0118798", "tmdb://9622"], key="100"),
                       _movie(1999, guids=["imdb://tt37281055"], key="101")]
    assert await _resolve() == ("101", "plex", {"media_type": "movie"})
    # Plex's legacy agents keep the id in the item's guid.
    plex["section"] = [{**_movie(2026, key="102"), "guid": "com.plexapp.agents.imdb://tt0118798?lang=en"}]
    assert await _resolve() == (None, "placeholder", {})
    plex["section"] = [{**_movie(1999, key="103"), "guid": "com.plexapp.agents.imdb://tt37281055?lang=en"}]
    assert await _resolve() == ("103", "plex", {"media_type": "movie"})


@pytest.mark.asyncio
async def test_plexs_global_search_follows_the_same_rules(plex):
    plex["global"] = [_movie(1997, key="100"),
                      {"ratingKey": "50", "type": "episode", "title": "Buddy", "year": 2026,
                       "thumb": "/library/metadata/50/thumb/1"},
                      _movie(2026, guids=["imdb://tt0000001"], key="101")]
    assert await _resolve() == (None, "placeholder", {})
    plex["global"].append(_movie(2026, file="/data/misc/HD 2025/Buddy (2026) [tt37281055]/b.mkv", key="102"))
    assert await _resolve() == ("102", "plex", {"media_type": "movie"})


@pytest.mark.asyncio
async def test_a_folder_plex_doesnt_see_is_matched_by_ids_title_and_year(plex):
    """Without a path mapping Plex's paths can't be compared with this
    folder's: its ids, else its title and year, decide."""
    plex["global"] = [_movie(2026, file="/data/misc/Buddy (2026)/Buddy.mkv", key="100")]
    assert await _resolve(mapping="") == ("100", "plex", {"media_type": "movie"})
    plex["global"] = [_movie(1997, file="/data/misc/Buddy (1997)/Buddy.mkv", key="100")]
    assert await _resolve(mapping="") == (None, "placeholder", {})


@pytest.mark.asyncio
async def test_a_show_is_found_by_its_folder(plex):
    folder = "/media/TV/Dark (2017)"
    show = {"ratingKey": "7", "type": "show", "title": "Dark", "year": 2017, "thumb": "/library/metadata/7/thumb/1"}
    plex["section"] = [show]
    plex["metadata"] = {"7": {**show, "Location": [{"path": "/data/tv/Dark (2017)"}]}}
    assert await _resolve(folder) == ("7", "plex", {"media_type": "tv"})
    assert ("/library/metadata/7", {"includeGuids": "1"}) in plex["asked"]
    assert await _resolve(folder + "/Season 1") == ("7", "plex", {"media_type": "tv"})  # a season folder's key
    # The same title and year in another folder: another show — asked about
    # once, though the global search finds it again.
    plex["metadata"] = {"7": {**show, "Location": [{"path": "/data/tv/Dark (2017) [German]"}]}}
    plex["global"], plex["asked"] = [show], []
    assert await _resolve(folder) == (None, "placeholder", {})
    assert [path for path, _ in plex["asked"]] == ["/library/sections/2/search", "/library/metadata/7", "/search"]
    # A show of another title isn't asked about.
    plex["asked"].clear()
    plex["section"], plex["global"] = [{**show, "title": "Dark Matter", "ratingKey": "8"}], []
    assert await _resolve(folder) == (None, "placeholder", {})
    assert not any(path.startswith("/library/metadata/") for path, _ in plex["asked"])


# --- Plex as it answers on a real server (the user's, read-only) ------------------

HALHUB = "/media=/home/hal9000/HALHUB"  # Shrinkerr's /media is Plex's /home/hal9000/HALHUB
HALHUB_LIBRARIES = [
    {"id": "1", "title": "Movies", "type": "movie",
     "paths": ["/home/hal9000/HALHUB/Misc/Movies2", "/home/hal9000/HALHUB/M2T2/Movies4"]},
    {"id": "2", "title": "TV Shows", "type": "show",
     "paths": ["/home/hal9000/HALHUB/M2T2/TV4", "/home/hal9000/HALHUB/TV1/TV1"]},
]
BUTTERFLY = "/media/TV1/TV1/Butterfly (2025) [tvdb-438640]"
BUTTERFLY_2018 = {"ratingKey": "6001", "type": "show", "title": "Butterfly", "year": 2018,
                  "thumb": "/library/metadata/6001/thumb/1",
                  "Guid": [{"id": "imdb://tt7703440"}, {"id": "tmdb://83244"}, {"id": "tvdb://352161"}]}
BUTTERFLY_2025 = {"ratingKey": "6002", "type": "show", "title": "Butterfly (2025)", "year": 2025,
                  "thumb": "/library/metadata/6002/thumb/1",
                  "Guid": [{"id": "imdb://tt26672404"}, {"id": "tmdb://227114"}, {"id": "tvdb://438640"}]}
BUTTERFLY_RECORDS = {  # /library/metadata/{ratingKey}: a show's Location is only here
    "6001": {**BUTTERFLY_2018, "Location": [{"path": "/home/hal9000/HALHUB/M2T2/TV4/Butterfly (2018) [tvdb-352161]"}]},
    "6002": {**BUTTERFLY_2025, "Location": [{"path": "/home/hal9000/HALHUB/TV1/TV1/Butterfly (2025) [tvdb-438640]"}]},
}


def _without_ids(item):
    return {k: v for k, v in item.items() if k != "Guid"}


@pytest.mark.asyncio
async def test_a_film_plex_has_in_its_folder(plex):
    plex["libraries"] = HALHUB_LIBRARIES
    folder = "/home/hal9000/HALHUB/M2T2/Movies4/Futuresport (1999) [tt0158409]"
    plex["section"] = {"Futuresport": [{
        "ratingKey": "5001", "type": "movie", "title": "Futuresport", "year": 1998,
        "thumb": "/library/metadata/5001/thumb/1",
        "Guid": [{"id": "imdb://tt0158409"}, {"id": "tmdb://16365"}, {"id": "tvdb://43367"}],
        "Media": [{"Part": [{"file": f"{folder}/Futuresport (1999) 480p DVDRip AC3 2.0 x264-HANDJOB.mkv"}]}]}]}
    assert await _resolve("/media/M2T2/Movies4/Futuresport (1999) [tt0158409]", HALHUB) == (
        "5001", "plex", {"media_type": "movie"})
    assert plex["asked"][0] == ("/library/sections/1/search",
                                {"type": "1", "query": "Futuresport", "includeGuids": "1"})


@pytest.mark.asyncio
async def test_a_show_plex_has_two_of_is_told_by_its_ids(plex):
    """Today's code took the 2018 show: its title is the folder's."""
    plex["libraries"], plex["metadata"] = HALHUB_LIBRARIES, BUTTERFLY_RECORDS
    plex["section"] = {"Butterfly": [BUTTERFLY_2018, BUTTERFLY_2025]}
    assert await _resolve(BUTTERFLY, HALHUB) == ("6002", "plex", {"media_type": "tv"})
    # The ids in the search results decide: no show's record asked for.
    assert [path for path, _ in plex["asked"]] == ["/library/sections/2/search"]


@pytest.mark.asyncio
async def test_without_ids_plexs_year_in_the_title_and_the_shows_folder_decide(plex):
    plex["libraries"], plex["metadata"] = HALHUB_LIBRARIES, BUTTERFLY_RECORDS
    plex["section"] = {"Butterfly": [_without_ids(BUTTERFLY_2018), _without_ids(BUTTERFLY_2025)]}
    assert await _resolve(BUTTERFLY, HALHUB) == ("6002", "plex", {"media_type": "tv"})
    # The 2018 show isn't this title by its year: only the 2025 one's folder is asked for.
    assert [path for path, _ in plex["asked"]] == ["/library/sections/2/search", "/library/metadata/6002"]


@pytest.mark.asyncio
async def test_plexs_global_search_finds_many_and_takes_none_of_the_others(plex):
    plex["libraries"], plex["metadata"] = HALHUB_LIBRARIES, BUTTERFLY_RECORDS
    films = "/home/hal9000/HALHUB/Misc/Movies2"
    plex["global"] = {"Butterfly": [
        _movie(2008, file=f"{films}/Barbie Mariposa (2008)/b.mkv", key="7001",
               title="Barbie Mariposa & Her Butterfly Fairy Friends"),
        _movie(2017, file=f"{films}/Black Butterfly (2017)/b.mkv", key="7002", title="Black Butterfly"),
        _movie(1975, file=f"{films}/Butterfly (1975)/b.mkv", key="7003", title="Butterfly"),
        _movie(2025, key="7004", title="Butterfly"),  # a film of the name and year, where Plex doesn't say
        BUTTERFLY_2018,
        {"ratingKey": "7005", "type": "episode", "title": "Butterfly", "year": 2025,
         "thumb": "/library/metadata/7005/thumb/1"},
    ]}
    assert await _resolve(BUTTERFLY, HALHUB) == (None, "placeholder", {})
    plex["global"]["Butterfly"].append(BUTTERFLY_2025)
    assert await _resolve(BUTTERFLY, HALHUB) == ("6002", "plex", {"media_type": "tv"})
    assert not any(path.startswith("/library/metadata/") for path, _ in plex["asked"])


# --- Plex matches made before ----------------------------------------------------

@pytest.mark.asyncio
async def test_plex_matches_are_looked_up_again_once(posters, test_db, monkeypatch):
    async with aiosqlite.connect(test_db) as db:
        await db.executemany(
            "INSERT INTO poster_cache (folder_path, title, source, media_type, image_data) "
            "VALUES (?, ?, ?, 'movie', 'aW1n')",
            [("/m/movies/Buddy (2026)", "Buddy", "plex"), ("/m/movies/Picked", "Picked", "tmdb-manual"),
             ("/m/movies/Found", "Found", "tmdb")])
        await db.commit()
    searched = []

    async def search(title, year, key, media_type_hint=None, **kw):
        searched.append(title)
        return None, "placeholder", {}
    prefetches = []

    async def start_prefetch(scheduled=False):
        prefetches.append(scheduled)
    monkeypatch.setattr(posters, "_resolve_tmdb_search", search)
    monkeypatch.setattr(posters, "start_prefetch", start_prefetch)
    paths = ["/m/movies/Buddy (2026)", "/m/movies/Picked", "/m/movies/Found"]
    got = await posters.resolve_posters(posters.ResolveRequest(paths=paths))
    assert searched == ["Buddy"] and got["/m/movies/Buddy (2026)"]["source"] == "placeholder"
    # The rest of them, and their details, found again now: not in up to 3 hours.
    assert prefetches == [False]
    assert (got["/m/movies/Picked"]["source"], got["/m/movies/Found"]["source"]) == ("tmdb-manual", "tmdb")
    # Once: a Plex match made now stays.
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE poster_cache SET source = 'plex', image_data = 'aW1n', media_type = 'movie' "
                         "WHERE folder_path = '/m/movies/Buddy (2026)'")
        await db.commit()
    got = await posters.resolve_posters(posters.ResolveRequest(paths=paths))
    assert searched == ["Buddy"] and got["/m/movies/Buddy (2026)"]["source"] == "plex"
    assert prefetches == [False]


@pytest.mark.asyncio
async def test_the_prefetch_purges_plex_matches_too(posters, test_db, monkeypatch):
    async with aiosqlite.connect(test_db) as db:
        await db.executemany("INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, 1, 'x')",
                             [("/m/movies/Buddy (2026)/b.mkv",), ("/m/movies/Picked/p.mkv",)])
        await db.executemany(
            "INSERT INTO poster_cache (folder_path, title, source, image_data) VALUES (?, ?, ?, 'aW1n')",
            [("/m/movies/Buddy (2026)", "Buddy", "plex"), ("/m/movies/Picked", "Picked", "tmdb-manual")])
        await db.commit()
    searched = []

    async def search(title, year, key, media_type_hint=None, **kw):
        searched.append(title)
        return "https://img/x.jpg", "tmdb", {"media_type": "movie", "tmdb_id": 1}

    async def details(tmdb_id, media_type, key):
        return {"imdb_id": "", "country": "Iceland", "genres": "", "year": ""}

    async def no_backfill(*a):
        return 0
    monkeypatch.setattr(posters, "_resolve_tmdb_search", search)
    monkeypatch.setattr(posters, "_tmdb_details", details)
    monkeypatch.setattr(posters, "_backfill_details", no_backfill)
    await posters._run_prefetch()
    assert searched == ["Buddy"]  # a Plex match was never looked up again here
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT folder_path, source FROM poster_cache ORDER BY folder_path") as cur:
            assert await cur.fetchall() == [("/m/movies/Buddy (2026)", "tmdb"), ("/m/movies/Picked", "tmdb-manual")]


# --- TMDB finds the title, Plex gives its poster: the grid and the prefetch ------

PLEX_SETTINGS = [("plex_url", PLEX), ("plex_token", "tok"), ("plex_path_mapping", MAPPING)]
HEAT = "/media/Misc/Heat (1995)"
CHASE = "/media/Misc/Chase (2010) [tvdb-163541]"  # a show, in a films library


def _plex_poster(key):
    return f"/api/posters/image?path=%2Flibrary%2Fmetadata%2F{key}%2Fthumb%2F1"


async def _plex_library(db_path, folders, rows=()):
    async with aiosqlite.connect(db_path) as db:
        await db.executemany("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", PLEX_SETTINGS)
        await db.executemany("INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, 1, 'x')",
                             [(f + "/file.mkv",) for f in folders])
        await db.executemany("INSERT INTO poster_cache (folder_path, title, source, poster_url, media_type, "
                             "image_data) VALUES (?, ?, 'plex', ?, 'movie', 'aW1n')", rows)
        await db.commit()


async def _poster_rows(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT folder_path, source, poster_url, media_type FROM poster_cache") as cur:
            return {r[0]: r[1:] for r in await cur.fetchall()}


async def _no_backfill(*a):
    return 0


async def _no_prefetch(*a, **kw):
    return {"status": "started"}


BUDDY_2026 = {"id": 12, "title": "Buddy", "release_date": "2026-04-17", "poster_path": "/buddy.jpg"}
HEAT_1995 = {"id": 949, "title": "Heat", "release_date": "1995-12-15", "poster_path": "/heat.jpg",
             "imdb_id": "tt0113277"}


def _tmdb_knows(posters, monkeypatch):
    """TMDB as it answers for FOLDER (Buddy 2026: /find doesn't know its
    IMDb id, its search does), Heat 1995 (its record, its search) and
    Chase (a show, found by its TVDB id); the paths asked."""
    asked = []

    async def get(client, url, params):
        path = url.split("/3/")[1]
        asked.append(path)
        answers = {
            "find/163541": {"movie_results": [], "tv_results": [
                {"id": 5, "name": "Chase", "first_air_date": "2010-09-20", "poster_path": "/chase.jpg"}]},
            "tv/5": {"first_air_date": "2010-09-20", "origin_country": ["US"]},
            "search/movie": {"results": [dict(BUDDY_2026)] if params.get("query") == "Buddy" else []},
            "search/multi": {"results": [{**HEAT_1995, "media_type": "movie"}] if params.get("query") == "Heat" else []},
            "movie/12": {"imdb_id": "tt37281055", "release_date": "2026-04-17"},
            "movie/949": dict(HEAT_1995),
        }
        if path in answers:
            return httpx.Response(200, json=answers[path])
        return httpx.Response(404 if path.startswith(("movie/", "tv/")) else 200,
                              json={"results": [], "movie_results": [], "tv_results": []})
    monkeypatch.setattr(posters, "_tmdb_get", get)
    return asked


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["grid", "prefetch"])
async def test_plex_gives_the_poster_of_the_title_tmdb_found(posters, plex, test_db, monkeypatch, via):
    """Which title a folder is, TMDB says; Plex only supplies its poster —
    the grid and the prefetch alike. Plex matches made before are looked up
    again: a right one comes back with its Plex poster, a wrong one (the
    1997 Buddy) is replaced."""
    await _plex_library(test_db, [FOLDER, HEAT, CHASE],
                        [(FOLDER, "Buddy", _plex_poster(100)), (HEAT, "Heat", _plex_poster(200))])
    plex["section"] = {
        # Buddy: Plex has the 1997 film elsewhere and this folder's file, without ids.
        "Buddy": [_movie(1997, file="/data/misc/Buddy (1997)/Buddy.mkv", key="100"),
                  _movie(2026, file="/data/misc/HD 2025/Buddy (2026) [tt37281055]/b.mkv", key="101")],
        # Heat: its file here, with its ids — they find it on TMDB, in one request.
        "Heat": [_movie(1995, file="/data/misc/Heat (1995)/Heat.mkv", key="200", title="Heat",
                        guids=["imdb://tt0113277", "tmdb://949"])],
        # Chase, a show in a films library: Plex's film of the name isn't its poster.
        "Chase": [_movie(2010, file="/data/misc/Chase (2010) [tvdb-163541]/Chase.mkv", key="300", title="Chase")]}
    asked = _tmdb_knows(posters, monkeypatch)
    monkeypatch.setattr(posters, "_backfill_details", _no_backfill)
    monkeypatch.setattr(posters, "start_prefetch", _no_prefetch)
    if via == "grid":
        await posters.resolve_posters(posters.ResolveRequest(paths=[FOLDER, HEAT, CHASE]))
    else:
        await posters._run_prefetch()
    assert await _poster_rows(test_db) == {
        FOLDER: ("tmdb", _plex_poster(101), "movie"), HEAT: ("tmdb", _plex_poster(200), "movie"),
        CHASE: ("tmdb", "https://image.tmdb.org/t/p/w300/chase.jpg", "tv")}
    assert sorted(asked) == ["find/163541", "find/tt37281055", "movie/12", "movie/949", "search/movie", "tv/5"]
    assert plex["libraries_read"] == 1  # once, not for each title


@pytest.mark.asyncio
async def test_with_plex_the_prefetch_finds_titles_without_a_tmdb_key(posters, plex, test_db, monkeypatch):
    monkeypatch.delenv("SHRINKERR_TMDB_API_KEY")
    monkeypatch.setattr(posters, "_backfill_details", _no_backfill)
    await _plex_library(test_db, [HEAT])
    plex["section"] = {"Heat": [_movie(1995, file="/data/misc/Heat (1995)/Heat.mkv", key="200", title="Heat")]}
    await posters._run_prefetch()
    assert await _poster_rows(test_db) == {HEAT: ("plex", _plex_poster(200), "movie")}


@pytest.mark.asyncio
async def test_the_prefetch_still_waits_after_a_rate_limit_and_the_grid_doesnt(posters, test_db, monkeypatch):
    import asyncio
    waits = []
    real_sleep = asyncio.sleep

    async def sleep(seconds, *a, **kw):
        waits.append(seconds)
        await real_sleep(0)

    async def search(*a, **kw):
        raise RuntimeError("TMDB returned 429")
    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(posters, "_resolve_tmdb_search", search)
    monkeypatch.setattr(posters, "_backfill_details", _no_backfill)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) "
                         "VALUES ('/m/movies/Film (2001)/f.mkv', 1, 'x')")
        await db.commit()
    await posters._run_prefetch()
    assert waits == [0.35, 5]  # its pace, then a rate limit's wait
    waits.clear()
    await posters.resolve_posters(posters.ResolveRequest(paths=["/m/movies/Other (2002)"]))
    assert waits == []



@pytest.mark.asyncio
@pytest.mark.parametrize("down", ["libraries", "search"])
async def test_plex_not_answering_keeps_tmdbs_poster_until_it_does(posters, plex, test_db, monkeypatch, down):
    """A Plex outage never keeps a title from being stored: TMDB's poster,
    and Plex's asked for by the next prefetch — Plex only, TMDB isn't asked
    and the match stays as it is."""
    import backend.plex as plex_mod
    await _plex_library(test_db, [HEAT])
    if down == "libraries":
        async def unreachable(url, token):
            raise httpx.ConnectError("refused")
        monkeypatch.setattr(plex_mod, "get_plex_libraries", unreachable)
    else:
        plex["section"] = 500
    asked = _tmdb_knows(posters, monkeypatch)
    monkeypatch.setattr(posters, "_backfill_details", _no_backfill)
    got = (await posters.resolve_posters(posters.ResolveRequest(paths=[HEAT])))[HEAT]
    assert (got["source"], got["imdb_id"]) == ("tmdb", "tt0113277")
    rows = await _poster_rows(test_db)
    assert rows == {HEAT: ("tmdb", "https://image.tmdb.org/t/p/w300/heat.jpg", "movie")}
    asked.clear()
    plex["section"] = {"Heat": [_movie(1995, file="/data/misc/Heat (1995)/Heat.mkv", key="200", title="Heat",
                                       guids=["imdb://tt0113277"])]}
    monkeypatch.setattr(plex_mod, "get_plex_libraries", lambda url, token: _libraries())

    async def download(url, plex_url="", plex_token=""):
        return "cGxleA==" if url.startswith("/api/posters/image") else None
    monkeypatch.setattr(posters, "_download_image", download)
    await posters._run_prefetch()  # Plex back: its poster
    assert await _poster_rows(test_db) == {HEAT: ("tmdb", _plex_poster(200), "movie")}
    assert asked == []
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT art_retry, image_data, imdb_id FROM poster_cache") as cur:
            assert await cur.fetchall() == [(0, "cGxleA==", "tt0113277")]


async def _libraries():
    return LIBRARIES



@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["grid", "prefetch"])
async def test_a_match_found_after_a_lookup_that_failed_is_stored(posters, test_db, monkeypatch, via):
    """The IMDb id's lookup was rate-limited; the title search found it."""
    async def get(client, url, params):
        if "/find/" in url:
            return httpx.Response(503, json={})
        if "/search/" in url:
            return httpx.Response(200, json={"results": [dict(BUDDIES[1])]})
        return httpx.Response(200, json={"imdb_id": "tt37281055", "origin_country": ["IS"]})
    monkeypatch.setattr(posters, "_tmdb_get", get)
    monkeypatch.setattr(posters, "_backfill_details", _no_backfill)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, 1, 'x')",
                         (BUDDY_FOLDER + "/Buddy.mkv",))
        await db.commit()
    if via == "grid":
        await posters.resolve_posters(posters.ResolveRequest(paths=[BUDDY_FOLDER]))
    else:
        await posters._run_prefetch()
    assert await _poster_rows(test_db) == {
        BUDDY_FOLDER: ("tmdb", "https://image.tmdb.org/t/p/w300/right.jpg", "movie")}
