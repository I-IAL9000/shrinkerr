"""A title's credits (actors, directors, studios) come with its TMDB
details, and every Plex label is synced: the Title menu's lists."""
import json

import aiosqlite
import pytest

from backend.routes.posters import CREDITS_ACTORS, DETAILS_VERSION, _credits_of, _details_append, _details_of


def test_a_films_credits():
    data = {"credits": {
        "cast": [{"name": f"Actor {n}", "order": n} for n in reversed(range(14))] + [{"name": "Actor 3", "order": 20}],
        "crew": [{"name": "Di Rector", "job": "Director"}, {"name": "Pro Ducer", "job": "Producer"},
                 {"name": "Co Director", "job": "Director"}]},
        "production_companies": [{"name": "Acme, Inc."}, {"name": ""}, {"name": "Zeta"}]}
    credits = json.loads(_credits_of(data, tv=False))
    assert credits == {"actor": [f"Actor {n}" for n in range(CREDITS_ACTORS)],  # top-billed, once each
                       "director": ["Di Rector", "Co Director"], "studio": ["Acme, Inc.", "Zeta"]}
    assert json.loads(_details_of(data, tv=False)["credits"]) == credits
    assert _details_append(False) == "external_ids,credits"


def test_a_shows_credits_are_its_whole_runs_cast_and_its_creators():
    data = {"aggregate_credits": {"cast": [{"name": "Lead", "order": 0}, {"name": "Guest"}]},
            "credits": {"cast": [{"name": "This season only", "order": 0}]},
            "created_by": [{"name": "Show Runner"}], "production_companies": []}
    assert json.loads(_credits_of(data, tv=True)) == {"actor": ["Lead", "Guest"], "director": ["Show Runner"]}
    assert _details_append(True) == "external_ids,aggregate_credits"


def test_no_credits_is_nothing():
    assert _credits_of({}, tv=False) == "" and _details_of({}, tv=True)["credits"] == ""
    assert DETAILS_VERSION == 4  # titles stored before are fetched again


@pytest.mark.asyncio
async def test_the_details_backfill_stores_the_credits(test_db, monkeypatch):
    from backend.routes import posters
    monkeypatch.setattr(posters, "DB_PATH", test_db)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO poster_cache (folder_path, title, year, source, media_type, details_v, credits, "
                         "resolved_at) VALUES ('/m/Heat (1995)', 'Heat', '1995', 'tmdb', 'movie', 3, NULL, 't')")
        await db.commit()

    async def find(*a, **k):
        return None, "tmdb", {"tmdb_id": 949, "media_type": "movie"}

    async def details(tmdb_id, media_type, api_key):
        return _details_of({"credits": {"cast": [{"name": "Al Pacino", "order": 0}]}}, False)
    monkeypatch.setattr(posters, "_find_on_tmdb", find)
    monkeypatch.setattr(posters, "_tmdb_details", details)
    monkeypatch.setattr(posters.asyncio, "sleep", lambda *_: _none())
    assert await posters._backfill_details("key", lambda path: "movie") == 1
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT credits, details_v FROM poster_cache") as cur:
            assert await cur.fetchone() == ('{"actor": ["Al Pacino"]}', DETAILS_VERSION)


async def _none():
    return None


@pytest.mark.asyncio
async def test_every_plex_label_is_synced(test_db, monkeypatch):
    import backend.plex as plex

    async def settings():
        return "http://plex", "tok", ""

    async def nothing(*a, **k):
        return None

    async def options():
        return {"labels": ["Keep 4K", "Kids"], "collections": [], "libraries": []}

    async def folders(label):
        return {"Keep 4K": ["/m/Heat (1995)"], "Kids": ["/m/Up (2009)/"]}[label]

    async def watch():
        return {"watched": [], "unwatched": []}
    monkeypatch.setattr(plex, "_get_plex_settings", settings)
    monkeypatch.setattr(plex, "sync_watch_activity", nothing)
    monkeypatch.setattr(plex, "get_available_plex_options", options)
    monkeypatch.setattr(plex, "get_folders_by_label", folders)
    monkeypatch.setattr(plex, "get_watch_status_folders", watch)
    assert (await plex.sync_plex_metadata_cache())["labels_synced"] == 2  # no rule asks for either
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT folder_path, metadata_value FROM plex_metadata_cache "
                              "WHERE metadata_type = 'label' ORDER BY 1") as cur:
            assert await cur.fetchall() == [("/m/Heat (1995)/", "Keep 4K"), ("/m/Up (2009)/", "Kids")]
