"""v0.9.149: a locked DB during a progress write must not kill the encode.

The encode loop awaits a throttled `UPDATE jobs SET progress` write. When
another connection held the write lock past busy_timeout (60s), the raised
"database is locked" escaped the progress callback, the converter turned it
into a plain failed result (bypassing the worker's lock-requeue), and its
generic error branch left ffmpeg running orphaned.
"""
import asyncio
import shutil
import sqlite3
import subprocess

import pytest

from backend.queue import QueueWorker


@pytest.mark.asyncio
async def test_progress_write_swallows_db_lock(test_db, monkeypatch):
    w = QueueWorker(test_db)

    async def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(w.queue, "update_progress", locked)
    await w._write_progress(1, 42.0, fps=100.0, eta=60)  # must not raise


def _has_libx265() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "libx265" in out


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_libx265(), reason="needs ffmpeg with libx265")
async def test_encode_error_kills_ffmpeg(test_db, tmp_path):
    from backend.converter import convert_file

    src = tmp_path / "Show - S01E01 - 1080p WEB h264.mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=120",
         "-c:v", "libx264", "-preset", "ultrafast", str(src)],
        check=True,
    )
    procs = []

    async def progress(**kw):
        raise sqlite3.OperationalError("database is locked")

    try:
        result = await convert_file(
            str(src), "libx265", 120.0,
            progress_callback=progress, proc_callback=procs.append,
            override_libx265_preset="slow",
        )
        assert result["success"] is False
        assert procs, "ffmpeg never started"
        # The failed encode's ffmpeg must not keep running orphaned.
        await asyncio.wait_for(procs[0].wait(), timeout=5)
        assert src.exists(), "original must be untouched"
    finally:
        for p in procs:
            if p.returncode is None:
                p.kill()
                await p.wait()


@pytest.mark.asyncio
async def test_slow_write_transaction_is_logged_with_its_opener(test_db, monkeypatch, capsys):
    import aiosqlite
    import backend.database as database

    monkeypatch.setattr(database, "SLOW_WRITE_TX_SECONDS", 0.0)
    db = await aiosqlite.connect(test_db)
    try:
        await db.execute("SELECT COUNT(*) FROM settings")  # reads don't open a write tx
        assert "[DB] Write lock held" not in capsys.readouterr().out
        await db.execute("INSERT INTO settings (key, value) VALUES ('x', 'y')")
        await db.commit()
    finally:
        await db.close()
    out = capsys.readouterr().out
    assert "[DB] Write lock held" in out
    assert "tests/test_progress_db_lock.py" in out


@pytest.mark.asyncio
async def test_write_transaction_ended_by_close_is_logged(test_db, monkeypatch, capsys):
    import aiosqlite
    import backend.database as database

    monkeypatch.setattr(database, "SLOW_WRITE_TX_SECONDS", 0.0)
    db = await aiosqlite.connect(test_db)
    await db.execute("INSERT INTO settings (key, value) VALUES ('x', 'y')")
    await db.close()  # implicit rollback
    assert "[DB] Write lock held" in capsys.readouterr().out
