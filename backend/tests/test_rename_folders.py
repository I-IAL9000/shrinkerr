"""v0.10.0 (SC-02): "Also rename folders" only touches folders it recognizes.

build_plan assumed every file sits in file → season → show folders: an
episode directly in its show folder renamed the show folder to "Season 01"
and the category folder above it after the show; a movie in a shared
folder renamed that whole folder after itself. In a batch, the first
episode renamed the season/show folders and every later file then failed
"file not found", with Scanner rows pointing at paths that no longer exist.
"""
import aiosqlite
import pytest
import pytest_asyncio

import backend.media_paths as media_paths
import backend.rename as rename_mod
import backend.routes.rename as rename_route
from backend.rename import RenameMeta, RenameSettings, build_plan

FOLDERS = RenameSettings(rename_folders=True)


def _meta(media_type, title, year, season=None, episode=None):
    m = RenameMeta(title=title, year=year, media_type=media_type, season=season, episode=episode)
    if media_type == "tv":
        m.series_title = title
    return m


@pytest.fixture
def metadata(monkeypatch):
    by_path = {}

    async def fake_resolve(fp, probe=None):
        return by_path[fp]

    monkeypatch.setattr(rename_mod, "resolve_metadata", fake_resolve)
    return by_path


def _touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


@pytest.mark.asyncio
async def test_episode_without_a_season_folder_renames_no_folders(tmp_path, metadata):
    ep = _touch(tmp_path / "tv" / "Mini Series (2019)" / "Mini.Series.S01E01.mkv")
    metadata[str(ep)] = _meta("tv", "Mini Series", "2019", 1, 1)
    plan = await build_plan(str(ep), None, FOLDERS)
    assert plan.old_season_folder is None and plan.old_folder is None


@pytest.mark.asyncio
async def test_episode_in_a_season_folder_renames_season_and_show(tmp_path, metadata):
    ep = _touch(tmp_path / "tv" / "show" / "season 1" / "Show.S01E01.mkv")
    metadata[str(ep)] = _meta("tv", "Show", "2019", 1, 1)
    plan = await build_plan(str(ep), None, FOLDERS)
    assert plan.old_season_folder == str(tmp_path / "tv" / "show" / "season 1")
    assert plan.old_folder == str(tmp_path / "tv" / "show")


@pytest.mark.asyncio
async def test_movie_alone_in_its_folder_renames_the_folder(tmp_path, metadata):
    mv = _touch(tmp_path / "movies" / "heat" / "Heat.1995.mkv")
    metadata[str(mv)] = _meta("movie", "Heat", "1995")
    plan = await build_plan(str(mv), None, FOLDERS)
    assert plan.old_folder == str(tmp_path / "movies" / "heat")


@pytest.mark.asyncio
async def test_movie_in_a_shared_folder_renames_no_folder(tmp_path, metadata):
    mv = _touch(tmp_path / "movies" / "Heat.1995.mkv")
    _touch(tmp_path / "movies" / "Alien.1979.mkv")
    metadata[str(mv)] = _meta("movie", "Heat", "1995")
    plan = await build_plan(str(mv), None, FOLDERS)
    assert plan.old_folder is None


@pytest_asyncio.fixture
async def media(test_db, tmp_path, monkeypatch):
    monkeypatch.setattr(media_paths, "DB_PATH", test_db)
    monkeypatch.setattr(rename_mod, "DB_PATH", test_db)
    root = tmp_path / "media"
    root.mkdir()
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO media_dirs (path, label) VALUES (?, '')", (str(root),))
        await db.commit()
    return root


@pytest.mark.asyncio
async def test_a_batch_follows_folders_renamed_by_earlier_files(media, test_db, metadata, monkeypatch):
    season = media / "tv" / "show" / "season 1"
    eps = [_touch(season / f"Show.S01E0{n}.mkv") for n in (1, 2)]
    async with aiosqlite.connect(test_db) as db:
        for ep in eps:
            await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) "
                             "VALUES (?, 1, '2026-10-08T00:00:00')", (str(ep),))
        await db.commit()

    async def fake_resolve(fp, probe=None):
        n = int(fp[-6:-4])  # ...E01.mkv
        return _meta("tv", "Show", "2019", 1, n)

    monkeypatch.setattr(rename_mod, "resolve_metadata", fake_resolve)
    monkeypatch.setattr(rename_route, "resolve_metadata", fake_resolve, raising=False)

    async def folders_on():
        return FOLDERS

    monkeypatch.setattr(rename_route, "get_settings", folders_on)
    res = await rename_route.apply_rename(rename_route.ApplyRequest(
        file_paths=[str(e) for e in eps], rescan_arr=False, rescan_plex=False))

    assert [r["applied"] for r in res["results"]] == [True, True], res["results"]
    new_paths = [r["new_path"] for r in res["results"]]
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT file_path FROM scan_results ORDER BY file_path") as cur:
            rows = [r[0] for r in await cur.fetchall()]
    assert rows == sorted(new_paths)
    assert all(__import__("os").path.exists(p) for p in new_paths)
