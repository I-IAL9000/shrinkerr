"""v0.9.150: the weekly backup must not freeze the server.

The zip step deflated the whole database (multi-GB on a large library) on
the event loop, so every request — e.g. the Queue page's Completed tab —
hung for the ~5 minutes the backup took.
"""
import asyncio
import time
import zipfile

import pytest


@pytest.mark.asyncio
async def test_backup_zip_does_not_block_event_loop(test_db, tmp_path, monkeypatch):
    import backend.routes.settings as settings_mod

    monkeypatch.setattr(settings_mod, "DB_PATH", test_db)
    monkeypatch.setattr(settings_mod, "BACKUP_DIR", tmp_path / "backups")

    real_write = zipfile.ZipFile.write

    def slow_write(self, *a, **kw):
        time.sleep(1.0)  # stands in for deflating a multi-GB database
        return real_write(self, *a, **kw)

    monkeypatch.setattr(zipfile.ZipFile, "write", slow_write)

    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    t = asyncio.create_task(ticker())
    try:
        result = await settings_mod._do_create_backup()
    finally:
        t.cancel()

    assert ticks >= 10, f"event loop starved during backup ({ticks} ticks in >1s)"
    with zipfile.ZipFile(tmp_path / "backups" / result["name"]) as zf:
        assert {"shrinkerr.db", "settings.json"} <= set(zf.namelist())
