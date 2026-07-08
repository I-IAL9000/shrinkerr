"""Tests for the v0.7.32 hung-scan reaping helper.

`scan_is_actively_running()` breaks the "Scan in progress, skipping
cycle" deadlock that happens when a scan subprocess hangs on a dead
filesystem mount: its progress file goes stale, the helper detects
that, reaps the stuck subprocess, and clears `_scan_task` so the
watcher resumes.
"""
import time
import types
import pytest

import backend.routes.scan as scan_mod
from backend.routes.scan import scan_is_actively_running


@pytest.fixture(autouse=True)
def _reset_scan_state():
    """Save/restore module state so tests don't leak into each other."""
    saved = (scan_mod._scan_task, scan_mod._scan_proc)
    yield
    scan_mod._scan_task, scan_mod._scan_proc = saved


class _FakeTask:
    """Minimal asyncio.Task stand-in."""
    def __init__(self, done: bool):
        self._done = done
        self.cancelled = False

    def done(self):
        return self._done

    def cancel(self):
        self.cancelled = True


class _FakeProc:
    def __init__(self, alive: bool):
        self._alive = alive
        self.killed = False

    def is_alive(self):
        return self._alive

    def kill(self):
        self.killed = True
        self._alive = False


def test_no_task_is_not_running():
    scan_mod._scan_task = None
    assert scan_is_actively_running() is False


def test_done_task_is_not_running():
    scan_mod._scan_task = _FakeTask(done=True)
    assert scan_is_actively_running() is False


def test_fresh_progress_is_running(tmp_path, monkeypatch):
    """A live task with a freshly-updated progress file → running."""
    pf = tmp_path / "progress.json"
    pf.write_text('{"status": "scanning"}')
    # mtime is now → fresh
    monkeypatch.setattr(scan_mod, "_scan_progress_file", str(pf))
    scan_mod._scan_task = _FakeTask(done=False)
    scan_mod._scan_proc = _FakeProc(alive=True)
    assert scan_is_actively_running() is True


def test_missing_progress_file_treated_as_just_started(tmp_path, monkeypatch):
    """No progress file yet (subprocess spinning up) → treat as running,
    don't reap prematurely."""
    monkeypatch.setattr(
        scan_mod, "_scan_progress_file", str(tmp_path / "nonexistent.json")
    )
    scan_mod._scan_task = _FakeTask(done=False)
    scan_mod._scan_proc = _FakeProc(alive=True)
    assert scan_is_actively_running() is True


def test_stale_progress_reaps_and_returns_false(tmp_path, monkeypatch):
    """A live task whose progress file is older than the threshold is
    treated as hung: the subprocess is killed, the task cancelled, and
    the helper returns False so the watcher resumes."""
    pf = tmp_path / "progress.json"
    pf.write_text('{"status": "scanning"}')
    # Backdate the mtime well past the threshold.
    monkeypatch.setattr(scan_mod, "_scan_progress_file", str(pf))
    monkeypatch.setattr(scan_mod, "STALE_SCAN_MINUTES", 15)
    old = time.time() - (16 * 60)  # 16 min ago > 15 min threshold
    import os
    os.utime(str(pf), (old, old))

    task = _FakeTask(done=False)
    proc = _FakeProc(alive=True)
    scan_mod._scan_task = task
    scan_mod._scan_proc = proc

    result = scan_is_actively_running()

    assert result is False, "stale scan should be reaped → not running"
    assert proc.killed is True, "hung subprocess should be killed"
    assert task.cancelled is True, "hung task should be cancelled"
    assert scan_mod._scan_task is None, "_scan_task should be cleared"
    assert scan_mod._scan_proc is None, "_scan_proc should be cleared"
