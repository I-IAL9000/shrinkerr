"""v0.10.0 (SC-16): duplicate detection."""
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
