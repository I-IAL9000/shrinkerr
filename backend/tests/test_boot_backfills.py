"""F15 (v0.10.0): init_db's converted-flag backfill reset and re-set every
flag on every boot. It now writes only wrong flags, and only after a
completed job changed."""
import aiosqlite
import pytest


async def _flags(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path, converted FROM scan_results ORDER BY file_path") as cur:
            return dict(await cur.fetchall())


async def _exec(db_path, sql, params=()):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(sql, params)
        await db.commit()


async def _changes_during_init(db_path):
    """Rows the boot backfills wrote to scan_results."""
    import backend.database as database
    await _exec(db_path, "CREATE TABLE IF NOT EXISTS _writes (n INTEGER)")
    await _exec(db_path, "DELETE FROM _writes")
    await _exec(db_path, "CREATE TRIGGER IF NOT EXISTS _count_writes AFTER UPDATE OF converted ON scan_results "
                         "BEGIN INSERT INTO _writes VALUES (1); END")
    await database.init_db()
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM _writes") as cur:
            return (await cur.fetchone())[0]


@pytest.mark.asyncio
async def test_converted_flags_follow_the_jobs_and_are_written_only_when_wrong(test_db):
    async with aiosqlite.connect(test_db) as db:
        await db.executemany(
            "INSERT INTO scan_results (file_path, file_size, converted, scan_timestamp) VALUES (?, 1, ?, '2026-01-01')",
            [("/m/a.mkv", 0), ("/m/b.mkv", 1), ("/m/c.mkv", 1), ("/m/orig.mkv", 0)])
        await db.executemany(
            "INSERT INTO jobs (file_path, original_file_path, job_type, status, space_saved, created_at) "
            "VALUES (?, ?, 'convert', 'completed', 10, '2026-10-09')",
            [("/m/a.mkv", None), ("/m/b.mkv", None), ("/m/out.mkv", "/m/orig.mkv")])
        await db.commit()

    # Stale flags (a should be 1, c should be 0, orig 1 via original_file_path)
    # are healed, and b — already right — isn't rewritten.
    assert await _changes_during_init(test_db) == 3
    assert await _flags(test_db) == {"/m/a.mkv": 1, "/m/b.mkv": 1, "/m/c.mkv": 0, "/m/orig.mkv": 1}

    # Nothing changed: no work at all, even with a flag gone wrong meanwhile.
    await _exec(test_db, "UPDATE scan_results SET converted = 0 WHERE file_path = '/m/b.mkv'")
    assert await _changes_during_init(test_db) == 0

    # A completed job changes (history cleared for a): healed on the next boot.
    await _exec(test_db, "DELETE FROM jobs WHERE file_path = '/m/a.mkv'")
    assert await _changes_during_init(test_db) == 2
    assert await _flags(test_db) == {"/m/a.mkv": 0, "/m/b.mkv": 1, "/m/c.mkv": 0, "/m/orig.mkv": 1}
