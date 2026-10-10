"""When each title was last watched (v0.10.0), for "Not watched in N months":
read from Plex, Jellyfin and Emby after each full scan, one set of rows per
server. (What the filters match: test_filter_spec.)"""
import aiosqlite
import httpx
import pytest

import backend.watch_activity as watch_activity


@pytest.fixture
def db(test_db, monkeypatch):
    monkeypatch.setattr(watch_activity, "DB_PATH", test_db)
    return test_db


def _serve(monkeypatch, handler):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler)))


async def _rows(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT folder_path, server, last_viewed, added_at FROM watch_activity "
                              "ORDER BY server, folder_path") as cur:
            return [tuple(r) for r in await cur.fetchall()]


PLEX_MOVIES = """<MediaContainer>
  <Video title="Heat" lastViewedAt="1700000000" addedAt="1500000000">
    <Media><Part file="/data/movies/Heat (1995)/Heat.mkv"/></Media>
    <Media><Part file="/data/movies/Heat (1995) 4K/Heat.2160p.mkv"/></Media>
  </Video>
  <Video title="Dune" addedAt="1600000000"><Media><Part file="/data/movies/Dune/Dune.mkv"/></Media></Video>
</MediaContainer>"""
PLEX_SHOWS = """<MediaContainer>
  <Directory title="Show" lastViewedAt="1710000000" addedAt="1400000000"><Location path="/data/tv/Show"/></Directory>
</MediaContainer>"""


@pytest.mark.asyncio
async def test_plex_movies_and_shows(monkeypatch):
    import backend.plex as plex

    async def settings():
        return "http://plex", "tok", "/media/Movies=/data/movies;/media/TV=/data/tv"

    async def libraries(url, token):
        return [{"id": "1", "type": "movie"}, {"id": "2", "type": "show"}, {"id": "3", "type": "artist"}]
    monkeypatch.setattr(plex, "_get_plex_settings", settings)
    monkeypatch.setattr(plex, "get_plex_libraries", libraries)
    asked = []

    def handler(request):
        asked.append((request.url.path, request.url.params.get("type")))
        return httpx.Response(200, text={"1": PLEX_MOVIES, "2": PLEX_SHOWS}[request.url.path.split("/")[3]])
    _serve(monkeypatch, handler)
    assert await plex.get_watch_activity() == {
        "/media/Movies/Heat (1995)/": (1700000000, 1500000000),
        "/media/Movies/Heat (1995) 4K/": (1700000000, 1500000000),  # each version's folder
        "/media/Movies/Dune/": (None, 1600000000),  # never watched
        "/media/TV/Show/": (1710000000, 1400000000),  # the show's latest episode
    }
    assert asked == [("/library/sections/1/all", "1"), ("/library/sections/2/all", "2")]  # no music


@pytest.mark.asyncio
@pytest.mark.parametrize("server", ["jellyfin", "emby"])
async def test_jellyfin_and_emby_items(monkeypatch, server):
    import importlib
    mod = importlib.import_module(f"backend.{server}")

    async def settings():
        return {f"{server}_url": "http://srv", f"{server}_api_key": "k", f"{server}_user_id": "u1",
                f"{server}_path_mapping": "/media=/srv"}
    monkeypatch.setattr(mod, f"_get_{server}_settings", settings)

    def handler(request):
        assert request.url.path == "/Users/u1/Items"
        assert request.url.params["IncludeItemTypes"] == "Movie,Episode"
        return httpx.Response(200, json={"Items": [
            {"Path": "/srv/TV/Show/Season 1/e1.mkv", "DateCreated": "2023-01-01T00:00:00.0000000Z",
             "UserData": {"LastPlayedDate": "2024-05-01T20:15:00.1234567Z"}},
            {"Path": "/srv/TV/Show/Season 1/e2.mkv", "DateCreated": "2022-01-01T00:00:00Z",
             "UserData": {"LastPlayedDate": "2024-06-01T00:00:00Z"}},
            {"Path": "/srv/Movies/Dune/Dune.mkv", "DateCreated": "2021-03-04T05:06:07Z", "UserData": {}},
            {"Name": "no path"}]})
    _serve(monkeypatch, handler)
    epoch = watch_activity.iso_epoch
    assert await mod.get_watch_activity() == {
        # the season: its latest play, its earliest add
        "/media/TV/Show/Season 1/": (epoch("2024-06-01T00:00:00"), epoch("2022-01-01T00:00:00")),
        "/media/Movies/Dune/": (None, epoch("2021-03-04T05:06:07")),
    }
    assert epoch("2024-05-01T20:15:00.1234567Z") == 1714594500


@pytest.mark.asyncio
async def test_each_server_replaces_only_its_rows(db, monkeypatch):
    import backend.plex as plex
    await watch_activity.store("jellyfin", {"/m/A/": (1, 2)})
    await watch_activity.store("plex", {"/m/A/": (5, 1), "/m/B/": (None, 3)})
    await watch_activity.store("plex", {"/m/B/": (4, 3)})
    assert await _rows(db) == [("/m/A/", "jellyfin", 1, 2), ("/m/B/", "plex", 4, 3)]

    async def down():
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(plex, "get_watch_activity", down)
    await plex.sync_watch_activity()
    assert len(await _rows(db)) == 2  # a failed read keeps them

    async def not_set_up():
        return {}
    monkeypatch.setattr(plex, "get_watch_activity", not_set_up)
    await plex.sync_watch_activity()
    assert await _rows(db) == [("/m/A/", "jellyfin", 1, 2)]  # Plex removed: its rows go


@pytest.mark.asyncio
async def test_the_servers_combine(db):
    await watch_activity.store("plex", {"/m/Show/": (None, 100)})
    await watch_activity.store("jellyfin", {"/m/Show/": (500, 200)})
    async with aiosqlite.connect(db) as conn:
        assert await watch_activity.load(conn) == {"/m/Show/": (500, 100)}


def test_a_files_folders_combine():
    """A file's record is its folders' together — a Plex show's and a
    Jellyfin season's: the latest play; never played, when it was added."""
    from backend.scan_filters import row_last_watched
    ep = "/m/Show/S1/e1.mkv"
    assert row_last_watched({"file_path": ep}, {"watch_activity": {"/m/Show/": (100, 50), "/m/Show/S1/": (None, 80)}}) == 100
    assert row_last_watched({"file_path": ep}, {"watch_activity": {"/m/Show/": (None, 50), "/m/Show/S1/": (100, 80)}}) == 100
    assert row_last_watched({"file_path": ep}, {"watch_activity": {"/m/Show/": (None, 50)}}) == 50
    # Added again after its last play (a new file): counts from then.
    assert row_last_watched({"file_path": ep}, {"watch_activity": {"/m/Show/": (100, 300)}}) == 300
    assert row_last_watched({"file_path": "/m/Other/x.mkv"}, {"watch_activity": {"/m/Show/": (None, 50)}}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("server", ["plex", "jellyfin", "emby"])
async def test_the_metadata_syncs_read_it(test_db, monkeypatch, server):
    import importlib
    mod = importlib.import_module(f"backend.{server}")
    monkeypatch.setattr(mod, "DB_PATH", test_db)
    called = []

    async def sync():
        called.append(server)
    monkeypatch.setattr(mod, "sync_watch_activity", sync)
    await getattr(mod, f"sync_{server}_metadata_cache")()  # not set up: nothing else to do
    assert called == [server]
