"""Tests for the v0.9.4 stale-disc_type fixes.

A BDMV/DVD title converted to a single .mkv used to keep disc_type set on
its row (the post-conversion handler repointed the disc-marker row at the
.mkv without clearing it). That made converted titles show a disc badge and
render the category-dir name ("Movies2") instead of the title, because
_disc_aware_file_name fell into its folder-disc branch for any row with a
disc_type set.
"""
import aiosqlite
import pytest

import backend.database as database
from backend.routes.scan import _disc_aware_file_name

_MKV = ("/media/Misc/Movies2/44 Inch Chest (2009) [tt0914837]/"
        "44 Inch Chest (2009) 1080p Bluray EAC3 5.1 h265.mkv")
_BDMV = "/media/Misc/Movies2/Some Movie (2010)/BDMV/index.bdmv"
_DVD = "/media/Misc/Movies2/Some Movie (2010)/VIDEO_TS/VIDEO_TS.IFO"
_ISO = "/media/Misc/Movies2/Elephant (2003) [tt0363589]/rz0u.iso"


def test_stale_disc_type_on_mkv_uses_basename_not_category_dir():
    # Even with a stale disc_type, a real .mkv must show its own filename,
    # never the category dir two levels up.
    assert _disc_aware_file_name(_MKV, "bdmv") == \
        "44 Inch Chest (2009) 1080p Bluray EAC3 5.1 h265.mkv"


def test_real_bdmv_marker_uses_title_folder():
    assert _disc_aware_file_name(_BDMV, "bdmv") == "Some Movie (2010)"


def test_real_dvd_marker_uses_title_folder():
    assert _disc_aware_file_name(_DVD, "dvd") == "Some Movie (2010)"


def test_iso_uses_iso_basename():
    assert _disc_aware_file_name(_ISO, "bdmv") == "rz0u.iso"


def test_regular_file_no_disc_type_uses_basename():
    assert _disc_aware_file_name(_MKV, None) == \
        "44 Inch Chest (2009) 1080p Bluray EAC3 5.1 h265.mkv"


async def _make_db(path):
    db = await aiosqlite.connect(path)
    await db.execute(
        "CREATE TABLE scan_results (id INTEGER PRIMARY KEY, file_path TEXT, disc_type TEXT)"
    )
    await db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    rows = [
        (1, _MKV, "bdmv"),   # stale -> cleared
        (2, _BDMV, "bdmv"),  # real disc -> kept
        (3, _DVD, "dvd"),    # real disc -> kept
        (4, _ISO, "bdmv"),   # real iso disc -> kept
        (5, "/media/x/normal.mkv", None),  # already null -> untouched
    ]
    await db.executemany(
        "INSERT INTO scan_results (id, file_path, disc_type) VALUES (?, ?, ?)", rows,
    )
    await db.commit()
    await db.close()


@pytest.mark.asyncio
async def test_backfill_clears_only_stale_disc_type(tmp_path, monkeypatch):
    db_path = str(tmp_path / "disc.db")
    await _make_db(db_path)
    monkeypatch.setattr(database, "DB_PATH", db_path)

    cleared = await database.backfill_stale_disc_type()
    assert cleared == 1  # only the converted .mkv row

    db = await aiosqlite.connect(db_path)
    async with db.execute("SELECT id FROM scan_results WHERE disc_type IS NOT NULL") as cur:
        kept = sorted(r[0] for r in await cur.fetchall())
    assert kept == [2, 3, 4]  # real discs untouched
    await db.close()

    # Idempotent via sentinel.
    assert await database.backfill_stale_disc_type() == 0
