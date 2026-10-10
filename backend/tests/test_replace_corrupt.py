"""Replacing corrupt files through Sonarr / Radarr (v0.10.0): a dry run of
the files the health check found corrupt — what each would blocklist,
delete and search — then the approved ones are replaced."""
import json

import aiosqlite
import httpx
import pytest
import pytest_asyncio

import backend.arr as arr

GRAB = "Show.S01E02.1080p.WEB-GRP"


@pytest_asyncio.fixture
async def library(test_db, tmp_path, monkeypatch):
    """Files on disk, their scan rows, and Sonarr / Radarr faked: returns
    (paths, the requests each received)."""
    monkeypatch.setattr(arr, "_sonarr_cache", {})
    monkeypatch.setattr(arr, "_radarr_cache", {})
    paths = {
        "episode": tmp_path / "TV" / "Show" / "Season 01" / "Show - S01E02.mkv",
        "manual": tmp_path / "TV" / "Show" / "Season 01" / "Show - S01E03.mkv",  # imported by hand
        "unknown": tmp_path / "TV" / "Lost Show" / "Season 01" / "Lost Show - S01E01.mkv",
        "film": tmp_path / "Movies" / "Film (2020)" / "Film (2020).mkv",
        "stray": tmp_path / "Movies" / "Other (2019)" / "Other (2019) old.mkv",  # not Radarr's file
        "healthy": tmp_path / "Movies" / "Film (2020)" / "Film (2020) sample.mkv",
    }
    for p in paths.values():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    gone = tmp_path / "TV" / "Show" / "Season 01" / "Show - S01E04.mkv"  # already deleted
    async with aiosqlite.connect(test_db) as db:
        for name, p in [*paths.items(), ("gone", gone)]:
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, audio_tracks_json, "
                "scan_timestamp, health_status) VALUES (?, 1, 'h264', 1, '[]', 'x', ?)",
                (str(p), "healthy" if name == "healthy" else "corrupt"))
        for k, v in (("sonarr_url", "http://sonarr"), ("sonarr_api_key", "k"), ("sonarr_path_mapping", f"{tmp_path}/TV=/tv"),
                     ("radarr_url", "http://radarr"), ("radarr_api_key", "k"), ("radarr_path_mapping", f"{tmp_path}/Movies=/movies")):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()

    routes = {
        "GET /api/v3/series": [{"id": 1, "title": "Show", "path": "/tv/Show"}],
        "GET /api/v3/episodefile?seriesId=1": [{"id": 10, "path": "/tv/Show/Season 01/Show - S01E02.mkv"},
                                               {"id": 11, "path": "/tv/Show/Season 01/Show - S01E03.mkv"}],
        "GET /api/v3/episode?seriesId=1": [{"id": 100, "episodeFileId": 10, "seasonNumber": 1, "episodeNumber": 2},
                                           {"id": 101, "episodeFileId": 11, "seasonNumber": 1, "episodeNumber": 3}],
        "GET /api/v3/history?episodeId=100&pageSize=20&sortKey=date&sortDirection=descending":
            {"records": [{"id": 554, "eventType": "downloadFolderImported"},
                         {"id": 555, "eventType": "grabbed", "sourceTitle": GRAB}]},
        "GET /api/v3/history?episodeId=101&pageSize=20&sortKey=date&sortDirection=descending":
            {"records": [{"id": 556, "eventType": "downloadFolderImported"}]},
        "GET /api/v3/movie": [{"id": 5, "title": "Film", "path": "/movies/Film (2020)"},
                              {"id": 6, "title": "Other", "path": "/movies/Other (2019)"}],
        "GET /api/v3/movie/5": {"id": 5, "title": "Film", "year": 2020,
                                "movieFile": {"id": 50, "path": "/movies/Film (2020)/Film (2020).mkv"}},
        "GET /api/v3/movie/6": {"id": 6, "title": "Other", "year": 2019,
                                "movieFile": {"id": 60, "path": "/movies/Other (2019)/Other (2019) Remux.mkv"}},
        "GET /api/v3/history/movie?movieId=5&eventType=1": [
            {"id": 76, "date": "2025-01-01", "sourceTitle": "Film.2020.720p-OLD"},
            {"id": 77, "date": "2026-01-01", "sourceTitle": "Film.2020.1080p.BluRay-GRP"}],
        "GET /api/v3/history/movie?movieId=6&eventType=1": [],
    }
    sent = {"sonarr": [], "radarr": []}

    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}" + ("?" + request.url.query.decode() if request.url.query else "")
        sent[request.url.host].append((key, json.loads(request.content) if request.content else None))
        if request.method != "GET":
            return httpx.Response(200 if request.method == "DELETE" else 201, json={})
        return httpx.Response(200, json=routes[key]) if key in routes else httpx.Response(404)
    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    return {k: str(v) for k, v in paths.items()}, sent


def _changes(sent):
    return [(key, body) for service in sent.values() for key, body in service if not key.startswith("GET ")]


@pytest.mark.asyncio
async def test_the_dry_run_changes_nothing(library):
    from backend.routes.arr import replace_plan
    paths, sent = library
    plan = await replace_plan()
    by_path = {f["file_path"]: f for f in plan["files"]}
    assert sorted(by_path) == sorted(p for name, p in paths.items() if name != "healthy")  # gone: left out
    episode = by_path[paths["episode"]]
    assert (episode["service"], episode["series"], episode["episodes"], episode["release"], episode["deletes"]) == (
        "sonarr", "Show", ["S01E02"], GRAB, True)
    assert by_path[paths["manual"]]["release"] is None  # no download record: nothing to blocklist
    film = by_path[paths["film"]]
    assert (film["service"], film["movie"], film["year"], film["release"], film["deletes"]) == (
        "radarr", "Film", 2020, "Film.2020.1080p.BluRay-GRP", True)
    assert not by_path[paths["unknown"]]["success"]
    stray = by_path[paths["stray"]]
    assert not stray["success"] and "Other (2019) Remux.mkv" in stray["error"]
    assert _changes(sent) == [] and plan["truncated"] is False


@pytest.mark.asyncio
async def test_the_approved_ones_are_replaced(library, test_db):
    from backend.routes.arr import BulkActionRequest, action_bulk, replace_plan
    paths, sent = library
    res = await action_bulk(BulkActionRequest(file_paths=[paths["episode"], paths["film"]], action="replace"))
    assert (res["succeeded"], res["failed"]) == (2, 0)
    assert all(r["blocklisted"] and r["deleted"] and r["searched"] for r in res["results"])
    assert _changes(sent) == [
        ("POST /api/v3/history/failed/555", None), ("DELETE /api/v3/episodefile/10", None),
        ("POST /api/v3/command", {"name": "EpisodeSearch", "episodeIds": [100]}),
        ("POST /api/v3/history/failed/77", None), ("DELETE /api/v3/moviefile/50", None),
        ("POST /api/v3/command", {"name": "MoviesSearch", "movieIds": [5]})]
    async with aiosqlite.connect(test_db) as db:  # forgotten: a new download starts unchecked
        async with db.execute("SELECT file_path FROM scan_results WHERE file_path IN (?, ?)",
                              (paths["episode"], paths["film"])) as cur:
            assert await cur.fetchall() == []
    assert paths["episode"] not in [f["file_path"] for f in (await replace_plan())["files"]]


@pytest.mark.asyncio
async def test_radarr_only_deletes_its_own_file(library, test_db):
    """The folder's other file is Radarr's: deleting "the movie file" would
    delete that one instead."""
    paths, sent = library
    result = await arr.research_file(paths["stray"])
    assert not result["success"] and "different one" in result["error"]
    assert _changes(sent) == []
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT 1 FROM scan_results WHERE file_path = ?", (paths["stray"],)) as cur:
            assert await cur.fetchone()
