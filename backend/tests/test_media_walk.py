"""v0.10.0: what the Scanner and the watcher walk.

SC-03: os.walk silently skips a folder it can't list. On a CIFS mount a
stall shows up as EIO for that folder, the walk "succeeds", and both the
watcher's stale cleanup and the full scan's orphan cleanup deleted every row
under it — the files came back as new, with manual matches and per-track
edits lost.
SC-06: only hidden *files* were skipped, so Shrinkerr's own backups
(.shrinkerr_backup), NAS recycle bins (#recycle, @eaDir) and tool trash
(.deletedByTMM) were scanned, and backups were queued and converted again.
"""
import os
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from backend.scanner import walk_media_dir

HIDDEN = [".shrinkerr_backup", "#recycle", "@eaDir", ".deletedByTMM", ".Trash-1000"]


def _tree(root: Path) -> None:
    (root / "Show A").mkdir(parents=True)
    (root / "Show A" / "ep1.mkv").write_bytes(b"x")
    (root / "Show B").mkdir()
    (root / "Show B" / "ep1.mkv").write_bytes(b"x")
    for name in HIDDEN:
        (root / "Show A" / name).mkdir()
        (root / "Show A" / name / "ep1.mkv").write_bytes(b"x")
    # A backed-up disc must not turn the backup folder into a "disc".
    (root / "Show A" / ".shrinkerr_backup" / "BDMV").mkdir()
    (root / "Show A" / ".shrinkerr_backup" / "BDMV" / "index.bdmv").write_bytes(b"x")


def _unlistable(monkeypatch, folder: Path) -> None:
    """Make `folder` fail to list, as a stalled CIFS folder does (works as root too)."""
    real = os.scandir

    def scandir(path="."):
        if os.fspath(path) == str(folder):
            raise OSError(5, "Input/output error", str(folder))
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)


def test_walk_skips_hidden_and_system_folders(tmp_path):
    _tree(tmp_path)
    unreadable: list[str] = []
    files = sorted(str(Path(r) / f) for r, _, fs in walk_media_dir(str(tmp_path), unreadable) for f in fs)
    assert files == [str(tmp_path / "Show A" / "ep1.mkv"), str(tmp_path / "Show B" / "ep1.mkv")]
    assert unreadable == []


def test_walk_reports_folders_it_cannot_list(tmp_path, monkeypatch):
    _tree(tmp_path)
    _unlistable(monkeypatch, tmp_path / "Show B")
    unreadable: list[str] = []
    files = [f for _, _, fs in walk_media_dir(str(tmp_path), unreadable) for f in fs]
    assert files == ["ep1.mkv"]
    assert unreadable == [str(tmp_path / "Show B")]


async def _seed(db_path, media: Path, rows: list[Path]) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO media_dirs (path, auto_scan) VALUES (?, 1)", (str(media),))
        for p in rows:
            await db.execute("INSERT INTO scan_results (file_path, file_size, scan_timestamp) "
                             "VALUES (?, 0, '2026-10-08T00:00:00Z')", (str(p),))
        await db.commit()


async def _rows(db_path) -> list[str]:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path FROM scan_results ORDER BY file_path") as cur:
            return [r[0] for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_watcher_keeps_rows_under_a_folder_it_cannot_list(test_db, tmp_path, monkeypatch):
    from backend.watcher import FileWatcher
    media = tmp_path / "TV"
    _tree(media)
    rows = [media / "Show A" / "ep1.mkv", media / "Show B" / "ep1.mkv"]
    await _seed(test_db, media, rows)
    _unlistable(monkeypatch, media / "Show B")
    with patch("backend.scanner.probe_file", new_callable=AsyncMock) as probe:
        await FileWatcher(test_db, interval_minutes=5).check_once()
    assert await _rows(test_db) == [str(p) for p in rows]
    probed = [str(c.args[0]) for c in probe.await_args_list]
    assert not [p for p in probed if any(f"/{h}/" in p for h in HIDDEN)], probed


def test_full_scan_keeps_rows_under_a_folder_it_cannot_list(test_db, tmp_path, monkeypatch):
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    from backend.routes.scan import _scan_worker_process
    from backend.tests.test_same_name_conversion import _mkv
    media = tmp_path / "TV"
    _tree(media)
    for p in [media / "Show A" / "ep1.mkv", media / "Show B" / "ep1.mkv"]:
        _mkv(p, ["eng"])
    gone = media / "Show A" / "deleted since the last scan.mkv"
    rows = [media / "Show A" / "ep1.mkv", media / "Show B" / "ep1.mkv", gone]
    import asyncio
    asyncio.run(_seed(test_db, media, rows))
    _unlistable(monkeypatch, media / "Show B")

    _scan_worker_process([str(media)], test_db, str(tmp_path / "progress.json"), str(tmp_path / "cancel"))

    remaining = asyncio.run(_rows(test_db))
    assert str(media / "Show B" / "ep1.mkv") in remaining   # couldn't be listed: kept
    assert str(gone) not in remaining                        # really gone: removed
    assert not [p for p in remaining if any(f"/{h}/" in p for h in HIDDEN)], remaining


def test_full_scan_of_an_unmounted_share_keeps_its_rows(test_db, tmp_path):
    """An unmounted share is an empty mountpoint: the walk "succeeds", finds
    nothing, and every row under it was deleted (v0.10.0)."""
    import asyncio
    from backend.routes.scan import _scan_worker_process
    media = tmp_path / "Movies"
    media.mkdir()
    rows = [media / f"Movie {i} (2009)" / f"Movie {i} (2009).mkv" for i in range(5)]
    asyncio.run(_seed(test_db, media, rows))

    _scan_worker_process([str(media)], test_db, str(tmp_path / "progress.json"), str(tmp_path / "cancel"))

    assert asyncio.run(_rows(test_db)) == sorted(str(p) for p in rows)
