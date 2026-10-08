"""v0.10.0 (F22): % and _ in an Activity search matched any characters."""
import aiosqlite
import pytest


@pytest.mark.asyncio
async def test_activity_search_is_literal(test_db):
    from backend.routes.activity import activity_feed
    async with aiosqlite.connect(test_db) as db:
        for path in ("/media/100% Wolf (2020).mkv", "/media/1000 Wolves (2020).mkv", "/media/Show_S01.mkv",
                     "/media/ShowXS01.mkv"):
            await db.execute("INSERT INTO file_events (file_path, event_type, occurred_at, summary) "
                             "VALUES (?, 'completed', '2026-10-08T00:00:00', 'x')", (path,))
        await db.commit()

    async def paths(search):
        res = await activity_feed(event_type=None, search=search, since=None, until=None, limit=100, offset=0)
        return sorted(e["file_path"] for e in res["events"])

    assert await paths("100%") == ["/media/100% Wolf (2020).mkv"]
    assert await paths("Show_S") == ["/media/Show_S01.mkv"]
