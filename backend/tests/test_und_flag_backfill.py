"""Tests for the v0.9.3 has_und_tracks_flag backfill.

The flag column (v0.8.0) is only set at scan time, so rows scanned on an
earlier version stayed 0 even with und tracks — the Unknown-language filter
showed almost nothing. backfill_und_tracks_flag recomputes it from the
stored track JSON, once, guarded by a settings sentinel.
"""
import json
import aiosqlite
import pytest

import backend.database as database


async def _make_db(path):
    db = await aiosqlite.connect(path)
    await db.execute(
        "CREATE TABLE scan_results (id INTEGER PRIMARY KEY, "
        "audio_tracks_json TEXT, subtitle_tracks_json TEXT, "
        "has_und_tracks_flag INTEGER DEFAULT 0)"
    )
    await db.execute(
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    und_audio = json.dumps([{"language": "eng"}, {"language": "und"}])
    eng_only = json.dumps([{"language": "eng"}])
    und_sub = json.dumps([{"language": "und"}])
    rows = [
        (1, und_audio, None, 0),        # und audio -> flag
        (2, eng_only, None, 0),         # nothing und -> stays 0
        (3, eng_only, und_sub, 0),      # und subtitle -> flag
        (4, und_audio, None, 1),        # already flagged -> untouched, not recounted
        (5, "{bad json", None, 0),      # malformed -> no crash, stays 0
        (6, None, None, 0),             # no tracks at all -> stays 0
    ]
    await db.executemany(
        "INSERT INTO scan_results (id, audio_tracks_json, subtitle_tracks_json, has_und_tracks_flag) "
        "VALUES (?, ?, ?, ?)", rows,
    )
    await db.commit()
    await db.close()


@pytest.mark.asyncio
async def test_backfill_flags_und_rows_and_is_idempotent(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    await _make_db(db_path)
    monkeypatch.setattr(database, "DB_PATH", db_path)

    flagged = await database.backfill_und_tracks_flag()
    assert flagged == 2  # rows 1 and 3 (row 4 already flagged, not recounted)

    db = await aiosqlite.connect(db_path)
    async with db.execute("SELECT id FROM scan_results WHERE has_und_tracks_flag = 1") as cur:
        ids = sorted(r[0] for r in await cur.fetchall())
    assert ids == [1, 3, 4]  # 4 was already flagged and left alone

    # Sentinel recorded.
    async with db.execute("SELECT value FROM settings WHERE key = 'und_flag_backfilled'") as cur:
        assert (await cur.fetchone())[0] == "1"
    await db.close()

    # Second run is a no-op (sentinel guard) — returns 0, changes nothing.
    assert await database.backfill_und_tracks_flag() == 0


@pytest.mark.asyncio
async def test_resync_und_flag_both_directions(tmp_path, monkeypatch):
    """v0.9.25: resync corrects has_und_tracks_flag to match the track JSON
    in both directions (the post-conversion staleness bug)."""
    import json
    db_path = str(tmp_path / "resync.db")
    db = await aiosqlite.connect(db_path)
    await db.execute(
        "CREATE TABLE scan_results (id INTEGER PRIMARY KEY, "
        "audio_tracks_json TEXT, subtitle_tracks_json TEXT, has_und_tracks_flag INTEGER DEFAULT 0)"
    )
    await db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    und = json.dumps([{"language": "und"}])
    eng = json.dumps([{"language": "eng"}])
    await db.executemany(
        "INSERT INTO scan_results (id, audio_tracks_json, subtitle_tracks_json, has_und_tracks_flag) VALUES (?,?,?,?)",
        [
            (1, eng, und, 0),   # has und but flag=0 (the bug) -> should become 1
            (2, eng, eng, 1),   # no und but flag=1 (stale) -> should become 0
            (3, eng, und, 1),   # already correct
            (4, eng, eng, 0),   # already correct
        ],
    )
    await db.commit()
    await db.close()
    monkeypatch.setattr(database, "DB_PATH", db_path)

    n = await database.resync_und_tracks_flag()
    assert n == 2  # rows 1 and 2 corrected

    db = await aiosqlite.connect(db_path)
    async with db.execute("SELECT id, has_und_tracks_flag FROM scan_results ORDER BY id") as cur:
        flags = {r[0]: r[1] for r in await cur.fetchall()}
    await db.close()
    assert flags == {1: 1, 2: 0, 3: 1, 4: 0}

    # Idempotent (sentinel).
    assert await database.resync_und_tracks_flag() == 0
