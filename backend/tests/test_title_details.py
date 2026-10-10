"""TV network and show status from TMDB (v0.10.0), for Advanced Search.

TMDB's search results carry the rating, genres and country the poster cache
already stored, but not a show's network or status: those need the show's
own record (/tv/{id}). The poster prefetch asks for them, and once for the
shows it cached before."""
import aiosqlite
import pytest


@pytest.fixture
def tmdb(test_db, monkeypatch):
    """TMDB faked: [tvdb-N] folders are TV show N; details per show id."""
    import backend.routes.posters as posters
    monkeypatch.setattr(posters, "DB_PATH", test_db)
    details = {1: {"network": "HBO", "status": "Ended"}, 2: {}}
    calls, finds = [], []

    async def find_tvdb(tvdb_id, key):
        finds.append(tvdb_id)
        return f"https://img/{tvdb_id}.jpg", "tmdb", {"media_type": "tv", "tmdb_id": int(tvdb_id), "rating": 7.5}

    async def tv_details(tmdb_id, key):
        calls.append(tmdb_id)
        return {k: v for k, v in details.get(tmdb_id, {}).items()}

    async def no_image(*a, **kw):
        return None
    monkeypatch.setattr(posters, "_resolve_tmdb_tvdb", find_tvdb)
    monkeypatch.setattr(posters, "_tmdb_tv_details", tv_details)
    monkeypatch.setattr(posters, "_download_image", no_image)
    monkeypatch.setenv("SHRINKERR_TMDB_API_KEY", "test-key")
    return posters, calls, finds


async def _cache(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT folder_path, network, status FROM poster_cache ORDER BY folder_path") as cur:
            return [tuple(r) for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_the_prefetch_stores_network_and_status(test_db, tmdb):
    posters, _, finds = tmdb
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES "
                         "('/media/TV/Show [tvdb-1]/Season 1/S01E01.mkv', 1, '2026-10-10')")
        await db.commit()
    await posters._run_prefetch()
    assert await _cache(test_db) == [("/media/TV/Show [tvdb-1]", "HBO", "Ended")]
    assert finds == ["1"]  # with the poster, not found a second time


@pytest.mark.asyncio
async def test_shows_cached_before_get_them_once(test_db, tmdb):
    posters, calls, _ = tmdb
    async with aiosqlite.connect(test_db) as db:
        await db.executemany(
            "INSERT INTO poster_cache (folder_path, title, source, media_type, image_data) VALUES (?, ?, 'tmdb', ?, 'x')",
            [("/media/TV/Old Show [tvdb-1]", "Old Show", "tv"), ("/media/TV/Gone [tvdb-2]", "Gone", "tv"),
             ("/media/Movies/Film [tt0000001]", "Film", "movie")])
        await db.commit()
    assert await posters._backfill_tv_details("test-key", lambda p: None) == 2
    # TMDB had nothing for show 2: stored as "", not asked again. Movies: no.
    assert await _cache(test_db) == [("/media/Movies/Film [tt0000001]", None, None),
                                     ("/media/TV/Gone [tvdb-2]", "", ""),
                                     ("/media/TV/Old Show [tvdb-1]", "HBO", "Ended")]
    assert await posters._backfill_tv_details("test-key", lambda p: None) == 0
    assert sorted(calls) == [1, 2]


@pytest.mark.asyncio
async def test_movies_need_no_details(tmdb):
    posters, calls, _ = tmdb
    meta = {"media_type": "movie", "tmdb_id": 9}
    assert await posters._with_tv_details(meta, "test-key") == meta and calls == []
