"""v0.9.156: an audio cleanup removed EVERY audio track (reported: a German
film's queued job said "Removing 3 audio tracks" and produced a silent file),
and Cancel couldn't stop it — remux_audio never handed its ffmpeg process to
the queue, so there was nothing to kill.
"""
import shutil
import subprocess

import pytest

from backend.audio import remux_audio

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _clip(tmp_path):
    src = tmp_path / "Film (2009) 1080p Bluray AC3 5.1 h265.mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=3",
         "-f", "lavfi", "-i", "sine=duration=3", "-f", "lavfi", "-i", "sine=frequency=600:duration=3",
         "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
         "-metadata:s:a:0", "language=ger", "-metadata:s:a:1", "language=rus", str(src)],
        check=True,
    )
    return src


@pytest.mark.asyncio
async def test_remux_refuses_to_remove_every_audio_track(test_db, tmp_path):
    src = _clip(tmp_path)
    before = src.read_bytes()

    result = await remux_audio(str(src), [], duration=3.0)

    assert result["success"] is False
    assert result.get("error_key") == "errors.wouldRemoveAllAudio"
    assert src.read_bytes() == before  # original untouched
    assert not list(tmp_path.glob("*.remuxing.mkv"))


@pytest.mark.asyncio
async def test_remux_reports_its_ffmpeg_process(test_db, tmp_path):
    src = _clip(tmp_path)
    procs = []

    result = await remux_audio(str(src), [1], duration=3.0, proc_callback=procs.append)

    assert result["success"] is True
    assert len(procs) == 1 and procs[0].returncode is not None


@pytest.mark.asyncio
async def test_remux_allows_subtitle_cleanup_on_a_file_without_audio(test_db, tmp_path):
    src = tmp_path / "Clip.mkv"
    sub = tmp_path / "s.srt"
    sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=2",
                    "-i", str(sub), "-map", "0", "-map", "1", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:s", "srt", str(src)], check=True)

    result = await remux_audio(str(src), [], duration=2.0, keep_subtitle_indices=[])

    assert result["success"] is True
