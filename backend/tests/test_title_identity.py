"""TMDB decides which title a folder is; Plex only supplies its artwork
(v0.10.0).

Most folders have no ids in their names, so the title search is the main
way, and it reads release-style names, season folders and loose files. A
title is found by the ids in its name, else by the ids a media server
(Plex) gave the files in its folder — only when that item's title and year
are the folder's — else by its title. Its poster is Plex's when Plex has
that title (its ids agree, or its files are in the folder), else TMDB's;
when Plex doesn't answer, TMDB's is stored and Plex's is asked for again
later. Without a TMDB key, Plex's own match (by its files, ids, or title
and year) is the fallback."""
import pytest

from backend.routes.posters import parse_folder_name


@pytest.mark.parametrize("path, title, year, tv", [
    ("/m/Movies/Movie.Name.2019.1080p.BluRay.x264-GRP", "Movie Name", "2019", False),
    ("/m/Movies/Movie Name (2019)", "Movie Name", "2019", False),
    ("/m/Movies/Movie Name [2019]", "Movie Name", "2019", False),
    ("/m/Movies/Movie.Name.2019.1080p.BluRay.x264-GRP.mkv", "Movie Name", "2019", False),  # a loose file
    ("/m/Movies/Movie Name (2019) [imdbid-tt1234567]", "Movie Name", "2019", False),      # Jellyfin's ids
    ("/m/Movies/Heat", "Heat", None, False),
    ("/m/Movies/Jagten.2012.DANISH.1080p.BluRay.x264-GRP", "Jagten", "2012", False),
    ("/m/Movies/Blade.Runner.2049.2017.2160p.UHD.BluRay.x265-GRP", "Blade Runner 2049", "2017", False),
    ("/m/TV/Show Name/Season 01", "Show Name", None, True),
    ("/m/TV/Dark/Staffel 1", "Dark", None, True),
    ("/m/TV/Les Revenants (2012)/Saison 2", "Les Revenants", "2012", True),
    ("/m/TV/Show Name/S01", "Show Name", None, True),
    ("/m/TV/Show.Name.S01.1080p.WEB-DL.DDP5.1.H.264-GRP", "Show Name", None, True),  # a season pack
    ("/m/TV/The.Office.US.S02-S03.1080p.BluRay.x264-GRP", "The Office US", None, True),
    ("/m/TV/Show.Name.S01E01.1080p.WEB.h264-GRP.mkv", "Show Name", None, True),        # a loose episode
    # Films whose names start with a season word, or end like a season pack.
    ("/m/Movies/Special Forces (2011)", "Special Forces", "2011", False),
    ("/m/Movies/Special ID (2013)", "Special ID", "2013", False),
    ("/m/Movies/Temporada de patos (2004)", "Temporada de patos", "2004", False),
    ("/m/Movies/Saison (2020)", "Saison", "2020", False),
    ("/m/Movies/Season of the Witch (2011)", "Season of the Witch", "2011", False),
    ("/m/Movies/Series 7 The Contenders (2001)", "Series 7 The Contenders", "2001", False),
    ("/m/Movies/Model S1 (2020)", "Model S1", "2020", False),
    ("/m/Movies/Area 51 [1080]", "Area 51", None, False),  # not a year
    ("/m/TV/Show Name/Specials", "Show Name", None, True),
])
def test_release_and_library_names_give_the_title(path, title, year, tv):
    parsed = parse_folder_name(path, walk_files=False)
    assert (parsed["title"], parsed["year"], parsed["tv"]) == (title, year, tv)


# --- The whole resolver, on folders as they are -------------------------------------

import aiosqlite  # noqa: E402
import httpx  # noqa: E402

from backend.tests.test_title_matching import (  # noqa: E402,F401 — the fake Plex and its helpers
    LIBRARIES, MAPPING, PLEX, PLEX_SETTINGS, _movie, _no_backfill, _no_prefetch, _plex_poster, _poster_rows,
    plex, plex_logs, posters)


def _film(tmdb_id, title, date, **kw):
    return {"id": tmdb_id, "title": title, "release_date": date, "poster_path": f"/{tmdb_id}.jpg", **kw}


def _tmdb(posters, monkeypatch, searches=None, records=None):
    """TMDB: `searches` {query: results} for its title searches, `records`
    {tmdb_id: record} for /movie/{id} and /tv/{id}. The requests, as
    (path, params)."""
    asked = []

    async def get(client, url, params):
        path = url.split("/3/")[1]
        asked.append((path, {k: v for k, v in params.items() if k != "api_key"}))
        if path.startswith("search/"):
            return httpx.Response(200, json={"results": [dict(r) for r in (searches or {}).get(params["query"], [])]})
        if path.startswith("find/"):
            return httpx.Response(200, json={"movie_results": [], "tv_results": []})
        record = (records or {}).get(int(path.split("/")[1]))
        return httpx.Response(200, json=record) if record is not None else httpx.Response(404, json={})
    monkeypatch.setattr(posters, "_tmdb_get", get)
    monkeypatch.setattr(posters, "_backfill_details", _no_backfill)
    return asked


async def _library(db_path, files, labels=(), plex_on=False):
    """`files`: (path, size, duration in seconds); `labels`: (media folder, label)."""
    async with aiosqlite.connect(db_path) as db:
        await db.executemany("INSERT INTO media_dirs (path, label) VALUES (?, ?)", labels)
        await db.executemany("INSERT INTO scan_results (file_path, file_size, duration, scan_timestamp) "
                             "VALUES (?, ?, ?, 'x')", files)
        if plex_on:
            await db.executemany("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", PLEX_SETTINGS)
        await db.commit()


async def _stored(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT folder_path, source, poster_url, media_type, art_retry FROM poster_cache") as cur:
            return {r[0]: r[1:] for r in await cur.fetchall()}


def _tmdb_poster(tmdb_id):
    return f"https://image.tmdb.org/t/p/w300/{tmdb_id}.jpg"


async def _grid(posters, *paths):
    return await posters.resolve_posters(posters.ResolveRequest(paths=list(paths)))


@pytest.mark.asyncio
async def test_a_release_named_folder_is_found_by_its_title_and_year(posters, test_db, monkeypatch):
    folder = "/m/Movies/Movie.Name.2019.1080p.BluRay.x264-GRP"
    await _library(test_db, [(folder + "/movie.name.2019.mkv", 9, 6000)], [("/m/Movies", "Movies")])
    asked = _tmdb(posters, monkeypatch, {"Movie Name": [_film(1, "Movie Name", "2019-05-01")]},
                  {1: {"imdb_id": "tt0000001"}})
    got = (await _grid(posters, folder))[folder]
    assert (got["source"], got["imdb_id"]) == ("tmdb", "tt0000001")
    # A clear match costs no checks: the search and the details stored.
    assert asked == [("search/movie", {"query": "Movie Name", "year": "2019"}),
                     ("movie/1", {"append_to_response": "external_ids"})]


@pytest.mark.asyncio
async def test_a_clear_match_needs_no_check_and_is_stored_without_its_details(posters, test_db, monkeypatch):
    folder = "/m/Movies/Movie Name (2019)"
    await _library(test_db, [(folder + "/m.mkv", 9, 6000)], [("/m/Movies", "Movies")])
    _tmdb(posters, monkeypatch, {"Movie Name": [_film(1, "Movie Name", "2019-05-01")]})

    async def details_down(client, url, params, real=posters._tmdb_get):
        return httpx.Response(503, json={}) if "/movie/" in url else await real(client, url, params)
    monkeypatch.setattr(posters, "_tmdb_get", details_down)  # its details: TMDB doesn't answer
    await _grid(posters, folder)
    assert (await _stored(test_db))[folder] == ("tmdb", _tmdb_poster(1), "movie", 0)  # details next time


@pytest.mark.asyncio
async def test_namesakes_matching_less_well_ask_for_no_checks(posters, test_db, monkeypatch):
    """"Buddy (2026)": the film of that title and year is the match;
    "Comedy Buddy" and "Gym Buddy" of the year don't make it doubtful."""
    folder = "/m/Movies/Buddy (2026)"
    await _library(test_db, [(folder + "/b.mkv", 9, 90 * 60)], [("/m/Movies", "Movies")])  # Comedy Buddy's runtime
    asked = _tmdb(posters, monkeypatch, {"Buddy": [_film(30, "Comedy Buddy", "2026-01-09"),
                                                   _film(12, "Buddy", "2026-04-17"),
                                                   _film(31, "Gym Buddy", "2026-02-20")]},
                  {12: {"runtime": 118}, 30: {"runtime": 90}, 31: {"runtime": 100}})
    await posters._run_prefetch()
    assert [path for path, _ in asked] == ["search/movie", "movie/12"]
    assert (await _stored(test_db))[folder][1] == _tmdb_poster(12)


@pytest.mark.asyncio
async def test_a_show_named_with_an_imdb_id_in_a_tv_library_is_searched_as_a_show(posters, test_db, monkeypatch):
    folder = "/m/TV/Show Name [tt0944947]"
    await _library(test_db, [(folder + "/e.mkv", 1, None)], [("/m/TV", "TV Shows")])
    asked = _tmdb(posters, monkeypatch, {"Show Name": [{"id": 5, "name": "Show Name", "first_air_date": "2011-04-17"}]},
                  {5: {}})
    await posters._run_prefetch()
    assert asked[1] == ("search/tv", {"query": "Show Name"})  # after /find, which doesn't know the id


@pytest.mark.parametrize("runtime, minutes, fits", [
    (170, 145, True), (170, 144, False),  # 15% of the film's runtime
    (50, 59, True), (50, 61, False),      # at least 10 minutes
    (None, 100, False),
])
def test_a_runtime_fits_within_15_percent_or_10_minutes(runtime, minutes, fits):
    from backend.routes.posters import _runtime_fits
    assert _runtime_fits(runtime, minutes * 60) is fits


@pytest.mark.asyncio
@pytest.mark.parametrize("files, key, query", [
    (["/m/Media/Dark/Staffel 1/Dark.S01E01.mkv"], "/m/Media/Dark/Staffel 1", "Dark"),
    (["/m/Media/Show Name/Show.Name.S01E02.mkv"], "/m/Media/Show Name", "Show Name"),  # episodes: a show
    (["/m/Media/Show.Name.S02.1080p.WEB-DL-GRP/e1.mkv"], "/m/Media/Show.Name.S02.1080p.WEB-DL-GRP", "Show Name"),
])
async def test_seasons_and_episodes_are_a_shows(posters, test_db, monkeypatch, files, key, query):
    await _library(test_db, [(f, 1, 2700) for f in files])  # no label on the media folder
    asked = _tmdb(posters, monkeypatch, {query: [{"id": 5, "name": query, "first_air_date": "2017-12-01"},
                                                 {"id": 6, "name": query, "first_air_date": "2005-01-01"}]},
                  {5: {"first_air_date": "2017-12-01"}, 6: {}})
    await posters._run_prefetch()
    # A show: an episode's runtime tells nothing; TMDB's first, no checks.
    assert asked == [("search/tv", {"query": query}), ("tv/5", {"append_to_response": "external_ids"})]
    assert (await _stored(test_db))[key][:3] == ("tmdb", None, "tv")


@pytest.mark.asyncio
async def test_a_tmdb_id_in_the_name_can_be_a_shows(posters, test_db, monkeypatch):
    folder = "/m/Media/Game of Thrones {tmdb-1399}"
    await _library(test_db, [(folder + "/e.mkv", 1, 3600)])
    asked = _tmdb(posters, monkeypatch, records={1399: {"name": "Game of Thrones", "poster_path": "/got.jpg",
                                                        "external_ids": {"imdb_id": "tt0944947"}}})

    async def a_film_too(client, url, params, real=posters._tmdb_get):
        if url.endswith("/movie/1399"):  # films and shows are numbered apart: a film has the number too
            asked.append(("movie/1399", {}))
            return httpx.Response(200, json={"title": "A Film Numbered 1399", "poster_path": "/film.jpg"})
        return await real(client, url, params)
    monkeypatch.setattr(posters, "_tmdb_get", a_film_too)
    await posters._run_prefetch()
    assert [path for path, _ in asked] == ["movie/1399", "tv/1399"]  # the show's title is the folder's
    assert (await _stored(test_db))[folder] == ("tmdb", "https://image.tmdb.org/t/p/w300/got.jpg", "tv", 0)


@pytest.mark.asyncio
async def test_people_found_by_a_search_are_never_the_title(posters, test_db, monkeypatch):
    await _library(test_db, [("/m/Media/Heat/heat.mkv", 9, None)])  # no label, no year: /search/multi
    _tmdb(posters, monkeypatch, {"Heat": [{"id": 77, "name": "Heat", "media_type": "person"},
                                          {**_film(949, "Heat", "1995-12-15"), "media_type": "movie"}]}, {949: {}})
    await posters._run_prefetch()
    assert (await _stored(test_db))["/m/Media/Heat"][:2] == ("tmdb", _tmdb_poster(949))


@pytest.mark.asyncio
async def test_a_loose_release_file_is_its_own_title(posters, test_db, monkeypatch):
    loose = "/m/Movies/Saw.II.2005.1080p.BluRay.x264.mkv"
    await _library(test_db, [(loose, 9, 5600)], [("/m/Movies", "Movies")])
    asked = _tmdb(posters, monkeypatch, {"Saw II": [_film(215, "Saw II", "2005-10-28")]}, {215: {}})
    await posters._run_prefetch()
    assert asked[0] == ("search/movie", {"query": "Saw II", "year": "2005"})
    assert (await _stored(test_db))[loose] == ("tmdb", _tmdb_poster(215), "movie", 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("main, search", [
    ("Heat.1995.1080p.BluRay.x264.mkv", {"query": "Heat", "year": "1995"}),
    ("Collateral.2004.1080p.mkv", {"query": "Heat"}),  # another title's year isn't the folder's
])
async def test_the_year_can_be_in_the_file_name(posters, test_db, monkeypatch, main, search):
    await _library(test_db, [(f"/m/Movies/Heat/{main}", 9, None),
                             ("/m/Movies/Heat/Heat.Featurette.mkv", 1, 600)], [("/m/Movies", "Movies")])
    asked = _tmdb(posters, monkeypatch, {"Heat": [_film(949, "Heat", "1995-12-15")]}, {949: {}})
    await posters._run_prefetch()
    assert asked[0] == ("search/movie", search)
    assert (await _stored(test_db))["/m/Movies/Heat"][1] == _tmdb_poster(949)


HEATS = [_film(2, "Heat", "1986-03-14"), _film(949, "Heat", "1995-12-15"), _film(3, "Heat", "1972-10-06")]
RUNTIMES = {2: {"runtime": 101}, 949: {"runtime": 170}, 3: {"runtime": 100}}


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["grid", "prefetch"])
@pytest.mark.parametrize("duration, found, asked_records", [
    (170 * 60 - 200, 949, ["movie/2", "movie/949"]),  # the 1995 remake's runtime: it
    (None, 2, ["movie/2"]),                           # unknown: TMDB's first, as before
    (60 * 60, 2, ["movie/2", "movie/949", "movie/3"]),  # none fits: TMDB's first, no more asked
])
async def test_a_remake_without_a_year_is_told_by_its_runtime(posters, test_db, monkeypatch, duration, found,
                                                              asked_records, via):
    await _library(test_db, [("/m/Movies/Heat/heat.mkv", 9, duration), ("/m/Movies/Heat/trailer.mkv", 1, 150)],
                   [("/m/Movies", "Movies")])
    asked = _tmdb(posters, monkeypatch, {"Heat": HEATS}, RUNTIMES)
    if via == "grid":
        await _grid(posters, "/m/Movies/Heat")
    else:
        await posters._run_prefetch()
    assert (await _stored(test_db))["/m/Movies/Heat"][1] == _tmdb_poster(found)
    assert [path for path, _ in asked[1:]] == asked_records  # the chosen one's details not asked twice


@pytest.mark.asyncio
@pytest.mark.parametrize("files, runtimes, found, asked_records", [
    # The best match's runtime unknown (new, obscure entries): it stays.
    ([("heat.mkv", 9, 170 * 60)], {2: {}, 949: {"runtime": 170}}, 2, ["movie/2"]),
    # Longer than the best match (an extended cut, +35%), and no other fits: it stays.
    ([("heat.mkv", 9, 136 * 60)], {2: {"runtime": 101}, 949: {"runtime": 170}, 3: {"runtime": 100}}, 2,
     ["movie/2", "movie/949", "movie/3"]),
    # In two parts: their runtimes together, not the bigger part's.
    ([("Heat.cd1.avi", 9, 85 * 60), ("Heat.cd2.avi", 8, 85 * 60)],
     {2: {"runtime": 101}, 949: {"runtime": 170}, 3: {"runtime": 100}}, 949, ["movie/2", "movie/949"]),
])
async def test_the_best_match_is_left_only_when_its_runtime_is_known_and_another(
        posters, test_db, monkeypatch, files, runtimes, found, asked_records):
    await _library(test_db, [(f"/m/Movies/Heat/{name}", size, d) for name, size, d in files], [("/m/Movies", "Movies")])
    asked = _tmdb(posters, monkeypatch, {"Heat": HEATS}, runtimes)
    await posters._run_prefetch()
    assert (await _stored(test_db))["/m/Movies/Heat"][1] == _tmdb_poster(found)
    assert [path for path, _ in asked[1:]] == asked_records


@pytest.mark.asyncio
async def test_two_films_of_a_title_and_year_are_told_by_their_runtimes(posters, test_db, monkeypatch):
    await _library(test_db, [("/m/Movies/Buddy (2026)/buddy.mkv", 9, 118 * 60)], [("/m/Movies", "Movies")])
    _tmdb(posters, monkeypatch, {"Buddy": [_film(11, "Buddy", "2026-02-01"), _film(12, "Buddy", "2026-04-17")]},
          {11: {"runtime": 95}, 12: {"runtime": 118}})
    await posters._run_prefetch()
    assert (await _stored(test_db))["/m/Movies/Buddy (2026)"][1] == _tmdb_poster(12)


@pytest.mark.asyncio
async def test_a_film_by_its_own_language_name(posters, test_db, monkeypatch):
    folder = "/m/Movies/Jagten.2012.DANISH.1080p.BluRay.x264-GRP"
    await _library(test_db, [(folder + "/jagten.mkv", 9, 6900)], [("/m/Movies", "Movies")])
    _tmdb(posters, monkeypatch, {"Jagten": [_film(103663, "The Hunt", "2012-01-10", original_title="Jagten")]},
          {103663: {}})
    await posters._run_prefetch()
    assert (await _stored(test_db))[folder][:2] == ("tmdb", _tmdb_poster(103663))


# --- Plex: the ids it gave the files, and its posters ----------------------------------------

BUDDY_DIR = "/media/Misc/HD 2025/Buddy (2026)"  # no ids in its name
BUDDY_FILE = "/data/misc/HD 2025/Buddy (2026)/Buddy.mkv"  # as Plex sees it


@pytest.mark.asyncio
async def test_plexs_ids_for_the_files_are_used_only_when_its_title_and_year_fit(posters, plex, test_db, monkeypatch):
    """Plex matched the new film's file to the 1997 one: its ids mustn't
    name the folder, nor its poster show."""
    await _library(test_db, [], plex_on=True)
    plex["section"] = {"Buddy": [_movie(1997, file=BUDDY_FILE, key="100", guids=["imdb://tt0118798", "tmdb://9622"])]}
    asked = _tmdb(posters, monkeypatch, {"Buddy": [_film(12, "Buddy", "2026-04-17")]}, {12: {}, 9622: {}})
    got = (await _grid(posters, BUDDY_DIR))[BUDDY_DIR]
    assert got["source"] == "tmdb" and await _stored(test_db) == {BUDDY_DIR: ("tmdb", _tmdb_poster(12), "movie", 0)}
    assert [path for path, _ in asked] == ["search/multi", "movie/12"]  # not Plex's movie/9622
    # Plex's match right: its ids find the title, in one request, and its poster shows.
    async with aiosqlite.connect(test_db) as db:
        await db.execute("DELETE FROM poster_cache")
        await db.commit()
    plex["section"] = {"Buddy": [_movie(2026, file=BUDDY_FILE, key="101", guids=["tmdb://12"])]}
    asked.clear()
    await _grid(posters, BUDDY_DIR)
    assert await _stored(test_db) == {BUDDY_DIR: ("tmdb", _plex_poster(101), "movie", 0)}
    assert [path for path, _ in asked] == ["movie/12"]


@pytest.mark.asyncio
@pytest.mark.parametrize("folder, item", [
    # Its ids contradict the folder's (TMDB's /find doesn't know the new film's).
    ("/media/Misc/HD 2025/Buddy (2026) [tt37281055]",
     _movie(2026, file="/data/misc/HD 2025/Buddy (2026) [tt37281055]/b.mkv", key="101",
            guids=["imdb://tt0000077", "tmdb://77"])),
    # Another film of the title and year, its files elsewhere: not this folder's.
    (BUDDY_DIR, _movie(2026, file="/data/misc/Other/Buddy (2026)/b.mkv", key="102", guids=["tmdb://77"])),
])
async def test_plexs_ids_name_only_the_title_in_the_folder(posters, plex, test_db, monkeypatch, folder, item):
    await _library(test_db, [], plex_on=True)
    plex["section"] = {"Buddy": [item]}
    asked = _tmdb(posters, monkeypatch, {"Buddy": [_film(12, "Buddy", "2026-04-17")]},
                  {12: {"imdb_id": "tt37281055"}, 77: {"imdb_id": "tt0000077"}})
    await _grid(posters, folder)
    assert "movie/77" not in [path for path, _ in asked]
    assert (await _stored(test_db))[folder][:2] == ("tmdb", _tmdb_poster(12))


@pytest.mark.asyncio
async def test_an_id_already_asked_about_isnt_asked_again(posters, plex, test_db, monkeypatch):
    """Plex's IMDb id for the files is the folder's: /find once."""
    folder = "/media/Misc/HD 2025/Buddy (2026) [tt37281055]"
    await _library(test_db, [], plex_on=True)
    plex["section"] = {"Buddy": [_movie(2026, file="/data/misc/HD 2025/Buddy (2026) [tt37281055]/b.mkv", key="101",
                                        guids=["imdb://tt37281055"])]}
    asked = _tmdb(posters, monkeypatch, {"Buddy": [_film(12, "Buddy", "2026-04-17")]}, {12: {"imdb_id": "tt37281055"}})
    await _grid(posters, folder)
    assert [path for path, _ in asked] == ["find/tt37281055", "search/movie", "movie/12"]


@pytest.mark.asyncio
async def test_plexs_poster_can_come_from_another_library(posters, plex, test_db, monkeypatch):
    """The title in Plex elsewhere (its 4K library, say): its ids are the
    title's, so its poster is."""
    await _library(test_db, [], plex_on=True)
    plex["global"] = {"Buddy": [_movie(2026, file="/data/4k/Buddy (2026)/b.mkv", key="400",
                                       guids=["imdb://tt37281055"])]}
    _tmdb(posters, monkeypatch, {"Buddy": [_film(12, "Buddy", "2026-04-17")]}, {12: {"imdb_id": "tt37281055"}})
    await _grid(posters, BUDDY_DIR)
    assert (await _stored(test_db))[BUDDY_DIR][1] == _plex_poster(400)


@pytest.mark.asyncio
async def test_plexs_poster_later_never_changes_the_title(posters, plex, test_db, monkeypatch):
    """Found while Plex was down; back, Plex has another film in the folder
    (its id isn't the title's): TMDB's poster stays, not asked again."""
    await _library(test_db, [(BUDDY_DIR + "/Buddy.mkv", 9, None)], plex_on=True)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO poster_cache (folder_path, title, year, source, poster_url, media_type, imdb_id, "
                         "image_data, art_retry, resolved_at) VALUES (?, 'Buddy', '2026', 'tmdb', ?, 'movie', "
                         "'tt37281055', 'aW1n', 1, 'r1')", (BUDDY_DIR, _tmdb_poster(12)))
        await db.commit()
    plex["section"] = {"Buddy": [_movie(1997, file=BUDDY_FILE, key="100", guids=["imdb://tt0118798"])]}
    asked = _tmdb(posters, monkeypatch)
    await posters._run_prefetch()
    assert await _stored(test_db) == {BUDDY_DIR: ("tmdb", _tmdb_poster(12), "movie", 0)} and asked == []


@pytest.mark.asyncio
async def test_plexs_poster_later_leaves_a_row_changed_meanwhile(posters, plex, test_db, monkeypatch):
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO poster_cache (folder_path, title, source, poster_url, media_type, imdb_id, "
                         "art_retry, resolved_at) VALUES (?, 'Buddy', 'tmdb', 'u', 'movie', 'tt37281055', 1, 'r2')",
                         (BUDDY_DIR,))
        await db.commit()
    plex["section"] = {"Buddy": [_movie(2026, file=BUDDY_FILE, key="101", guids=["imdb://tt37281055"])]}

    async def download(url, plex_url="", plex_token=""):
        return "cGxleA=="
    monkeypatch.setattr(posters, "_download_image", download)
    plex_for = await posters._plex_for(PLEX, "tok", MAPPING)
    read = {"folder_path": BUDDY_DIR, "title": "Buddy", "year": "2026", "media_type": "movie",
            "imdb_id": "tt37281055", "resolved_at": "r1"}  # as it was read, before a Fix match or a new lookup
    assert await posters._retry_plex_art([read], plex_for) == 1
    assert await _stored(test_db) == {BUDDY_DIR: ("tmdb", "u", "movie", 1)}  # not written over
    assert await posters._retry_plex_art([{**read, "resolved_at": "r2"}], plex_for) == 1
    assert await _stored(test_db) == {BUDDY_DIR: ("tmdb", _plex_poster(101), "movie", 0)}


@pytest.mark.asyncio
async def test_plex_failing_title_after_title_isnt_asked_for_the_rest(posters, plex, test_db, monkeypatch):
    """Plex restarting mid-run: every title is still stored, with TMDB's
    poster for now."""
    films = [f"/media/Misc/Film {i} (2001)" for i in range(12)]
    await _library(test_db, [(f + "/f.mkv", 9, None) for f in films], plex_on=True)
    plex["section"] = 500
    _tmdb(posters, monkeypatch, {f"Film {i}": [_film(100 + i, f"Film {i}", "2001-01-01")] for i in range(12)},
          {100 + i: {} for i in range(12)})
    await posters._run_prefetch()
    stored = await _stored(test_db)
    assert len(stored) == 12 and all(row[0] == "tmdb" and row[3] == 1 for row in stored.values())
    searched = [path for path, _ in plex["asked"] if path.startswith("/library/sections/")]
    assert posters._PLEX_FAILURES_STOP <= len(searched) < len(films)


@pytest.mark.asyncio
async def test_without_a_tmdb_key_plexs_own_match_is_kept(posters, plex, test_db, monkeypatch):
    monkeypatch.delenv("SHRINKERR_TMDB_API_KEY")
    await _library(test_db, [], plex_on=True)
    chase = "/media/Misc/Chase (2010) [tvdb-163541]"  # a show, in a films library
    plex["section"] = {"Buddy": [_movie(2026, file=BUDDY_FILE, key="101")],
                       "Chase": [_movie(2010, file="/data/misc/Chase (2010) [tvdb-163541]/c.mkv", key="300",
                                        title="Chase")]}
    got = await _grid(posters, BUDDY_DIR, chase)
    assert (got[BUDDY_DIR]["source"], got[chase]["source"]) == ("plex", "placeholder")  # Plex's film isn't the show
    assert (await _stored(test_db))[BUDDY_DIR] == ("plex", _plex_poster(101), "movie", 0)
    plex["section"] = 500  # Plex down, no TMDB: nothing stored, looked up again
    got = (await _grid(posters, "/media/Misc/Other (2001)"))["/media/Misc/Other (2001)"]
    assert got["source"] == "placeholder" and "/media/Misc/Other (2001)" not in await _stored(test_db)


@pytest.mark.asyncio
async def test_with_neither_titles_are_placeholders(posters, test_db, monkeypatch):
    monkeypatch.delenv("SHRINKERR_TMDB_API_KEY")
    await _library(test_db, [("/m/Movies/Heat (1995)/heat.mkv", 9, None)])
    assert (await _grid(posters, "/m/Movies/Heat (1995)"))["/m/Movies/Heat (1995)"]["source"] == "placeholder"
    await posters._run_prefetch()
    assert (await _stored(test_db))["/m/Movies/Heat (1995)"][0] == "placeholder"



@pytest.mark.asyncio
async def test_plex_failing_now_and_then_is_still_asked(posters, plex, test_db, monkeypatch):
    """Only titles in a row stop Plex being asked: every third one failing,
    the others get its poster."""
    names = [f"Film {i}" for i in range(21)]
    await _library(test_db, [(f"/media/Misc/{n} (2001)/f.mkv", 9, None) for n in names], plex_on=True)
    plex["section"] = {n: 500 if i % 3 == 0 else [
        _movie(2001, file=f"/data/misc/{n} (2001)/f.mkv", key=str(100 + i), title=n, guids=[f"tmdb://{100 + i}"])]
        for i, n in enumerate(names)}
    _tmdb(posters, monkeypatch, {n: [_film(100 + i, n, "2001-01-01")] for i, n in enumerate(names)},
          {100 + i: {} for i in range(21)})
    await posters._run_prefetch()
    stored = await _stored(test_db)
    assert sorted(row[3] for row in stored.values()) == [0] * 14 + [1] * 7


HOME_VIDEO = "/media/Misc/Family Trip (2019)"


@pytest.mark.asyncio
@pytest.mark.parametrize("plex_answers", [True, False])
async def test_a_title_tmdb_doesnt_know_keeps_plexs_own_match(posters, plex, test_db, monkeypatch, plex_answers):
    """A home video, a concert: TMDB doesn't know it, Plex has it with its
    own artwork — Plex's match (its hardened rules), as without a key."""
    await _library(test_db, [], plex_on=True)
    plex["section"] = {"Family Trip": [_movie(2019, file="/data/misc/Family Trip (2019)/trip.mkv", key="500",
                                              title="Family Trip")]} if plex_answers else 500
    asked = _tmdb(posters, monkeypatch)  # TMDB knows nothing
    got = (await _grid(posters, HOME_VIDEO))[HOME_VIDEO]
    assert [path for path, _ in asked] == ["search/multi"]
    if plex_answers:
        assert await _stored(test_db) == {HOME_VIDEO: ("plex", _plex_poster(500), "movie", 0)}
    else:  # Plex may have it: nothing stored, looked up again
        assert got["source"] == "placeholder" and await _stored(test_db) == {}


@pytest.mark.asyncio
async def test_a_tmdb_lookup_that_failed_isnt_answered_by_plex(posters, plex, test_db, monkeypatch):
    await _library(test_db, [], plex_on=True)
    plex["section"] = {"Family Trip": [_movie(2019, file="/data/misc/Family Trip (2019)/trip.mkv", key="500",
                                              title="Family Trip")]}

    async def down(client, url, params):
        return httpx.Response(503, json={})
    monkeypatch.setattr(posters, "_tmdb_get", down)
    got = (await _grid(posters, HOME_VIDEO))[HOME_VIDEO]
    assert got["source"] == "placeholder" and await _stored(test_db) == {}


@pytest.mark.asyncio
async def test_plexs_ids_for_the_files_later_find_the_title_again(posters, plex, test_db, monkeypatch):
    """Found by its title while Plex was down; back, Plex names the files
    here another film (its title and year fit): looked up again, not kept
    for good."""
    await _library(test_db, [(BUDDY_DIR + "/Buddy.mkv", 9, None)], plex_on=True)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO poster_cache (folder_path, title, year, source, poster_url, media_type, imdb_id, "
                         "image_data, details_v, art_retry, resolved_at) VALUES (?, 'Buddy', '2026', 'tmdb', ?, "
                         "'movie', 'tt37281055', 'aW1n', ?, 1, 'r1')",
                         (BUDDY_DIR, _tmdb_poster(12), posters.DETAILS_VERSION))
        await db.commit()
    plex["section"] = {"Buddy": [_movie(2026, file=BUDDY_FILE, key="177", guids=["imdb://tt0000077", "tmdb://77"])]}
    asked = _tmdb(posters, monkeypatch, records={77: {"title": "Buddy", "release_date": "2026-09-01",
                                                      "imdb_id": "tt0000077", "poster_path": "/77.jpg"}})
    await posters._run_prefetch()
    assert await _stored(test_db) == {} and asked == []  # Plex only; looked up again ...
    await posters._run_prefetch()
    assert await _stored(test_db) == {BUDDY_DIR: ("tmdb", _plex_poster(177), "movie", 0)}  # ... as Plex has it
    assert [path for path, _ in asked] == ["movie/77"]


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["grid", "prefetch", "backfill"])
async def test_the_titles_original_language_is_stored(posters, test_db, monkeypatch, via):
    """For the original-language lookup to take the poster's title's."""
    folder = "/m/Movies/Jagten (2012)"
    await _library(test_db, [(folder + "/j.mkv", 9, None)], [("/m/Movies", "Movies")])
    backfill = posters._backfill_details
    _tmdb(posters, monkeypatch, {"Jagten": [_film(103663, "The Hunt", "2012-01-10", original_title="Jagten",
                                                  original_language="da")]}, {103663: {}})
    if via == "grid":
        await _grid(posters, folder)
    elif via == "prefetch":
        await posters._run_prefetch()
    else:  # a title found before the language was stored
        async with aiosqlite.connect(test_db) as db:
            await db.execute("INSERT INTO poster_cache (folder_path, title, source, media_type, details_v) "
                             "VALUES (?, 'Jagten', 'tmdb', 'movie', 0)", (folder,))
            await db.commit()
        assert await backfill("k", lambda p: "movie") == 1
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT original_language FROM poster_cache") as cur:
            assert await cur.fetchall() == [("da",)]


@pytest.mark.asyncio
async def test_a_fix_match_stores_its_original_language(posters, test_db, monkeypatch):
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('tmdb_api_key', 'k')")
        await db.commit()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"title": "Druk", "original_language": "da"}))))
    await posters.override_poster(posters.OverrideRequest(folder_path="/m/Movies/Another Round", tmdb_id=580175,
                                                          media_type="movie"))
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT source, original_language FROM poster_cache") as cur:
            assert await cur.fetchall() == [("tmdb-manual", "da")]


@pytest.mark.asyncio
async def test_plex_rows_are_found_on_tmdb_once_a_key_is_there(posters, plex, test_db, monkeypatch):
    """Matched by Plex alone without a TMDB key; with one, the prefetch
    finds them on TMDB — once: a title TMDB doesn't know stays Plex's."""
    monkeypatch.delenv("SHRINKERR_TMDB_API_KEY")
    await _library(test_db, [(BUDDY_DIR + "/Buddy.mkv", 9, None), (HOME_VIDEO + "/trip.mkv", 9, None)], plex_on=True)
    plex["section"] = {"Buddy": [_movie(2026, file=BUDDY_FILE, key="101")],
                       "Family Trip": [_movie(2019, file="/data/misc/Family Trip (2019)/trip.mkv", key="500",
                                              title="Family Trip")]}
    await posters._run_prefetch()
    assert {k: v[0] for k, v in (await _stored(test_db)).items()} == {BUDDY_DIR: "plex", HOME_VIDEO: "plex"}
    monkeypatch.setenv("SHRINKERR_TMDB_API_KEY", "test-key")
    asked = _tmdb(posters, monkeypatch, {"Buddy": [_film(12, "Buddy", "2026-04-17")]}, {12: {}})
    await posters._run_prefetch()
    stored = await _stored(test_db)
    assert (stored[BUDDY_DIR], stored[HOME_VIDEO][0]) == (("tmdb", _plex_poster(101), "movie", 0), "plex")
    asked.clear()
    await posters._run_prefetch()
    assert asked == []  # not again
    # Without a key again, they would be again.
    monkeypatch.delenv("SHRINKERR_TMDB_API_KEY")
    await posters._run_prefetch()
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT 1 FROM settings WHERE key = ?", (posters._PLEX_ROWS_FOUND_FLAG,)) as cur:
            assert await cur.fetchall() == []


@pytest.mark.asyncio
async def test_a_title_found_again_is_left_when_changed_meanwhile(posters, plex, test_db, monkeypatch):
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO poster_cache (folder_path, title, year, source, poster_url, media_type, imdb_id, "
                         "art_retry, resolved_at) VALUES (?, 'Buddy', '2026', 'tmdb', 'u', 'movie', 'tt37281055', 1, "
                         "'r2')", (BUDDY_DIR,))
        await db.commit()
    plex["section"] = {"Buddy": [_movie(2026, file=BUDDY_FILE, key="177", guids=["imdb://tt0000077"])]}
    plex_for = await posters._plex_for(PLEX, "tok", MAPPING)
    read = {"folder_path": BUDDY_DIR, "title": "Buddy", "year": "2026", "media_type": "movie",
            "imdb_id": "tt37281055", "resolved_at": "r1"}
    await posters._retry_plex_art([read], plex_for)
    assert BUDDY_DIR in await _stored(test_db)  # a Fix match, say, meanwhile: kept
    await posters._retry_plex_art([{**read, "resolved_at": "r2"}], plex_for)
    assert await _stored(test_db) == {}


def test_a_season_folder_with_its_year_is_its_shows():
    """"Season 01 (2014)" is a season folder (TMDB was searched for "Season 01");
    a title starting with a season word still isn't."""
    from backend.routes.posters import parse_folder_name
    got = parse_folder_name("/m/TV/Fargo (2014)/Season 01 (2014)", walk_files=False)
    assert (got["title"], got["year"]) == ("Fargo", "2014")
    assert parse_folder_name("/m/TV/Dark/Staffel 2 [2019]", walk_files=False)["title"] == "Dark"
    assert parse_folder_name("/m/Movies/Season of the Witch (2011)", walk_files=False)["title"] == "Season of the Witch"
