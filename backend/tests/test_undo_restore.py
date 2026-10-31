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


@pytest.mark.asyncio
async def test_undo_never_takes_another_movies_disc_backup(test_db, tmp_path):
    """A Blu-ray and a DVD of same-named folders in different libraries
    shared a custom backup slot, and undoing the DVD moved the Blu-ray's
    BDMV/ and CERTIFICATE/ into the DVD's folder."""
    from backend.converter import _dispose_disc_source
    from backend.tests.test_disc_release_folder import _bluray, _dvd
    custom = tmp_path / "backups"
    bd_home = tmp_path / "Movies-4K" / "Movie (2009)"
    dvd_home = tmp_path / "Movies-SD" / "Movie (2009)"
    bd_backup = await _dispose_disc_source(_bluray(bd_home), bd_home, "bdmv", 7, False, str(custom))
    dvd_marker = _dvd(dvd_home)
    dvd_backup = await _dispose_disc_source(dvd_marker, dvd_home, "dvd", 7, False, str(custom))
    assert Path(dvd_backup).parent != Path(bd_backup).parent
    converted = dvd_home / "Movie (2009) 480p DVDRip AC3 2.0 h265.mkv"
    converted.write_bytes(b"converted")
    job_id = await _job(test_db, converted, dvd_marker, Path(dvd_backup))

    await undo_conversion(job_id)

    assert sorted(p.name for p in dvd_home.iterdir()) == ["VIDEO_TS"]
    assert (Path(bd_backup) / "index.bdmv").exists()


def test_restore_only_brings_back_this_discs_companions(tmp_path):
    from backend.routes.jobs import _restore_moves
    slot = tmp_path / "backups" / "Movie (2009)"
    for d in ("BDMV", "CERTIFICATE", "VIDEO_TS", "JACKET_P"):
        (slot / d).mkdir(parents=True)
    home = tmp_path / "Movie (2009)"
    moves = _restore_moves(slot / "VIDEO_TS", home / "VIDEO_TS" / "VIDEO_TS.IFO")
    assert [(a.name, b) for a, b in moves] == [("VIDEO_TS", home / "VIDEO_TS"), ("JACKET_P", home / "JACKET_P")]


@pytest.mark.asyncio
async def test_a_restored_iso_is_a_disc_again(test_db, tmp_path, monkeypatch):
    """A restored Showgirls ISO had no disc type (only disc folders were
    recognised by name), so the Scanner's Disc / ISO filter didn't count it;
    and its row kept the converted file's tracks until it's probed again."""
    import backend.routes.jobs as jobs_route
    from backend.tests.test_dvd_iso_concat import _make_iso
    title = tmp_path / "Film (1995)"
    backups = title / ".shrinkerr_backup"
    backups.mkdir(parents=True)
    _make_iso(backups / "Film (1995) DVD.iso", {"VIDEO_TS.IFO": b"DVDVIDEO-VMG" + b"\0" * 2036,
                                                "VTS_01_0.IFO": b"DVDVIDEO-VTS" + b"\0" * 2036,
                                                "VTS_01_1.VOB": b"\0" * 4096})
    converted = title / "Film (1995) 480p DVD AC3 2.0 h265.mkv"
    converted.write_bytes(b"converted")
    original = title / "Film (1995) DVD.iso"
    job_id = await _job(test_db, converted, original, backups / original.name)
    rescanned = []

    async def rescan(path):
        rescanned.append(path)
    monkeypatch.setattr(jobs_route, "_rescan_restored", rescan)

    await undo_conversion(job_id)

    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT file_path, disc_type FROM scan_results") as cur:
            assert await cur.fetchall() == [(str(original), "dvd")]
    await __import__("asyncio").sleep(0)  # the rescan runs in the background
    assert rescanned == [str(original)]


@pytest.mark.asyncio
async def test_isos_restored_before_the_fix_are_read_again_at_start_up(test_db, monkeypatch):
    """Only an ISO Undo restored and left without a disc type: not one
    with its type, not one that wasn't restored."""
    import backend.routes.jobs as jobs_route
    lost, fine, other = "/m/Lost (1995)/Lost.iso", "/m/Fine (1995)/Fine.iso", "/m/Other (1995)/Other.iso"
    async with aiosqlite.connect(test_db) as db:
        for path, disc, status in ((lost, None, "reverted"), (fine, "bdmv", "reverted"), (other, None, "completed")):
            await db.execute("INSERT INTO scan_results (file_path, file_size, disc_type, scan_timestamp) "
                             "VALUES (?, 1, ?, 't')", (path, disc))
            await db.execute("INSERT INTO jobs (file_path, original_file_path, job_type, status, created_at) "
                             "VALUES (?, ?, 'convert', ?, 't')", (path + ".mkv", path, status))
        await db.commit()
    read = []

    async def rescan(path):
        read.append(path)
    monkeypatch.setattr(jobs_route, "_rescan_restored", rescan)

    assert await jobs_route.rescan_restored_isos() == 1 and read == [lost]
