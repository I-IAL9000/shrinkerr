"""The filter index's change signals: triggers log every changed scan_results
row id (filter_index_changes) and count changes to the other tables filters
read (change_counters "fi:<input>")."""
import re

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


# Per table: a minimal row to insert, the columns an UPDATE of which must
# count (each tried on its own), and the ones that must not. Spelled out here,
# not read from TABLES, so narrowing a list there by mistake fails.
ROWS = {
    "jobs": ({"file_path": "/m/a.mkv", "job_type": "convert", "created_at": "x"},
             {"status": "completed", "job_type": "remux", "file_path": "/m/b.mkv", "original_file_path": "/m/o.mkv",
              "space_saved": 5, "error_key": "k", "backup_path": "/m/b", "completed_at": "y"},
             {"progress": 50, "fps": 30.0, "eta_seconds": 60}),
    "ignored_files": ({"file_path": "/m/a.mkv", "reason": "manual", "ignored_at": "x"},
                      {"file_path": "/m/b.mkv", "reason": "other", "ignored_at": "y"}, {}),
    "encoding_rules": ({"name": "r", "action": "skip", "created_at": "x"},
                       {"name": "s", "match_type": "path", "match_value": "v", "priority": 3, "action": "encode",
                        "enabled": 0, "encoder": "libx265", "created_at": "y"}, {}),
    "plex_metadata_cache": ({"folder_path": "/m/", "metadata_type": "watch_status", "metadata_value": "watched",
                             "synced_at": "x"},
                            {"folder_path": "/n/", "metadata_type": "label", "metadata_value": "unwatched",
                             "synced_at": "y"}, {}),
    "media_dirs": ({"path": "/m", "label": "Movies"},
                   {"path": "/n", "label": "Shows", "enabled": 0}, {"auto_scan": 0}),
    "watch_activity": ({"folder_path": "/m/", "server": "plex", "last_viewed": 1, "added_at": 1},
                       {"folder_path": "/n/", "server": "emby", "last_viewed": 2, "added_at": 2}, {}),
    "arr_file_status": ({"file_path": "/m/a.mkv", "service": "radarr", "monitored": 1, "cutoff_unmet": 0,
                         "synced_at": "x"},
                        {"file_path": "/m/b.mkv", "service": "sonarr", "monitored": 0, "cutoff_unmet": 1,
                         "synced_at": "y"}, {}),
    "maintainerr_media": ({"folder_path": "/m/", "collection": "c", "synced_at": "x"},
                          {"folder_path": "/n/", "collection": "d", "synced_at": "y"}, {}),
    "settings": ({"key": "always_keep_languages", "value": "[]"}, {"value": "[\"ice\"]"}, {}),
}
CASES = [(name, table) for name, entries in TABLES.items() for table, _, _ in entries]


async def _counted(db, name, expected, sql, params=()):
    """Run one statement: the "fi:<name>" counter moves by `expected` and no other does."""
    async def counters():
        async with db.execute("SELECT name, n FROM change_counters WHERE name LIKE 'fi:%'") as cur:
            return dict(await cur.fetchall())
    before = await counters()
    cur = await db.execute(sql, params)
    await db.commit()
    after = await counters()
    moved = {k: after[k] - before[k] for k in after}
    assert moved == {f"fi:{n}": (expected if n == name else 0) for n in TABLES}, sql
    return cur.lastrowid


@pytest.mark.asyncio
@pytest.mark.parametrize("name,table", CASES, ids=[f"{name}-{table}" for name, table in CASES])
async def test_every_event_and_column_of_a_table_counts(test_db, name, table):
    insert, counted, ignored = ROWS[table]
    async with aiosqlite.connect(test_db) as db:
        marks = ", ".join("?" * len(insert))
        row = await _counted(db, name, 1, f"INSERT INTO {table} ({', '.join(insert)}) VALUES ({marks})",
                             tuple(insert.values()))
        for column, value in counted.items():
            await _counted(db, name, 1, f"UPDATE {table} SET {column} = ? WHERE rowid = ?", (value, row))
        for column, value in ignored.items():
            await _counted(db, name, 0, f"UPDATE {table} SET {column} = ? WHERE rowid = ?", (value, row))
        await _counted(db, name, 1, f"DELETE FROM {table} WHERE rowid = ?", (row,))


WATCHED = ("always_keep_languages", "sub_keep_languages", "backup_original_days")


def test_the_watched_settings_are_the_ones_these_tests_name():
    (_, _, when), = TABLES["settings"]
    assert set(re.findall(r"'([^']+)'", when)) == set(WATCHED)


@pytest.mark.asyncio
@pytest.mark.parametrize("key,expected", [(key, 1) for key in WATCHED] + [("queue_run_state", 0)])
async def test_only_the_settings_filters_read_count(test_db, key, expected):
    async with aiosqlite.connect(test_db) as db:
        row = await _counted(db, "settings", expected, "INSERT INTO settings (key, value) VALUES (?, 'a')", (key,))
        await _counted(db, "settings", expected, "UPDATE settings SET value = 'b' WHERE rowid = ?", (row,))
        await _counted(db, "settings", expected, "DELETE FROM settings WHERE rowid = ?", (row,))


@pytest.mark.asyncio
async def test_renaming_a_setting_into_or_out_of_the_watched_keys_counts(test_db):
    async with aiosqlite.connect(test_db) as db:
        row = await _counted(db, "settings", 0, "INSERT INTO settings (key, value) VALUES ('queue_run_state', 'a')")
        await _counted(db, "settings", 1, "UPDATE settings SET key = ? WHERE rowid = ?", (WATCHED[0], row))
        await _counted(db, "settings", 1, "UPDATE settings SET key = 'queue_run_state' WHERE rowid = ?", (row,))
        await _counted(db, "settings", 0, "UPDATE settings SET key = 'other' WHERE rowid = ?", (row,))


@pytest.mark.asyncio
async def test_start_recreates_the_triggers(test_db):
    from backend.database import init_db
    async with aiosqlite.connect(test_db) as db:  # a database from an older version
        await db.execute("DROP TRIGGER trg_fi_jobs_insert")
        await db.execute("CREATE TRIGGER trg_fi_jobs_insert AFTER INSERT ON jobs BEGIN SELECT 1; END")
        await db.commit()
    await init_db()
    async with aiosqlite.connect(test_db) as db:
        await _counted(db, "jobs", 1,
                       "INSERT INTO jobs (file_path, job_type, created_at) VALUES ('/m/a.mkv', 'convert', 'x')")
