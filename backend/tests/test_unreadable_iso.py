"""v0.9.153: disc images no reader can open show up as "unreadable".

They used to vanish: the watcher skipped a failed probe without storing a
row, and a full scan stored it as "corrupt" with no reason — even though
the image (e.g. a Blu-ray whose UDF metadata file libudfread rejects) is
intact and plays elsewhere.
"""
import json
import os
import shutil
import sqlite3
import subprocess

import pytest

UDF_STDERR = """dir_posix.c:109: Error opening dir {p}
udfread ERROR: read metadata file 0: unexpected tag 261
udfread ERROR: read metadata file 1: unexpected tag 261
udfread ERROR: unknown partition 1
disc.c:333: failed opening UDF image {p}
disc.c:437: error opening file BDMV/index.bdmv
[bluray @ 0x559cce206680] bd_open() failed
bluray:{p}: Input/output error
"""


def test_diagnosis_names_the_udfread_error(tmp_path, monkeypatch):
    import backend.disc_metadata as dm
    iso = tmp_path / "Pure Country (1992) BR-DISK.iso"
    iso.write_bytes(b"\0" * 4096)

    class R:
        returncode = 1
        stdout = ""
        stderr = UDF_STDERR.format(p=iso)

    monkeypatch.setattr(dm, "_classify_disc_iso", lambda p: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: R())

    reason = json.loads(dm.diagnose_unreadable_iso(iso))

    assert reason["kind"] == "bluray"
    assert reason["detail"].startswith("udfread ERROR: read metadata file 0: unexpected tag 261")
    assert str(iso) not in reason["detail"]  # paths shortened to <iso>


def _junk_iso(folder):
    os.makedirs(folder, exist_ok=True)
    p = os.path.join(folder, "Movie (2001) BR-DISK.iso")
    with open(p, "wb") as f:
        f.write(b"\0" * 512 * 1024)  # deterministic: random bytes can sniff as audio
    return p


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("ffprobe"), reason="needs ffprobe")
async def test_full_scan_marks_unprobeable_iso_unreadable(test_db, tmp_path):
    from backend.scanner import scan_directory
    iso = _junk_iso(str(tmp_path / "media" / "Movie (2001)"))

    results = await scan_directory(str(tmp_path / "media"))

    row = next(r for r in results if r.file_path == iso)
    assert row.probe_status == "unreadable"
    assert json.loads(row.probe_error)["kind"] in ("dvd", "bluray", "unknown")


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("ffprobe"), reason="needs ffprobe")
async def test_watcher_records_unreadable_iso_instead_of_dropping_it(test_db, tmp_path):
    from backend.watcher import FileWatcher
    iso = _junk_iso(str(tmp_path / "media" / "Movie (2001)"))

    added = await FileWatcher(test_db)._scan_new_files([iso])

    assert added == 1
    db = sqlite3.connect(test_db)
    try:
        status, error = db.execute(
            "SELECT probe_status, probe_error FROM scan_results WHERE file_path = ?", (iso,)).fetchone()
    finally:
        db.close()
    assert status == "unreadable"
    assert json.loads(error)["detail"]
