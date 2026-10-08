"""v0.10.0 (C2): a conversion that keeps the file's name left its scan row
describing the source's streams.

Sonarr/Radarr naming has no codec tag, so the output keeps the source's name.
The post-conversion refresh (re-probe, re-sort tracks, recompute flags) only
ran when the name changed: the row kept the old track list, and an audio
cleanup queued from it later removed stream 1, by then the only audio track.
"""
import json
import shutil
import subprocess

import aiosqlite
import pytest

from backend.queue import JobQueue, QueueWorker

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _mkv(path, langs):
    cmd = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=2"]
    for _ in langs:
        cmd += ["-f", "lavfi", "-i", "sine=duration=2"]
    cmd += ["-map", "0:v"]
    for i in range(len(langs)):
        cmd += ["-map", f"{i + 1}:a"]
    cmd += ["-c:v", "mpeg4", "-c:a", "aac"]
    for i, lang in enumerate(langs):
        cmd += [f"-metadata:s:a:{i}", f"language={lang}"]
    subprocess.run(cmd + [str(path)], check=True)


def _track(index, lang, keep):
    return {"stream_index": index, "language": lang, "codec": "ac3", "channels": 6, "keep": keep}


async def _scan_row(db_path, path):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM scan_results WHERE file_path = ?", (str(path),)) as cur:
            return await cur.fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["converted", "vmaf_rejected"])
async def test_conversion_refreshes_track_data_even_when_the_name_is_kept(test_db, tmp_path, monkeypatch, outcome):
    src = tmp_path / "Movie (2009).mkv"
    # On disk after the job: the French track was removed, English is now stream 1.
    _mkv(src, ["eng"])
    before = [_track(1, "fre", False), _track(2, "eng", True)]
    async with aiosqlite.connect(test_db) as db:
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, "
            "audio_tracks_json, native_language, scan_timestamp, has_removable_tracks_flag) "
            "VALUES (?, ?, 'h264', 1, ?, 'eng', '2026-10-08T00:00:00', 1)",
            (str(src), 10_000_000, json.dumps(before)),
        )
        await db.commit()

    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(src), "combined", encoder="libx265", audio_tracks_to_remove=[1])
    result = {"success": True, "output_path": str(src), "space_saved": 1000, "error": None}
    if outcome == "vmaf_rejected":
        result.update({"vmaf_rejected": True, "space_saved": 0})

    import backend.converter as converter

    async def fake_convert_file(**kwargs):
        return result

    monkeypatch.setattr(converter, "convert_file", fake_convert_file)
    worker = QueueWorker(test_db)
    job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)
    await worker._process_job(job)

    row = await _scan_row(test_db, src)
    tracks = json.loads(row["audio_tracks_json"])
    if outcome == "converted":
        assert [(t["stream_index"], t["language"], t["keep"]) for t in tracks] == [(1, "eng", True)]
        assert row["has_removable_tracks_flag"] == 0
        assert row["video_codec"] == "hevc"
    else:
        # The encode was thrown away and the original kept: the row must
        # still describe the original.
        assert tracks == before
        assert row["video_codec"] == "h264"


async def _enable_audio_cleanup(db_path):
    from backend.scanner import invalidate_sub_settings_cache
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('audio_cleanup_enabled', 'true')")
        await db.commit()
    invalidate_sub_settings_cache()


async def _insert_row(db_path, path, tracks, native, source):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, "
            "audio_tracks_json, native_language, language_source, scan_timestamp, has_removable_tracks_flag) "
            "VALUES (?, ?, 'h264', 1, ?, ?, ?, '2026-10-08T00:00:00', 1)",
            (str(path), 10_000_000, json.dumps(tracks), native, source),
        )
        await db.commit()


async def _run(db_path, path, job_type, monkeypatch, remove):
    import backend.audio as audio
    import backend.converter as converter
    done = {"success": True, "output_path": str(path), "space_saved": 1000, "error": None}

    async def fake(*args, **kwargs):
        return done

    monkeypatch.setattr(converter, "convert_file", fake)
    monkeypatch.setattr(audio, "remux_audio", fake)
    queue = JobQueue(db_path)
    job_id = await queue.add_job(str(path), job_type, encoder="libx265", audio_tracks_to_remove=remove)
    job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)
    await QueueWorker(db_path)._process_job(job)
    row = await _scan_row(db_path, path)
    return row, {t["stream_index"]: (t["language"], t["keep"]) for t in json.loads(row["audio_tracks_json"])}


@pytest.mark.asyncio
async def test_stored_guess_is_not_reused_after_the_tracks_changed(test_db, tmp_path, monkeypatch):
    """[rus, ger, ger] with the guess "rus" stored: once Russian is removed,
    sorting against "rus" would mark both German tracks for removal."""
    await _enable_audio_cleanup(test_db)
    src = tmp_path / "Movie (2009).mkv"
    _mkv(src, ["ger", "ger"])  # the output
    await _insert_row(test_db, src, [_track(1, "rus", True), _track(2, "ger", False), _track(3, "ger", False)],
                      "rus", "heuristic")
    row, tracks = await _run(test_db, src, "combined", monkeypatch, remove=[1])
    assert tracks == {1: ("ger", True), 2: ("ger", True)}
    assert row["has_removable_tracks_flag"] == 0


@pytest.mark.asyncio
async def test_audio_cleanup_sorts_against_the_known_native(test_db, tmp_path, monkeypatch):
    """German film (native from TMDB): after removing Russian the remux has
    English first, and guessing from track order marked German for removal."""
    await _enable_audio_cleanup(test_db)
    src = tmp_path / "Movie (2009).mkv"
    _mkv(src, ["eng", "ger"])  # the output
    await _insert_row(test_db, src, [_track(1, "rus", False), _track(2, "eng", False), _track(3, "ger", True)],
                      "ger", "api")
    row, tracks = await _run(test_db, src, "audio", monkeypatch, remove=[1])
    assert tracks[2] == ("ger", True)
    assert tracks[1] == ("eng", False)
