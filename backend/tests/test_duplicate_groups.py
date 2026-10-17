"""v0.10.0 (SC-16): duplicate detection."""
import pytest

from backend.routes.scan import duplicate_groups


def test_episodes_of_different_seasons_are_not_duplicates():
    files = ["/tv/Show/Show - S01E01.mkv", "/tv/Show/Show - S02E01.mkv"]
    assert duplicate_groups(files) == {}


def test_the_same_episode_twice_is():
    files = ["/tv/Show/Season 01/Show - S01E01 - 1080p.mkv", "/tv/Show/Season 01/Show - S01E01 - 2160p.mkv",
             "/tv/Show/Season 01/Show - S01E02 - 1080p.mkv"]
    assert duplicate_groups(files) == {"ep:/tv/Show/Season 01/S1E1": files[:2]}


def test_a_movies_extras_and_parts_are_not_duplicates():
    folder = "/movies/Film (2009)"
    assert duplicate_groups([f"{folder}/Film (2009).mkv", f"{folder}/Film (2009)-trailer.mkv",
                             f"{folder}/Film (2009) sample.mkv"]) == {}
    assert duplicate_groups([f"{folder}/Film (2009) CD1.avi", f"{folder}/Film (2009) CD2.avi"]) == {}


def test_two_versions_of_a_movie_are():
    folder = "/movies/Film (2009)"
    files = [f"{folder}/Film (2009) 1080p.mkv", f"{folder}/Film (2009) 2160p.mkv"]
    assert duplicate_groups(files) == {f"folder:{folder}": files}


def test_a_resolved_duplicate_is_cleared(test_db, tmp_path):
    """The reset was only committed when some duplicate was found."""
    import asyncio
    import shutil
    import aiosqlite
    import pytest
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    from backend.routes.scan import _scan_worker_process
    from backend.tests.test_same_name_conversion import _mkv
    folder = tmp_path / "Movies" / "Film (2009)"
    folder.mkdir(parents=True)
    kept = folder / "Film (2009) 2160p.mkv"
    _mkv(kept, ["eng"])

    async def seed():
        async with aiosqlite.connect(test_db) as db:
            await db.execute("INSERT INTO media_dirs (path) VALUES (?)", (str(tmp_path / "Movies"),))
            await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp, dup_count, dup_group) "
                             "VALUES (?, 1, '2026-10-08T00:00:00', 2, 'folder:x')", (str(kept),))
            await db.commit()

    async def dup_count():
        async with aiosqlite.connect(test_db) as db:
            async with db.execute("SELECT dup_count FROM scan_results WHERE file_path = ?", (str(kept),)) as cur:
                return (await cur.fetchone())[0]

    asyncio.run(seed())
    _scan_worker_process([str(folder)], test_db, str(tmp_path / "p.json"), str(tmp_path / "c"))
    assert asyncio.run(dup_count()) == 0


def test_the_reset_writes_only_the_rows_it_changes(test_db, tmp_path):
    """Each row written is logged for the filter index: every full scan wrote
    every listed row."""
    import shutil
    import sqlite3
    from contextlib import closing
    import pytest
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    from backend.routes.scan import _scan_worker_process
    from backend.tests.test_same_name_conversion import _mkv
    folder = tmp_path / "Movies" / "Film (2009)"
    folder.mkdir(parents=True)
    _mkv(folder / "Film (2009) 2160p.mkv", ["eng"])
    with closing(sqlite3.connect(test_db)) as db:
        db.execute("INSERT INTO media_dirs (path) VALUES (?)", (str(tmp_path / "Movies"),))
        db.execute("CREATE TABLE dup_writes (row_id INTEGER)")
        db.execute("CREATE TRIGGER dup_written AFTER UPDATE OF dup_count, dup_group ON scan_results "
                   "BEGIN INSERT INTO dup_writes VALUES (NEW.id); END")
        db.commit()
    _scan_worker_process([str(folder)], test_db, str(tmp_path / "p.json"), str(tmp_path / "c"))
    with closing(sqlite3.connect(test_db)) as db:
        assert db.execute("SELECT COUNT(*) FROM scan_results").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM dup_writes").fetchone()[0] == 0


def test_files_in_an_extras_folder_are_not_duplicates():
    """A show's Specials/Deleted Scenes, a film's Featurettes: extras, named
    with the "-scene" suffix or not."""
    scenes = "/tv/The Office (2001)/Specials/Deleted Scenes"
    assert duplicate_groups([f"{scenes}/Freelove Freeway-scene.mkv", f"{scenes}/Who Cares Wins-scene.mkv"]) == {}
    assert duplicate_groups([f"{scenes}/Freelove Freeway.mkv", f"{scenes}/Who Cares Wins.mkv"]) == {}
    extras = "/movies/Film (2009)/Featurettes"
    assert duplicate_groups([f"{extras}/Making Of.mkv", f"{extras}/Cast Interviews.mkv"]) == {}


@pytest.mark.asyncio
async def test_start_up_recounts_duplicates_flagged_before_the_fix(test_db):
    """A folder's counts were redone only when a scan walked it, so files
    flagged before SC-16 kept the flag."""
    import aiosqlite
    from backend.database import init_db
    scenes = "/tv/The Office (2001)/Specials/Deleted Scenes"
    season = "/tv/Show/Season 01"
    film = "/movies/The Biggest Rescue (2014)"
    parts = [f"{film}/The Biggest Rescue (2014) - pt{i} 576p MP2 2.0 h265-MAiN.mkv" for i in (1, 2, 3)]
    rows = [(f"{scenes}/Freelove Freeway.mkv", 2, f"folder:{scenes}"), (f"{scenes}/Who Cares Wins.mkv", 2, f"folder:{scenes}"),
            (f"{season}/Show - S01E01 - 1080p.mkv", 0, None), (f"{season}/Show - S01E01 - 2160p.mkv", 0, None),
            *((part, 3, f"folder:{film}") for part in parts)]
    async with aiosqlite.connect(test_db) as db:
        await db.execute("DELETE FROM settings WHERE key = '_v0_10_0_recount_duplicates'")
        await db.executemany("INSERT INTO scan_results (file_path, file_size, dup_count, dup_group, scan_timestamp) "
                             "VALUES (?, 1, ?, ?, 't')", rows)
        await db.commit()

    await init_db()

    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT file_path, dup_count, dup_group FROM scan_results ORDER BY file_path") as cur:
            got = await cur.fetchall()
    assert got == [*((part, 0, None) for part in parts),
                   (f"{season}/Show - S01E01 - 1080p.mkv", 2, f"ep:{season}/S1E1"),
                   (f"{season}/Show - S01E01 - 2160p.mkv", 2, f"ep:{season}/S1E1"),
                   (f"{scenes}/Freelove Freeway.mkv", 0, None), (f"{scenes}/Who Cares Wins.mkv", 0, None)]
