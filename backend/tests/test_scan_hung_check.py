"""SC-19 (v0.10.0): the health check that runs after a scan was cancelled as
a "hung scan" after 15 minutes — the subprocess had finished, so nothing
touched the progress file while files were being checked."""
import asyncio
import os
import time

import pytest


class _Proc:
    def __init__(self, alive):
        self.alive = alive
        self.killed = False

    def is_alive(self):
        return self.alive

    def kill(self):
        self.killed = True


@pytest.fixture
def scan_mod(tmp_path, monkeypatch):
    import backend.routes.scan as scan_mod
    progress = tmp_path / "scan_progress.json"
    progress.write_text("{}")
    old = time.time() - 20 * 60  # untouched for 20 minutes
    os.utime(progress, (old, old))
    monkeypatch.setattr(scan_mod, "_scan_progress_file", str(progress))
    return scan_mod


@pytest.mark.asyncio
async def test_the_post_scan_health_check_is_not_reaped(scan_mod, monkeypatch):
    task = asyncio.create_task(asyncio.sleep(5))
    monkeypatch.setattr(scan_mod, "_scan_task", task)
    monkeypatch.setattr(scan_mod, "_scan_proc", _Proc(alive=False))  # walking is done
    assert scan_mod.scan_is_actively_running() is True
    assert not task.cancelled()
    task.cancel()


@pytest.mark.asyncio
async def test_a_stuck_scan_subprocess_is_still_reaped(scan_mod, monkeypatch):
    task = asyncio.create_task(asyncio.sleep(5))
    proc = _Proc(alive=True)  # still walking, no progress for 20 minutes
    monkeypatch.setattr(scan_mod, "_scan_task", task)
    monkeypatch.setattr(scan_mod, "_scan_proc", proc)
    assert scan_mod.scan_is_actively_running() is False
    assert proc.killed
    await asyncio.sleep(0)
    assert task.cancelled()
