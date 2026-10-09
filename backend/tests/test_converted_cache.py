"""F7 (v0.10.0): the Scanner's "converted" sets — built from every completed
conversion — are cached until a completed job changes, counted by triggers
on `jobs` so every writer invalidates them."""
import aiosqlite
import pytest


async def _sets(db_path):
    from backend.routes.scan import _converted_sets
    async with aiosqlite.connect(db_path) as db:
        return await _converted_sets(db)


async def _sql(db_path, sql, params=()):
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.lastrowid


async def _add_job(db_path, path, status="completed", job_type="convert", saved=100, original=None):
    return await _sql(
        db_path,
        "INSERT INTO jobs (file_path, original_file_path, job_type, status, space_saved, created_at) "
        "VALUES (?, ?, ?, ?, ?, '2026-10-09')",
        (path, original, job_type, status, saved),
    )


@pytest.mark.asyncio
async def test_cached_until_a_completed_job_changes(test_db):
    a = await _add_job(test_db, "/m/A/a.x265.mkv", original="/m/A/a.mkv")
    paths, folders = await _sets(test_db)
    assert paths == {"/m/A/a.x265.mkv", "/m/A/a.mkv"} and folders == {"/m/A/"}

    # Writes that don't touch the completed set keep the cache.
    await _sql(test_db, "UPDATE jobs SET progress = 50, fps = 30 WHERE id = ?", (a,))
    pending = await _add_job(test_db, "/m/B/b.mkv", status="pending")
    await _sql(test_db, "UPDATE jobs SET status = 'running' WHERE id = ?", (pending,))
    assert (await _sets(test_db))[0] is paths

    # A job that completes.
    await _sql(test_db, "UPDATE jobs SET status = 'completed', space_saved = 5 WHERE id = ?", (pending,))
    assert (await _sets(test_db))[0] == {"/m/A/a.x265.mkv", "/m/A/a.mkv", "/m/B/b.mkv"}
    # A completed job renamed, undone / retried, and deleted.
    await _sql(test_db, "UPDATE jobs SET file_path = '/m/B/b.x265.mkv' WHERE id = ?", (pending,))
    assert "/m/B/b.x265.mkv" in (await _sets(test_db))[0]
    await _sql(test_db, "UPDATE jobs SET status = 'pending' WHERE id = ?", (a,))
    assert (await _sets(test_db))[0] == {"/m/B/b.x265.mkv"}
    await _sql(test_db, "DELETE FROM jobs WHERE id = ?", (pending,))
    assert (await _sets(test_db)) == (set(), set())
    # A job recorded as completed straight away.
    await _add_job(test_db, "/m/D/d.mkv")
    assert (await _sets(test_db))[0] == {"/m/D/d.mkv"}


@pytest.mark.asyncio
async def test_only_conversions_that_saved_space_count(test_db):
    await _add_job(test_db, "/m/A/a.mkv", saved=0)
    await _add_job(test_db, "/m/B/b.mkv", job_type="audio")
    await _add_job(test_db, "/m/C/c.mkv", job_type="combined")
    assert (await _sets(test_db))[0] == {"/m/C/c.mkv"}


@pytest.mark.asyncio
async def test_another_database_is_not_served_from_the_cache(test_db, tmp_path):
    import backend.database as database
    await _add_job(test_db, "/m/A/a.mkv")
    assert (await _sets(test_db))[0] == {"/m/A/a.mkv"}
    other = str(tmp_path / "other.db")
    database.DB_PATH = other
    await database.init_db()
    await _add_job(other, "/m/Z/z.mkv")  # same counter value as test_db
    assert (await _sets(other))[0] == {"/m/Z/z.mkv"}


@pytest.mark.asyncio
async def test_a_database_without_the_counter_is_read_every_time(test_db):
    # A backup restored from before the counter existed.
    for trigger in ("insert", "update", "delete"):
        await _sql(test_db, f"DROP TRIGGER trg_completed_jobs_{trigger}")
    await _sql(test_db, "DROP TABLE change_counters")
    await _add_job(test_db, "/m/A/a.mkv")
    assert (await _sets(test_db))[0] == {"/m/A/a.mkv"}
    await _add_job(test_db, "/m/B/b.mkv")
    assert (await _sets(test_db))[0] == {"/m/A/a.mkv", "/m/B/b.mkv"}
