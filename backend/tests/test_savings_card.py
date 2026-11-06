"""The shareable savings card (v0.10.0) counts the files made smaller and
their size before — not health checks or conversions that saved nothing."""
import aiosqlite
import pytest


@pytest.mark.asyncio
async def test_the_dashboard_counts_the_files_shrunk(test_db, monkeypatch):
    import backend.routes.stats as stats
    async with aiosqlite.connect(test_db) as db:
        for path, job_type, status, original, saved in (
                ("/m/a.mkv", "convert", "completed", 1000, 400), ("/m/b.mkv", "convert", "completed", 500, 100),
                ("/m/c.mkv", "health_check", "completed", 0, 0), ("/m/d.mkv", "convert", "completed", 800, 0),
                ("/m/e.mkv", "convert", "failed", 900, 0)):
            await db.execute("INSERT INTO jobs (file_path, job_type, status, created_at, original_size, space_saved) "
                             "VALUES (?, ?, ?, 'x', ?, ?)", (path, job_type, status, original, saved))
        await db.commit()
    dash = await stats.get_dashboard()
    assert dash["shrunk"] == {"files": 2, "before": 1500}
    assert dash["total_saved"] == 500 and dash["queue"]["completed"] == 4


@pytest.mark.asyncio
async def test_the_cumulative_chart_ends_at_the_total_saved(test_db):
    """The Dashboard's "Cumulative space saved" (90 days) started at 0: it ended
    at the 90 days' savings, 60 TB, under a card saying 147 TB. It starts at
    what was saved before them, so it ends at the card's total (v0.10.0)."""
    from datetime import datetime, timedelta, timezone
    import backend.routes.stats as stats
    from backend.database import backfill_daily_stats
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(test_db) as db:
        for path, days_ago, saved in (("/m/a.mkv", 200, 700), ("/m/b.mkv", 10, 100), ("/m/c.mkv", 5, 200)):
            await db.execute("INSERT INTO jobs (file_path, job_type, status, created_at, completed_at, original_size, "
                             "space_saved) VALUES (?, 'convert', 'completed', 'x', ?, 1000, ?)",
                             (path, (now - timedelta(days=days_ago)).isoformat(), saved))
        await db.commit()
    await backfill_daily_stats()
    days = (await stats.get_stats_timeline(days=90))["days"]
    assert [d["cumulative_saved"] for d in days] == [800, 1000]
    assert days[-1]["cumulative_saved"] == (await stats.get_dashboard())["total_saved"]
