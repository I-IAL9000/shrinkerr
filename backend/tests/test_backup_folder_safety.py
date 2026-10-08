"""v0.10.0: backup expiry and "Delete backups" must only ever delete backups.

Expiry removed every old file and folder inside the custom backup folder's
folders, following symlinks: a backup folder set to (or above) the media
folder expired the library itself. Once disc backups (folders) could expire,
the first sweep would also have deleted every disc backup made before, all
dated with the disc's own old date. "Delete backups" accepted any path with
".shrinkerr_backup" in it, "../" included.
"""
import os
import time

import aiosqlite
import pytest

import backend.queue as queue_mod
from backend.media_paths import backup_folder_conflict

OLD = time.time() - 400 * 86400


def _age(path):
    os.utime(path, (OLD, OLD))


def _old_file(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    _age(path)
    return path


async def _settings(db_path, media, **values):
    async with aiosqlite.connect(db_path) as db:
        for key, value in values.items():
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
        await db.execute("INSERT INTO media_dirs (path) VALUES (?)", (str(media),))
        await db.commit()


@pytest.fixture
def sweep(test_db, monkeypatch):
    import backend.database as database
    monkeypatch.setattr(database, "DB_PATH", test_db)

    async def run():
        monkeypatch.setattr(queue_mod, "_last_backup_cleanup", 0)
        await queue_mod._cleanup_expired_backups()
    return run


def test_backup_folder_conflict(tmp_path):
    media = tmp_path / "media"
    (media / "Movies").mkdir(parents=True)
    (media / ".backups").mkdir()
    (tmp_path / "backups").mkdir()
    assert backup_folder_conflict(str(media), [str(media)]) == str(media)
    assert backup_folder_conflict(str(tmp_path), [str(media)]) == str(media)
    assert backup_folder_conflict(str(media / "Movies"), [str(media)]) == str(media)
    assert backup_folder_conflict(str(media / ".backups"), [str(media)]) is None
    assert backup_folder_conflict(str(tmp_path / "backups"), [str(media)]) is None


def test_backup_folder_conflict_ignores_spelling(tmp_path):
    """Case-insensitive filesystems and SMB shares spell one folder many ways."""
    media = tmp_path / "Media" / "Movies"
    media.mkdir(parents=True)
    other = tmp_path / "media" / "movies"
    if not other.exists():
        pytest.skip("case-sensitive filesystem")
    assert backup_folder_conflict(str(other), [str(media)]) == str(media)
    assert backup_folder_conflict(str(tmp_path / "MEDIA"), [str(media)]) == str(media)
    (media / "Film (2001)").mkdir()
    assert backup_folder_conflict(str(other / "film (2001)"), [str(media)]) == str(media)


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["media", "above"])
async def test_sweep_leaves_a_library_used_as_backup_folder_alone(test_db, tmp_path, sweep, where):
    media = tmp_path / "media"
    movie = _old_file(media / "Movie (2009)" / "Movie (2009).mkv")
    disc = _old_file(media / "Other (2010)" / "BDMV" / "index.bdmv")
    for folder in (disc.parent, disc.parent.parent, movie.parent):
        _age(folder)
    await _settings(test_db, media, backup_original_days=7, disc_backups_redated=1,
                    backup_folder=media if where == "media" else tmp_path)
    await sweep()
    assert movie.exists() and disc.exists()


@pytest.mark.asyncio
async def test_sweep_does_not_follow_symlinks_out_of_the_backup_folder(test_db, tmp_path, sweep):
    media = tmp_path / "media"
    movie = _old_file(media / "Movie (2009)" / "Movie (2009).mkv")
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "Movie (2009)").symlink_to(media / "Movie (2009)")
    await _settings(test_db, media, backup_original_days=7, disc_backups_redated=1, backup_folder=backups)
    await sweep()
    assert movie.exists()


@pytest.mark.asyncio
async def test_sweep_only_removes_folders_that_are_disc_backups(test_db, tmp_path, sweep):
    media = tmp_path / "media"
    media.mkdir()
    backups = tmp_path / "backups" / "Movie (2009)"
    old_file = _old_file(backups / "Movie (2009).mkv")
    _old_file(backups / "BDMV" / "index.bdmv")
    _old_file(backups / "Movie (2009) Disc 1" / "BDMV" / "index.bdmv")
    stray = _old_file(backups / "Featurettes" / "Making of.mkv")
    for d in ("BDMV", "Movie (2009) Disc 1", "Featurettes"):
        _age(backups / d)
    await _settings(test_db, media, backup_original_days=7, disc_backups_redated=1,
                    backup_folder=tmp_path / "backups")
    await sweep()
    assert not old_file.exists()
    assert sorted(p.name for p in backups.iterdir()) == ["Featurettes"]
    assert stray.exists()


@pytest.mark.asyncio
async def test_first_sweep_redates_existing_disc_backups(test_db, tmp_path, sweep):
    media = tmp_path / "media"
    backups = media / "Movie (2009)" / ".shrinkerr_backup"
    _old_file(backups / "BDMV" / "index.bdmv")
    _age(backups / "BDMV")
    await _settings(test_db, media, backup_original_days=7)

    await sweep()
    assert (backups / "BDMV" / "index.bdmv").exists()
    assert time.time() - os.stat(backups / "BDMV").st_mtime < 60

    _age(backups / "BDMV")  # its retention period runs out
    await sweep()
    assert not (backups / "BDMV").exists()


@pytest.mark.asyncio
async def test_backup_folder_inside_the_library_is_refused(test_db, tmp_path, monkeypatch):
    pytest.importorskip("apscheduler")
    import backend.routes.settings as route
    from backend.api_errors import ApiError
    from backend.models import SettingsUpdate
    monkeypatch.setattr(route, "DB_PATH", test_db)
    media = tmp_path / "media"
    (media / "Movies").mkdir(parents=True)
    (tmp_path / "backups").mkdir()
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO media_dirs (path) VALUES (?)", (str(media),))
        await db.commit()

    for bad in (media, tmp_path, media / "Movies"):
        with pytest.raises(ApiError) as exc:
            await route.update_encoding_settings(SettingsUpdate(backup_folder=str(bad)))
        assert exc.value.code == "settings.backupFolderInLibrary"
    await route.update_encoding_settings(SettingsUpdate(backup_folder=str(tmp_path / "backups")))


@pytest.mark.asyncio
async def test_delete_backups_only_deletes_backups(test_db, tmp_path, monkeypatch):
    pytest.importorskip("apscheduler")
    import backend.routes.settings as route
    monkeypatch.setattr(route, "DB_PATH", test_db)
    media = tmp_path / "media"
    movie = _old_file(media / "Movie (2009)" / "Movie (2009).mkv")
    backup = _old_file(media / "Movie (2009)" / ".shrinkerr_backup" / "Movie (2009) h264.mkv")
    await _settings(test_db, media, backup_folder=media)

    traversal = str(media / "Movie (2009)" / ".shrinkerr_backup" / ".." / "Movie (2009).mkv")
    res = await route.delete_backups(route.DeleteBackupsRequest(paths=[traversal, str(movie)]))
    assert res["deleted"] == 0
    res = await route.delete_backups(route.DeleteBackupsRequest(paths=[]))  # "Delete all"
    assert res["deleted"] == 1
    assert movie.exists() and not backup.exists()
