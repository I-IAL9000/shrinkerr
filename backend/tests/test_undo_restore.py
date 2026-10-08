"""v0.10.0: Undo of a conversion.

Undo deleted the converted file first and then renamed the backup onto
original_file_path. For a disc that path is the marker inside the disc
(.../BDMV/index.bdmv) while the backup is the BDMV folder, so the disc came
back as a folder named index.bdmv inside a new BDMV/ (CERTIFICATE stayed in
the backup), with the converted file already gone. And any failed restore
left neither file in the library.
"""
from pathlib import Path

import aiosqlite
import pytest

from backend.routes.jobs import undo_conversion


async def _job(db_path, converted: Path, original: Path, backup: Path) -> int:
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "INSERT INTO jobs (file_path, original_file_path, backup_path, job_type, status, created_at) "
            "VALUES (?, ?, ?, 'convert', 'completed', '2026-10-08T00:00:00')",
            (str(converted), str(original), str(backup)),
        )
        await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) "
                         "VALUES (?, 1, '2026-10-08T00:00:00')", (str(converted),))
        await db.commit()
        return cur.lastrowid


@pytest.mark.asyncio
async def test_undo_restores_a_disc_with_its_certificate(test_db, tmp_path):
    title = tmp_path / "Movie (2009)"
    backups = title / ".shrinkerr_backup"
    (backups / "BDMV").mkdir(parents=True)
    (backups / "BDMV" / "index.bdmv").write_bytes(b"INDX0200")
    (backups / "CERTIFICATE").mkdir()
    converted = title / "Movie (2009) 1080p Bluray AC3 5.1 h265.mkv"
    converted.write_bytes(b"converted")
    job_id = await _job(test_db, converted, title / "BDMV" / "index.bdmv", backups / "BDMV")

    await undo_conversion(job_id)

    assert (title / "BDMV" / "index.bdmv").read_bytes() == b"INDX0200"
    assert (title / "CERTIFICATE").is_dir()
    assert not converted.exists()


@pytest.mark.asyncio
async def test_undo_puts_a_release_folder_disc_back(test_db, tmp_path):
    from backend.converter import _dispose_disc_source
    title = tmp_path / "Movie (2009) [tt1]"
    release = title / "Movie (2009) 1080p MLP 5.1 VC-1"
    (release / "BDMV").mkdir(parents=True)
    (release / "BDMV" / "index.bdmv").write_bytes(b"INDX0200")
    (release / "CERTIFICATE").mkdir()
    marker = release / "BDMV" / "index.bdmv"
    converted = title / "Movie (2009) 1080p Bluray AC3 5.1 h265.mkv"
    converted.write_bytes(b"converted")
    backup = await _dispose_disc_source(marker, title, "bdmv", 7, False, "")
    assert not release.exists()
    job_id = await _job(test_db, converted, marker, Path(backup))

    await undo_conversion(job_id)

    assert marker.read_bytes() == b"INDX0200"
    assert (release / "CERTIFICATE").is_dir()
    assert not converted.exists()


@pytest.mark.asyncio
async def test_undo_restores_a_release_folder_backup(test_db, tmp_path):
    """Backups made by early v0.10.0 development builds hold the whole release folder."""
    title = tmp_path / "Movie (2009) [tt1]"
    release = "Movie (2009) 1080p MLP 5.1 VC-1"
    backups = title / ".shrinkerr_backup"
    (backups / release / "BDMV").mkdir(parents=True)
    (backups / release / "BDMV" / "index.bdmv").write_bytes(b"INDX0200")
    converted = title / "Movie (2009) 1080p Bluray AC3 5.1 h265.mkv"
    converted.write_bytes(b"converted")
    job_id = await _job(test_db, converted, title / release / "BDMV" / "index.bdmv", backups / release)

    await undo_conversion(job_id)

    assert (title / release / "BDMV" / "index.bdmv").read_bytes() == b"INDX0200"
    assert not converted.exists()


@pytest.mark.asyncio
async def test_undo_of_a_same_name_conversion(test_db, tmp_path):
    folder = tmp_path / "Movie (2009)"
    (folder / ".shrinkerr_backup").mkdir(parents=True)
    path = folder / "Movie (2009).mkv"
    path.write_bytes(b"converted")
    backup = folder / ".shrinkerr_backup" / "Movie (2009).mkv"
    backup.write_bytes(b"original")
    job_id = await _job(test_db, path, path, backup)

    await undo_conversion(job_id)

    assert path.read_bytes() == b"original"
    assert sorted(p.name for p in folder.iterdir()) == [".shrinkerr_backup", "Movie (2009).mkv"]


@pytest.mark.asyncio
@pytest.mark.parametrize("same_name", [True, False])
async def test_failed_restore_keeps_the_converted_file(test_db, tmp_path, monkeypatch, same_name):
    original = tmp_path / "Movie (2009).mkv"
    converted = original if same_name else tmp_path / "Movie (2009) h265.mkv"
    converted.write_bytes(b"converted")
    (tmp_path / ".shrinkerr_backup").mkdir()
    backup = tmp_path / ".shrinkerr_backup" / "Movie (2009).mkv"
    backup.write_bytes(b"original")
    job_id = await _job(test_db, converted, original, backup)

    real_rename = Path.rename

    def rename(self, target):
        if self == backup:
            raise OSError(18, "Invalid cross-device link")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(Exception):
        await undo_conversion(job_id)
    assert converted.read_bytes() == b"converted"
    assert backup.read_bytes() == b"original"
