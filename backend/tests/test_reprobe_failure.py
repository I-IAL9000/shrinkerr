"""v0.10.0: when the file can't be re-probed after a conversion or an audio
cleanup, its scan row kept the source's track list. Those stream indexes
are wrong for the new file, and a cleanup queued from them removed the
wrong — or the only — audio track. The tracks are now cleared until the
next scan probes the file again.
"""
import json

import aiosqlite
import pytest

from backend.queue import JobQueue, refresh_converted_scan_row
from backend.tests.test_same_name_conversion import _scan_row, _track


@pytest.fixture
def probe_fails(monkeypatch):
    import backend.queue as queue_mod
    import backend.scanner as scanner

    async def nothing(*args, **kwargs):
        return None

    async def no_wait(_):
        return None

    monkeypatch.setattr(scanner, "probe_file", nothing)
    monkeypatch.setattr(queue_mod.asyncio, "sleep", no_wait)


async def _row(db_path, path):
    stale = [_track(1, "fre", False), _track(2, "eng", True)]
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, audio_tracks_json, "
            "subtitle_tracks_json, scan_timestamp, has_removable_tracks_flag, has_removable_subs_flag) "
            "VALUES (?, 1, 'h264', 1, ?, ?, '2026-10-08T00:00:00', 1, 1)",
            (str(path), json.dumps(stale), json.dumps([{"stream_index": 3, "language": "fre", "keep": False}])),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_conversion_clears_tracks_it_could_not_reprobe(test_db, tmp_path, probe_fails):
    src = tmp_path / "Movie (2009).mkv"
    src.write_bytes(b"converted")
    await _row(test_db, src)
    job_id = await JobQueue(test_db).add_job(str(src), "convert", encoder="libx265")

    await refresh_converted_scan_row(test_db, job_id, str(src), str(src))

    row = await _scan_row(test_db, src)
    assert json.loads(row["audio_tracks_json"]) == [] and json.loads(row["subtitle_tracks_json"]) == []
    assert row["has_removable_tracks_flag"] == 0 and row["has_removable_subs_flag"] == 0
    assert row["video_codec"] == "hevc"


@pytest.mark.asyncio
async def test_audio_cleanup_clears_tracks_it_could_not_reprobe(test_db, tmp_path, monkeypatch):
    import shutil
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    import backend.audio as audio
    import backend.queue as queue_mod
    import backend.scanner as scanner
    from backend.queue import QueueWorker
    from backend.tests.test_same_name_conversion import _mkv
    src = tmp_path / "Movie (2009).mkv"
    _mkv(src, ["fre", "eng"])
    await _row(test_db, src)
    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(src), "audio", audio_tracks_to_remove=[1])
    real_probe, remux_done = scanner.probe_file, []

    async def probe(*args, **kwargs):
        return None if remux_done else await real_probe(*args, **kwargs)

    async def remuxed(*args, **kwargs):
        remux_done.append(True)
        return {"success": True, "output_path": str(src), "space_saved": 10, "error": None}

    async def no_wait(_):
        return None

    monkeypatch.setattr(scanner, "probe_file", probe)
    monkeypatch.setattr(queue_mod.asyncio, "sleep", no_wait)
    monkeypatch.setattr(audio, "remux_audio", remuxed)
    job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)
    await QueueWorker(test_db)._process_job(job)

    row = await _scan_row(test_db, src)
    assert json.loads(row["audio_tracks_json"]) == [] and json.loads(row["subtitle_tracks_json"]) == []
    assert row["has_removable_tracks_flag"] == 0 and row["has_removable_subs_flag"] == 0
