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
