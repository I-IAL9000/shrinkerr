"""v0.9.157 (F19): /rename/apply renamed whatever paths it was given.

Unlike every other file endpoint it never checked the media directories, so a
request (or a pattern rendering "../") could rename files outside them, and
folder renaming could rename a media directory itself.
"""
import aiosqlite
import pytest
import pytest_asyncio

import backend.media_paths as media_paths
import backend.rename as rename_mod
import backend.routes.rename as rename_route
from backend.rename import RenamePlan


@pytest_asyncio.fixture
async def media(test_db, tmp_path, monkeypatch):
    monkeypatch.setattr(media_paths, "DB_PATH", test_db)
    monkeypatch.setattr(rename_mod, "DB_PATH", test_db)
    root = tmp_path / "media"
    (root / "Movie (2009)").mkdir(parents=True)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO media_dirs (path, label) VALUES (?, '')", (str(root),))
        await db.commit()
    return root


async def _apply(monkeypatch, path, plan):
    async def fake_build_plan(fp, probe, settings):
        return plan
    monkeypatch.setattr(rename_route, "build_plan", fake_build_plan)
    res = await rename_route.apply_rename(rename_route.ApplyRequest(
        file_paths=[str(path)], rescan_arr=False, rescan_plex=False))
    return res["results"][0]


@pytest.mark.asyncio
async def test_file_outside_media_dirs_is_not_renamed(media, tmp_path, monkeypatch):
    outside = tmp_path / "elsewhere" / "notes.mkv"
    outside.parent.mkdir()
    outside.write_bytes(b"x")
    result = await _apply(monkeypatch, outside, RenamePlan(
        old_path=str(outside), new_path=str(outside.with_name("renamed.mkv"))))
    assert result["applied"] is False
    assert outside.exists()


@pytest.mark.asyncio
async def test_target_outside_media_dirs_is_refused(media, tmp_path, monkeypatch):
    src = media / "Movie (2009)" / "movie.mkv"
    src.write_bytes(b"x")
    result = await _apply(monkeypatch, src, RenamePlan(
        old_path=str(src), new_path=str(media / ".." / "escaped.mkv")))
    assert result["applied"] is False
    assert src.exists() and not (tmp_path / "escaped.mkv").exists()


@pytest.mark.asyncio
async def test_media_dir_itself_is_never_renamed(media, monkeypatch):
    src = media / "loose.mkv"   # a file directly in the media dir
    src.write_bytes(b"x")
    result = await _apply(monkeypatch, src, RenamePlan(
        old_path=str(src), new_path=str(src),
        old_folder=str(media), new_folder=str(media.with_name("Loose (2009)"))))
    assert result["applied"] is False
    assert media.is_dir() and src.exists()


@pytest.mark.asyncio
async def test_rename_inside_media_dirs_still_works(media, monkeypatch):
    src = media / "Movie (2009)" / "movie.mkv"
    src.write_bytes(b"x")
    result = await _apply(monkeypatch, src, RenamePlan(
        old_path=str(src), new_path=str(src.with_name("Movie (2009).mkv"))))
    assert result["applied"] is True
    assert (media / "Movie (2009)" / "Movie (2009).mkv").exists()
