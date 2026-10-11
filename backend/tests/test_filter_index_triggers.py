"""The filter index's change signals: triggers log every changed scan_results
row id (filter_index_changes) and count changes to the other tables filters
read (change_counters "fi:<input>")."""
import aiosqlite
import pytest

from backend.filter_index.inputs import TABLES


async def _counters(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT name, n FROM change_counters WHERE name LIKE 'fi:%'") as cur:
            return dict(await cur.fetchall())


@pytest.mark.asyncio
async def test_scan_results_changes_are_logged(test_db):
    async with aiosqlite.connect(test_db) as db:
        for path in ("/m/a.mkv", "/m/b.mkv"):
            await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) VALUES (?, 1, 'x')",
                             (path,))
        await db.execute("UPDATE scan_results SET file_size = 2 WHERE file_path = '/m/a.mkv'")
        await db.execute("DELETE FROM scan_results WHERE file_path = '/m/b.mkv'")
        await db.commit()
        async with db.execute("SELECT row_id FROM filter_index_changes ORDER BY seq") as cur:
            logged = [r[0] for r in await cur.fetchall()]
        async with db.execute("SELECT id FROM scan_results WHERE file_path = '/m/a.mkv'") as cur:
            a = (await cur.fetchone())[0]
    assert logged == [a, a + 1, a, a + 1]  # inserted, inserted, updated, deleted


@pytest.mark.asyncio
async def test_the_tables_filters_read_count_their_changes(test_db):
    before = await _counters(test_db)
    assert set(before) == {f"fi:{name}" for name in TABLES}
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO jobs (file_path, job_type, status, created_at, audio_tracks_to_remove) "
                         "VALUES ('/m/a.mkv', 'convert', 'pending', 'x', '[]')")
        # A running encode's progress, every second: not a change filters see.
        await db.execute("UPDATE jobs SET progress = 50, fps = 30, eta_seconds = 60 WHERE file_path = '/m/a.mkv'")
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('queue_run_state', 'paused')")  # no
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('always_keep_languages', '[\"ice\"]')")
        await db.execute("INSERT INTO ignored_files (file_path, reason, ignored_at) VALUES ('/m/a.mkv', 'manual', 'x')")
        await db.execute("INSERT INTO plex_metadata_cache (folder_path, metadata_type, metadata_value, synced_at) "
                         "VALUES ('/m/', 'watch_status', 'watched', 'x')")
        await db.execute("INSERT INTO watch_activity (folder_path, server, last_viewed, added_at) "
                         "VALUES ('/m/', 'plex', 1, 1)")
        await db.execute("INSERT INTO arr_file_status (file_path, service, monitored, cutoff_unmet, synced_at) "
                         "VALUES ('/m/a.mkv', 'radarr', 1, 0, 'x')")
        await db.execute("INSERT INTO maintainerr_media (folder_path, collection, synced_at) VALUES ('/m/', 'c', 'x')")
        await db.execute("INSERT INTO media_dirs (path, label) VALUES ('/m', 'Movies')")
        await db.execute("INSERT INTO encoding_rules (name, action, created_at) VALUES ('r', 'skip', 'x')")
        await db.commit()
    after = await _counters(test_db)
    assert {k for k in after if after[k] != before[k]} == {f"fi:{name}" for name in TABLES}
    assert after["fi:jobs"] == before["fi:jobs"] + 1          # the insert, not the progress
    assert after["fi:settings"] == before["fi:settings"] + 1  # the keep languages, not the queue state
