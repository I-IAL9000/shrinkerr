"""Tests for the v0.9.2 batch language-detection Plex-refresh coalescing.

`detect-languages-batch` used to trigger one Plex folder refresh per file,
so a season of episodes fired one refresh per episode. It now suppresses the
per-file notify and fires a single refresh per unique parent folder.
"""
import os
import pytest

import backend.routes.scan as scan_mod


@pytest.mark.asyncio
async def test_batch_coalesces_plex_refresh_by_folder(monkeypatch):
    notify_calls = []

    async def fake_detect(req, notify_plex=True):
        # The batch must suppress the per-file notify so it can coalesce.
        assert notify_plex is False
        written = "nowrite" not in req.file_path
        return {"status": "ok", "changed": written, "file_written": written}

    async def fake_notify(file_path):
        notify_calls.append(file_path)
        return True

    monkeypatch.setattr(scan_mod, "detect_languages", fake_detect)
    monkeypatch.setattr(scan_mod, "_maybe_notify_plex_lang_change", fake_notify)

    req = scan_mod.DetectLanguagesBatchRequest(file_paths=[
        "/media/Show/S1/e1.mkv",       # same folder as e2 -> one refresh
        "/media/Show/S1/e2.mkv",
        "/media/Movie/m.mkv",          # distinct folder -> its own refresh
        "/media/Show/S1/nowrite.mkv",  # no file write -> no refresh
    ])
    result = await scan_mod.detect_languages_batch(req)

    # Two unique folders had writes -> exactly two refreshes (not 3 writes,
    # not 4 files).
    assert len(notify_calls) == 2
    assert {os.path.dirname(c) for c in notify_calls} == {"/media/Show/S1", "/media/Movie"}
    assert result["folders_refreshed"] == 2
    assert len(result["results"]) == 4


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

    req = scan_mod.DetectLanguagesBatchRequest(
        file_paths=["/media/Show/S1/e1.mkv"])
    result = await scan_mod.detect_languages_batch(req)

    assert notify_calls == []
    assert result["folders_refreshed"] == 0
