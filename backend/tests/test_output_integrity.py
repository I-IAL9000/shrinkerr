"""v0.10.0: conversions must never replace an original with less than it had.

H6: when the source read dies mid-stream (a CIFS stall), ffmpeg can exit 0
with a truncated output. The duration was only compared inside the "output
under 5% of the source" branch, so a 40%-length output passed and the
original was disposed.
H4: when the convert-time probe failed (ffprobe timeout on a stalled mount),
the encode went ahead with no stream info: every subtitle was dropped (and a
disc was treated as a plain file), then the original was replaced.
"""
import shutil
import subprocess

import pytest

from backend.converter import convert_file


def _has_libx265() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "libx265" in out


needs_libx265 = pytest.mark.skipif(not _has_libx265(), reason="needs ffmpeg with libx265")


def _clip(path, seconds=3, subtitles=False):
    cmd = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=25:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=duration={seconds}"]
    if subtitles:
        srt = path.with_suffix(".srt")
        srt.write_text("1\n00:00:00,500 --> 00:00:01,500\nHello\n\n")
        cmd += ["-i", str(srt), "-map", "0:v", "-map", "1:a", "-map", "2:s", "-c:s", "srt",
                "-metadata:s:s:0", "language=eng"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path)]
    subprocess.run(cmd, check=True)
    if subtitles:
        path.with_suffix(".srt").unlink()


def _leftovers(folder):
    return sorted(p.name for p in folder.iterdir() if ".converting." in p.name)


@pytest.mark.asyncio
@needs_libx265
async def test_output_shorter_than_the_source_is_rejected(test_db, tmp_path):
    src = tmp_path / "Show - S01E01 - 1080p WEB h264.mkv"
    _clip(src, seconds=3)
    before = src.read_bytes()
    # The source is 10 s long as far as the job knows; the encode stops at 3 s,
    # as when the read dies mid-stream.
    result = await convert_file(str(src), "libx265", 10.0, override_libx265_preset="ultrafast")
    assert result["success"] is False
    assert result["error_key"] == "errors.outputTruncated"
    assert src.read_bytes() == before
    assert _leftovers(tmp_path) == []


@pytest.mark.asyncio
@needs_libx265
async def test_full_length_output_is_still_accepted(test_db, tmp_path):
    src = tmp_path / "Show - S01E01 - 1080p WEB h264.mkv"
    _clip(src, seconds=3)
    result = await convert_file(str(src), "libx265", 3.0, override_libx265_preset="ultrafast")
    assert result["success"], result.get("error")


@pytest.mark.asyncio
async def test_failed_probe_keeps_the_original(test_db, tmp_path, monkeypatch):
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    src = tmp_path / "Movie (2009) 1080p WEB h264.mkv"
    _clip(src, seconds=2, subtitles=True)
    before = src.read_bytes()

    import backend.scanner as scanner

    async def probe_times_out(path, *args, **kwargs):
        return None

    monkeypatch.setattr(scanner, "probe_file", probe_times_out)
    result = await convert_file(str(src), "libx265", 2.0)
    assert result["success"] is False
    assert result["error_key"] == "errors.probeFailed"
    assert src.read_bytes() == before
    assert _leftovers(tmp_path) == []
