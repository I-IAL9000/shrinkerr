"""M7 (v0.10.0): a conversion job probed its source twice (the job, then the
converter) and its output twice (the scan-row refresh, then the audio-codec
rename) — each a cold ffprobe over the NAS mount."""
import shutil
from collections import Counter

import aiosqlite
import pytest

from backend.queue import JobQueue, QueueWorker

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _counting_probe(monkeypatch):
    import backend.scanner as scanner
    real, calls = scanner.probe_file, Counter()

    async def probe(path, *args, **kwargs):
        calls[str(path)] += 1
        return await real(path, *args, **kwargs)
    monkeypatch.setattr(scanner, "probe_file", probe)
    return real, calls


@pytest.mark.asyncio
async def test_the_converter_uses_the_callers_probe(test_db, tmp_path, monkeypatch):
    from backend.converter import convert_file
    from backend.tests.test_same_name_conversion import _mkv
    folder = tmp_path / "Film (2020)"
    folder.mkdir()
    clip = folder / "Film (2020).mkv"
    _mkv(clip, ["eng"])
    real, calls = _counting_probe(monkeypatch)

    plan = await convert_file(str(clip), "libx265", 2.0, command_only=True)
    assert calls[str(clip)] == 1
    reused = await convert_file(str(clip), "libx265", 2.0, command_only=True,
                                pre_probe=await real(str(clip)))
    assert calls[str(clip)] == 1  # not probed again
    assert reused["command"] == plan["command"]


@pytest.mark.asyncio
async def test_a_conversion_job_probes_its_source_and_output_once_each(test_db, tmp_path, monkeypatch):
    import backend.converter as converter
    from backend.tests.test_same_name_conversion import _mkv
    src = tmp_path / "Movie (2009).mkv"
    out = tmp_path / "Movie (2009) x265.mkv"
    _mkv(src, ["eng"])
    async with aiosqlite.connect(test_db) as db:
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, "
            "audio_tracks_json, native_language, scan_timestamp) "
            "VALUES (?, 10000000, 'h264', 1, '[]', 'eng', '2026-10-09T00:00:00')", (str(src),))
        await db.commit()
    _real, calls = _counting_probe(monkeypatch)
    handed = {}

    async def fake_convert_file(**kwargs):
        handed["pre_probe"] = kwargs.get("pre_probe")
        _mkv(out, ["eng"])  # the output replaces the source
        src.unlink()
        return {"success": True, "output_path": str(out), "space_saved": 1000, "error": None}

    monkeypatch.setattr(converter, "convert_file", fake_convert_file)
    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(src), "convert", encoder="libx265")
    job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)
    await QueueWorker(test_db)._process_job(job)

    assert handed["pre_probe"] and handed["pre_probe"]["file_size"] > 0
    assert calls[str(src)] == 1
    assert calls[str(out)] == 1
