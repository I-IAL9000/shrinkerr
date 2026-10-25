"""VMAF compares each frame with the same frame of the encode (v0.10.0).

The encoder puts its frames on the frame-rate grid. Dragons' Den S21E01
(1080i HDTV, cut at a keyframe 1.112 s in by a clean-up before b057365)
was encoded with its frames at 1.120 s + n/25, the source's at
1.112 s + n/25, and the fps filter before libvmaf rounded some sample
windows a whole frame apart: the whole window compared each frame with its
neighbour — VMAF 43 and 59 on encodes that look fine (13 of 15 episodes
scored right; it depends on where the window starts)."""
import json
import shutil
import subprocess

import pytest

from backend.converter import _vmaf_grid_shift, _vmaf_pipelines


def test_the_encode_is_moved_by_the_fraction_of_a_frame():
    assert _vmaf_grid_shift(1.112, 1.120, 25) == pytest.approx(0.008)
    assert _vmaf_grid_shift(1.112, 1.104, 25) == pytest.approx(-0.008)
    assert _vmaf_grid_shift(1.112, 1.192, 25) == 0.0  # two whole frames: frames it doesn't have
    assert _vmaf_grid_shift(0.0, 0.0, 25) == _vmaf_grid_shift(None, 1.12, 25) == 0.0


def test_the_pipelines():
    ref, dist, fps = _vmaf_pipelines({"fps": 25.0, "start_time": 1.112}, {"fps": 25.0, "start_time": 1.120})
    assert fps == 25.0 and "setpts" not in ref
    assert dist.index("setpts=PTS-0.008000/TB") < dist.index("fps=fps=25.000000")
    _, dist, _ = _vmaf_pipelines({"fps": 25.0, "start_time": 1.112}, {"fps": 25.0, "start_time": 1.104})
    assert "setpts=PTS+0.008000/TB" in dist
    ref, dist, _ = _vmaf_pipelines({"fps": 25.0, "start_time": 0.0}, {"fps": 25.0, "start_time": 0.0})
    assert ref == dist == "scale=in_range=auto:out_range=tv:flags=bicubic,fps=fps=25.000000,format=yuv420p"


def _has_libvmaf() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return " libvmaf " in out


@pytest.mark.skipif(not _has_libvmaf(), reason="needs ffmpeg with libvmaf")
@pytest.mark.asyncio
async def test_a_window_that_rounded_a_frame_apart(tmp_path):
    from backend.converter import _probe_vmaf_stream, _run_libvmaf_pass
    clip, source, encoded = tmp_path / "clip.mkv", tmp_path / "source.mkv", tmp_path / "encoded.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=20",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "10", str(clip)], check=True)
    # the video starts 1.112 s in, the audio at 0 (a recording cut at a keyframe)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=22", "-itsoffset", "1.112",
                    "-i", str(clip), "-map", "1:v", "-map", "0:a", "-c:v", "copy", "-c:a", "aac", str(source)],
                   check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-map", "0:0", "-map", "0:a?",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-c:a", "copy", str(encoded)],
                   check=True)
    src, dst = await _probe_vmaf_stream(str(source)), await _probe_vmaf_stream(str(encoded))
    assert (src["start_time"], dst["start_time"]) == (pytest.approx(1.112), pytest.approx(1.12))

    async def score(ref_pipeline, dist_pipeline, name):
        result = await _run_libvmaf_pass(
            input_path=str(source), temp_path=str(encoded), seek=10.020, duration=5.0,
            ref_pipeline=ref_pipeline, dist_pipeline=dist_pipeline, json_path=tmp_path / name,
            fps_for_progress=25.0, progress_callback=None, step_label="")
        return result["score"]

    unshifted = "scale=in_range=auto:out_range=tv:flags=bicubic,fps=fps=25.000000,format=yuv420p"
    assert await score(unshifted, unshifted, "before.json") < 70  # a frame apart, as before
    ref, dist, _ = _vmaf_pipelines(src, dst)
    assert await score(ref, dist, "after.json") > 90
    assert json.loads((tmp_path / "after.json").read_text())["pooled_metrics"]["vmaf"]["min"] > 80
