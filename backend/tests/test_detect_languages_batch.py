"""Tests for the v0.9.2 batch language-detection Plex-refresh coalescing.

`detect-languages-batch` used to trigger one Plex folder refresh per file,
so a season of episodes fired one refresh per episode. It now suppresses the
per-file notify and fires a single refresh per unique parent folder.
"""
import os
import aiosqlite
import pytest

import backend.database as database
import backend.routes.scan as scan_mod


@pytest.mark.asyncio
async def test_batch_coalesces_plex_refresh_by_section(monkeypatch):
    """v0.9.26: the batch collects the folders it actually wrote to and fires a
    single deduped section refresh at the end (one call, covering one
    representative file per written folder) — not one refresh per file."""
    import backend.plex as plex_mod
    import backend.scanner as scanner_mod
    refresh_calls = []

    async def fake_detect(req, notify_plex=True):
        # The batch must suppress the per-file notify so it can coalesce.
        assert notify_plex is False
        written = "nowrite" not in req.file_path
        return {"status": "ok", "changed": written, "file_written": written}

    async def fake_refresh(file_paths):
        refresh_calls.append(list(file_paths))
        return 1

    monkeypatch.setattr(scan_mod, "detect_languages", fake_detect)
    monkeypatch.setattr(plex_mod, "refresh_plex_sections_for_files", fake_refresh)
    monkeypatch.setattr(scanner_mod, "_is_cleanup_enabled", lambda *a, **k: True)
    scan_mod._detect_progress = {"active": True, "total": 0, "done": 0,
                                 "current": "", "changed": 0, "failed": 0, "cancelled": False}

    await scan_mod._run_detect_batch([
        "/media/Show/S1/e1.mkv",       # same folder as e2 -> one rep file
        "/media/Show/S1/e2.mkv",
        "/media/Movie/m.mkv",          # distinct folder -> its own rep file
        "/media/Show/S1/nowrite.mkv",  # no file write -> not represented
    ])

    # One coalesced refresh call, given one representative file per written
    # folder (dedup down to sections happens inside refresh_plex_sections...).
    assert len(refresh_calls) == 1
    assert {os.path.dirname(c) for c in refresh_calls[0]} == {"/media/Show/S1", "/media/Movie"}
    p = scan_mod._detect_progress
    assert p["done"] == 4 and p["changed"] == 3 and p["active"] is False


@pytest.mark.asyncio
async def test_expand_paths_folders_to_und_files_only(tmp_path, monkeypatch):
    db_path = str(tmp_path / "expand.db")
    db = await aiosqlite.connect(db_path)
    await db.execute(
        "CREATE TABLE scan_results (file_path TEXT, removed_from_list INTEGER DEFAULT 0, "
        "has_und_tracks_flag INTEGER DEFAULT 0)"
    )
    await db.executemany(
        "INSERT INTO scan_results (file_path, removed_from_list, has_und_tracks_flag) VALUES (?, ?, ?)",
        [
            ("/media/Show/S1/e1.mkv", 0, 1),   # und -> included
            ("/media/Show/S1/e2.mkv", 0, 0),   # no und -> excluded
            ("/media/Show/S1/e3.mkv", 1, 1),   # removed -> excluded
            ("/media/Other/m.mkv", 0, 1),      # different folder -> excluded
        ],
    )
    await db.commit()
    await db.close()
    monkeypatch.setattr(database, "DB_PATH", db_path)

    # Folder selection expands to only its und, non-removed files; an explicit
    # file path passes through unchanged even though it's not in the DB.
    resolved = await scan_mod._expand_paths_for_detection(
        ["/media/Show/S1/", "/media/explicit/hand-picked.mkv"]
    )
    assert set(resolved) == {"/media/Show/S1/e1.mkv", "/media/explicit/hand-picked.mkv"}


@pytest.mark.asyncio
async def test_batch_no_writes_no_refresh(monkeypatch):
    notify_calls = []

    async def fake_detect(req, notify_plex=True):
        return {"status": "ok", "changed": False}

    async def fake_notify(file_path):
        notify_calls.append(file_path)
        return True

    monkeypatch.setattr(scan_mod, "detect_languages", fake_detect)
    monkeypatch.setattr(scan_mod, "_maybe_notify_plex_lang_change", fake_notify)
    scan_mod._detect_progress = {"active": True, "total": 0, "done": 0,
                                 "current": "", "changed": 0, "failed": 0, "cancelled": False}

    await scan_mod._run_detect_batch(["/media/Show/S1/e1.mkv"])

    assert notify_calls == []
    assert scan_mod._detect_progress["changed"] == 0


@pytest.mark.asyncio
async def test_run_detect_batch_stops_on_cancel(monkeypatch):
    """Setting cancelled mid-run stops before the next file (no more detects)."""
    seen = []

    async def fake_detect(req, notify_plex=True):
        seen.append(req.file_path)
        scan_mod._detect_progress["cancelled"] = True   # cancel after the first
        return {"status": "ok", "changed": False}

    async def fake_notify(file_path):
        return True

    monkeypatch.setattr(scan_mod, "detect_languages", fake_detect)
    monkeypatch.setattr(scan_mod, "_maybe_notify_plex_lang_change", fake_notify)
    scan_mod._detect_progress = {"active": True, "total": 0, "done": 0,
                                 "current": "", "changed": 0, "failed": 0, "cancelled": False}

    await scan_mod._run_detect_batch(["/m/a.mkv", "/m/b.mkv", "/m/c.mkv"])

    assert seen == ["/m/a.mkv"]   # stopped after the first, before b/c
    assert scan_mod._detect_progress["active"] is False


@pytest.mark.asyncio
async def test_detect_languages_requests_raw_und_subs(monkeypatch):
    """Regression: detect_languages must probe with detect_und_subs=False, or
    probe_file's inline detection masks the und sub and it's skipped (never
    persisted/written)."""
    from fastapi import HTTPException
    import backend.scanner as scanner_mod
    seen = {}

    async def fake_probe(fp, detect_und_subs=True):
        seen["detect_und_subs"] = detect_und_subs
        return None  # force the early 404 after recording the arg

    monkeypatch.setattr(scanner_mod, "probe_file", fake_probe)
    with pytest.raises(HTTPException):
        await scan_mod.detect_languages(scan_mod.DetectLanguagesRequest(file_path="/x.mkv"))
    assert seen["detect_und_subs"] is False
