"""v0.10.0 (M9): track removals are checked against the file at job time.

A job stores the stream indices to remove, chosen from the Scanner's track
list. When the file changed in between — Sonarr/Radarr importing an upgrade,
a re-mux — the same indices point at other tracks, and the cleanup removed
those instead (the original-language audio, say).
"""
import json
import shutil

import aiosqlite
import pytest

from backend.queue import JobQueue, QueueWorker
from backend.tests.test_same_name_conversion import _mkv

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


async def _scan_row(db_path, path, langs):
    tracks = [{"stream_index": i + 1, "language": lang, "codec": "aac", "channels": 2, "keep": True}
              for i, lang in enumerate(langs)]
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, video_codec, audio_tracks_json, native_language, "
            "scan_timestamp) VALUES (?, 1, 'mpeg4', ?, 'ger', '2026-10-08T00:00:00')",
            (str(path), json.dumps(tracks)))
        await db.commit()


async def _run_audio_job(db_path, path, remove, monkeypatch):
    import backend.audio as audio
    calls = []

    async def remux(*args, **kwargs):
        calls.append(kwargs)
        return {"success": True, "output_path": str(path), "space_saved": 0, "error": None}

    monkeypatch.setattr(audio, "remux_audio", remux)
    queue = JobQueue(db_path)
    job_id = await queue.add_job(str(path), "audio", audio_tracks_to_remove=remove)
    job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)
    await QueueWorker(db_path)._process_job(job)
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT status, error_key FROM jobs WHERE id = ?", (job_id,)) as cur:
            status, error_key = await cur.fetchone()
    return status, error_key, calls


@pytest.mark.asyncio
async def test_cleanup_refuses_when_the_tracks_changed(test_db, tmp_path, monkeypatch):
    path = tmp_path / "Movie (2009).mkv"
    await _scan_row(test_db, path, ["ger", "rus"])   # scanned: remove Russian (stream 2)
    _mkv(path, ["eng", "ger"])                        # now: an upgrade with German at stream 2
    status, error_key, calls = await _run_audio_job(test_db, path, [2], monkeypatch)
    assert (status, error_key) == ("failed", "errors.tracksChanged")
    assert calls == []


@pytest.mark.asyncio
async def test_cleanup_runs_when_the_tracks_are_unchanged(test_db, tmp_path, monkeypatch):
    path = tmp_path / "Movie (2009).mkv"
    await _scan_row(test_db, path, ["ger", "rus"])
    _mkv(path, ["ger", "rus"])
    status, error_key, calls = await _run_audio_job(test_db, path, [2], monkeypatch)
    assert error_key != "errors.tracksChanged"
    assert len(calls) == 1
