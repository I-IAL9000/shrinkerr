"""v0.9.151: Plex posters were cached at full original size.

Plex `thumb` URLs return the original artwork (avg 774 KB base64, up to
10 MB), so 8k Plex posters made poster_cache 7.7 GB of a 9 GB database and
the weekly backup a 5.9 GB zip. They're now fetched through Plex's photo
transcoder at poster size; existing oversized rows are shrunk in place and
the freed space is reclaimed with a startup VACUUM.
"""
import base64
import os
import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest

import backend.routes.posters as posters

THUMB = "/library/metadata/123/thumb/1699999999"
PROXY_URL = "/api/posters/image?path=%2Flibrary%2Fmetadata%2F123%2Fthumb%2F1699999999"
BIG = b"\xff\xd8" + b"B" * 600_000
SMALL = b"\xff\xd8" + b"s" * 20_000


class _Resp:
    def __init__(self, status, content=b""):
        self.status_code = status
        self.content = content


def _fake_client(handler, calls):
    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            calls.append(url)
            return handler(url)

    return _Client


@pytest.mark.asyncio
async def test_plex_poster_fetched_through_transcoder(monkeypatch):
    import httpx
    calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(
        lambda url: _Resp(200, SMALL if "/photo/:/transcode" in url else BIG), calls))

    data = await posters._download_image(PROXY_URL, "http://plex:32400", "tok")

    assert base64.b64decode(data) == SMALL
    u = urlparse(calls[0])
    q = parse_qs(u.query)
    assert u.path == "/photo/:/transcode"
    assert q["url"] == [THUMB] and q["width"] == ["300"] and q["X-Plex-Token"] == ["tok"]


@pytest.mark.asyncio
async def test_plex_poster_falls_back_to_original_when_transcoder_fails(monkeypatch):
    import httpx
    calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _fake_client(
        lambda url: _Resp(404) if "/photo/:/transcode" in url else _Resp(200, BIG), calls))

    data = await posters._download_image(PROXY_URL, "http://plex:32400", "tok")

    assert base64.b64decode(data) == BIG
    assert calls[1].startswith(f"http://plex:32400{THUMB}?X-Plex-Token=tok")


async def _seed(db_path, rows):
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        for folder, source, url, img in rows:
            await db.execute(
                "INSERT INTO poster_cache (folder_path, poster_url, source, image_data) VALUES (?, ?, ?, ?)",
                (folder, url, source, base64.b64encode(img).decode() if img else None))
        await db.commit()


def _images(db_path):
    db = sqlite3.connect(db_path)
    try:
        return {f: (base64.b64decode(i) if i else None)
                for f, i in db.execute("SELECT folder_path, image_data FROM poster_cache")}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_shrink_replaces_only_oversized_plex_posters(test_db, monkeypatch):
    monkeypatch.setattr(posters, "DB_PATH", test_db)
    await _seed(test_db, [
        ("/m/Big Plex/", "plex", PROXY_URL, BIG),
        ("/m/Small Plex/", "plex", PROXY_URL, SMALL),
        ("/m/Big Tmdb/", "tmdb", "https://image.tmdb.org/t/p/w300/x.jpg", BIG),
        ("/m/Unshrinkable/", "plex", PROXY_URL.replace("123", "777"), BIG),
    ])

    async def fake_download(url, plex_url, plex_token):
        if "777" in url:  # transcoder failed → fell back to the full original
            return base64.b64encode(BIG).decode()
        return base64.b64encode(SMALL).decode()

    async def fake_settings():
        return "http://plex:32400", "tok", ""

    monkeypatch.setattr(posters, "_download_image", fake_download)
    import backend.plex
    monkeypatch.setattr(backend.plex, "_get_plex_settings", fake_settings)

    shrunk = await posters.shrink_plex_posters()

    imgs = _images(test_db)
    assert shrunk == 1
    assert imgs["/m/Big Plex/"] == SMALL
    assert imgs["/m/Small Plex/"] == SMALL
    assert imgs["/m/Big Tmdb/"] == BIG
    assert imgs["/m/Unshrinkable/"] == BIG


@pytest.mark.asyncio
async def test_compact_vacuums_only_when_much_space_is_free(test_db, monkeypatch):
    import aiosqlite
    import backend.database as database

    async with aiosqlite.connect(test_db) as db:
        await db.executemany(
            "INSERT INTO poster_cache (folder_path, image_data) VALUES (?, ?)",
            [(f"/m/{i}/", "x" * 100_000) for i in range(100)])
        await db.commit()

    assert await database.compact_if_bloated(min_free_bytes=1) is False  # nothing free yet

    async with aiosqlite.connect(test_db) as db:
        await db.execute("DELETE FROM poster_cache")
        await db.commit()
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = os.path.getsize(test_db)

    assert await database.compact_if_bloated(min_free_bytes=1) is True
    assert os.path.getsize(test_db) < before / 2


@pytest.mark.asyncio
async def test_shrink_runs_once_not_on_every_startup(test_db, monkeypatch):
    monkeypatch.setattr(posters, "DB_PATH", test_db)
    await _seed(test_db, [("/m/Gone From Plex/", "plex", PROXY_URL, BIG)])
    calls = []

    async def plex_has_no_smaller(url, plex_url, plex_token):
        calls.append(url)
        return None  # item gone from Plex: transcoder and original both fail

    async def fake_settings():
        return "http://plex:32400", "tok", ""

    monkeypatch.setattr(posters, "_download_image", plex_has_no_smaller)
    import backend.plex
    monkeypatch.setattr(backend.plex, "_get_plex_settings", fake_settings)

    assert await posters.shrink_plex_posters() == 0
    assert await posters.shrink_plex_posters() == 0
    assert len(calls) == 1  # second startup doesn't re-fetch
