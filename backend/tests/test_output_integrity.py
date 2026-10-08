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


def _source_runs(seconds, monkeypatch, src):
    """Make `src` report a longer video stream than it has, so the encode
    stops early as it does when the read dies mid-stream."""
    import backend.converter as converter
    real = converter._probe_video_duration

    async def probe(path, **kwargs):
        return seconds if path == str(src) else await real(path, **kwargs)

    monkeypatch.setattr(converter, "_probe_video_duration", probe)


@pytest.mark.asyncio
@needs_libx265
async def test_output_shorter_than_the_source_is_rejected(test_db, tmp_path, monkeypatch):
    src = tmp_path / "Show - S01E01 - 1080p WEB h264.mkv"
    _clip(src, seconds=3)
    before = src.read_bytes()
    _source_runs(10.0, monkeypatch, src)
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


# --- what counts as truncated (v0.10.0) -------------------------------------

from backend.converter import _truncation_failure  # noqa: E402


@pytest.mark.asyncio
async def test_read_error_in_the_log_rejects_the_output(tmp_path):
    log = ["Press [q] to stop", "[in#0/matroska @ 0x1] Error during demuxing: Input/output error"]
    got = await _truncation_failure(str(tmp_path / "a.mkv"), str(tmp_path / "b.mkv"), 100.0, None, log)
    assert got["error_key"] == "errors.sourceReadFailed"


@pytest.mark.asyncio
async def test_dropping_a_track_that_runs_past_the_video_is_not_truncation(tmp_path):
    """The container lasts as long as its longest stream: removing a 10 s
    audio track from a 3 s video made the output look truncated."""
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    src, out = tmp_path / "src.mkv", tmp_path / "out.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=3",
                    "-f", "lavfi", "-i", "sine=duration=10", "-c:v", "mpeg4", "-c:a", "aac", str(src)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-map", "0:v", "-c", "copy", str(out)], check=True)
    assert await _truncation_failure(str(src), str(out), 10.0, None, []) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("name, disc", [("Recording.ts", None), ("Movie.m2ts", None), ("VIDEO_TS.IFO", "dvd")])
@pytest.mark.parametrize("output_seconds, complete", [(80.0, True), (50.0, False)])
async def test_estimated_durations_get_slack(tmp_path, monkeypatch, name, disc, output_seconds, complete):
    """MPEG-TS/PS and DVD lengths are estimates: a small shortfall is noise,
    half the film missing is not."""
    import backend.converter as converter

    async def output_length(path):
        return output_seconds

    monkeypatch.setattr(converter, "_probe_output_duration", output_length)
    got = await _truncation_failure(str(tmp_path / name), str(tmp_path / "out.mkv"), 100.0, disc, [])
    assert (got is None) is complete


@pytest.mark.asyncio
async def test_a_copied_mkvmerge_duration_tag_doesnt_hide_truncation(tmp_path):
    """mkvmerge tags streams "DURATION-eng"; ffmpeg copies that into its
    output next to its own DURATION, so a 25 s output claimed 60 s."""
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    src, out = tmp_path / "src.mkv", tmp_path / "out.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25:duration=60",
                    "-c:v", "mpeg4", "-metadata:s:v:0", "DURATION-eng=00:01:00.000000000", str(src)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-t", "25", "-c", "copy", str(out)], check=True)
    got = await _truncation_failure(str(src), str(out), 60.0, None, [])
    assert got and got["error_key"] == "errors.outputTruncated"


@pytest.mark.asyncio
async def test_a_hardware_decoder_error_is_not_a_read_error(tmp_path, monkeypatch):
    import backend.converter as converter

    async def length(path, *args, **kwargs):
        return 100.0

    monkeypatch.setattr(converter, "_probe_video_duration", length)
    monkeypatch.setattr(converter, "_probe_output_duration", length)
    log = ["[vist#0:0/hevc @ 0x1] Decoding error: Input/output error"]
    assert await _truncation_failure(str(tmp_path / "a.mkv"), str(tmp_path / "b.mkv"), 100.0, None, log) is None


@pytest.mark.asyncio
async def test_an_output_whose_length_cant_be_read_is_not_trusted(tmp_path, monkeypatch):
    import backend.converter as converter

    async def no_wait(_):
        return None

    monkeypatch.setattr(converter.asyncio, "sleep", no_wait)
    got = await _truncation_failure(str(tmp_path / "gone.mkv"), str(tmp_path / "out.mkv"), 100.0, None, [])
    assert got["error_key"] == "errors.outputUnverifiable"


@pytest.mark.asyncio
async def test_remux_that_stops_early_keeps_the_original(test_db, tmp_path, monkeypatch):
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    from backend.audio import remux_audio
    from backend.tests.test_remux_guards import _clip as _two_audio_clip
    src = _two_audio_clip(tmp_path)
    before = src.read_bytes()
    _source_runs(10.0, monkeypatch, src)
    result = await remux_audio(str(src), [1], duration=10.0)
    assert result["success"] is False
    assert result["error_key"] == "errors.outputTruncated"
    assert src.read_bytes() == before
    assert not list(tmp_path.glob("*.remuxing.mkv"))
