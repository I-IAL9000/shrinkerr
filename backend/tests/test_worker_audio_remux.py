"""v0.9.155: remote workers passed the REMOVE lists to remux_audio, whose
parameters are KEEP lists — `remux_audio(path, audio_remove, sub_remove)` kept
exactly the audio tracks the user wanted gone (dropping e.g. the native
track) and passed the subtitle list as `duration`. A failed remux was also
reported as a successful job.
"""
import pytest

import backend.audio
import backend.scanner
from backend import worker_mode


class FakeClient:
    def __init__(self):
        self.completed = []

    async def report_progress(self, *a, **kw):
        return False

    async def report_complete(self, node_id, job_id, success, **kw):
        self.completed.append((success, kw))


PROBE = {
    "duration": 100.0, "file_size": 1000, "video_codec": "hevc",
    "audio_tracks": [{"stream_index": 1, "language": "eng"},
                     {"stream_index": 2, "language": "spa"},
                     {"stream_index": 3, "language": "jpn"}],
    "subtitle_tracks": [{"stream_index": 4, "language": "eng"},
                        {"stream_index": 5, "language": "spa"}],
}


def _job(path):
    return {"id": 7, "file_path": str(path), "job_type": "audio",
            "audio_tracks_to_remove": "[2]", "subtitle_tracks_to_remove": "[5]"}


@pytest.mark.asyncio
async def test_worker_audio_job_passes_keep_lists(tmp_path, monkeypatch):
    src = tmp_path / "Show - S01E01.mkv"
    src.write_bytes(b"x")
    calls = []

    async def fake_probe(path, *a, **kw):
        return dict(PROBE)

    async def fake_remux(input_path, keep_audio_indices, duration=0, progress_callback=None,
                         keep_subtitle_indices=None, audio_languages=None, **kw):
        calls.append((keep_audio_indices, duration, keep_subtitle_indices))
        return {"success": True, "output_path": input_path, "space_saved": 10}

    monkeypatch.setattr(backend.scanner, "probe_file", fake_probe)
    monkeypatch.setattr(backend.audio, "remux_audio", fake_remux)
    client = FakeClient()

    await worker_mode.execute_job(client, "node-1", _job(src), ["libx265"])

    assert calls == [([1, 3], 100.0, [4])]
    assert client.completed and client.completed[-1][0] is True


@pytest.mark.asyncio
async def test_worker_reports_failed_remux_as_failure(tmp_path, monkeypatch):
    src = tmp_path / "Show - S01E01.mkv"
    src.write_bytes(b"x")

    async def fake_probe(path, *a, **kw):
        return dict(PROBE)

    async def failing_remux(*a, **kw):
        return {"success": False, "error": "ffmpeg exited with code 1"}

    monkeypatch.setattr(backend.scanner, "probe_file", fake_probe)
    monkeypatch.setattr(backend.audio, "remux_audio", failing_remux)
    client = FakeClient()

    await worker_mode.execute_job(client, "node-1", _job(src), ["libx265"])

    assert client.completed[-1][0] is False
    assert "ffmpeg exited" in (client.completed[-1][1].get("error") or "")
