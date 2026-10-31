"""The after-conversion VMAF on a disc (v0.10.0): frames by number.

The check seeks a third of the way into the reference and the encode and
compares 30 s. A disc's reference is its main title as the encode read it
(concat: over a DVD's VOBs, bluray: over a Blu-ray's playlist), whose
timestamps restart in every VOB or start far from zero: Showgirls' started
at 4200 s, the seek landed on another scene, and a fine encode scored 1.8
(its samples had scored 92). A disc's window is now read from the start of
both and compared by frame number, ten minutes in at most.
"""
import shutil
import subprocess

import pytest

from backend.converter import _probe_vmaf_stream, _run_libvmaf_pass, _vmaf_pipelines, vmaf_window


def _has_libvmaf() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return "libvmaf" in out


def test_a_files_window_is_a_seek_and_a_discs_is_by_frame_number():
    seek = vmaf_window("ref.mkv", "out.mkv", 120.0, 30.0, None)
    assert seek["inputs"] == ["-ss", "120.000", "-i", "ref.mkv", "-ss", "120.000", "-i", "out.mkv"]
    assert seek["limit"] == ["-t", "30.000"]  # before each output: the cross-check has two
    assert seek["ref"] == "[0:V]" and seek["frames"] is None and not seek["outputs"]

    disc = vmaf_window("bluray:/m/f.iso", "out.mkv", 600.0, 30.0, (14386, 719), ["-probesize", "200M"], rate=23.976)
    assert disc["inputs"] == ["-probesize", "200M", "-i", "bluray:/m/f.iso", "-i", "out.mkv"]  # no seek
    trim = "trim=start_frame=14386:end_frame=15105,setpts=N/(23.976000*TB),"  # timed by number
    assert disc["ref"] == "[ref_in]" + trim and disc["dist"] == "[1:v]" + trim
    assert disc["frames"] == 15105 and disc["limit"] == []  # the progress counts the frames up to its end


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_libvmaf(), reason="needs ffmpeg with libvmaf")
async def test_a_dvds_window_is_measured_where_a_seek_finds_nothing(tmp_path):
    """Three VOBs whose timestamps each start at 0: a seek 70 s into the
    title reads nothing; by frame number, the encode scores as it should."""
    vobs = []
    for n, colour in enumerate(("red", "green", "blue"), start=1):
        vob = tmp_path / f"VTS_01_{n}.VOB"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=40",
                        "-vf", f"drawbox=x=0:y=0:w=iw/4:h=ih/4:color={colour}@1:t=fill", "-c:v", "mpeg2video",
                        "-b:v", "3M", "-g", "12", "-f", "vob", str(vob)], check=True)
        vobs.append(str(vob))
    title = "concat:" + "|".join(vobs)
    encode = tmp_path / "out.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", title, "-map", "0:V:0", "-c:v", "libx264", "-crf", "16",
                    "-preset", "ultrafast", str(encode)], check=True)
    src, dst = await _probe_vmaf_stream(title), await _probe_vmaf_stream(str(encode))
    ref_pipeline, dist_pipeline, fps = _vmaf_pipelines({**src, "start_time": None}, dst)
    common = dict(input_path=title, temp_path=str(encode), seek=70.0, duration=10.0, ref_pipeline=ref_pipeline,
                  dist_pipeline=dist_pipeline, fps_for_progress=fps, step_label="VMAF analysis")

    by_time = await _run_libvmaf_pass(**common, json_path=str(tmp_path / "t.json"), progress_callback=None)
    assert by_time["score"] is None or by_time["score"] < 80

    said = []

    async def progress(**kw):
        said.append(kw["progress"])
    by_frame = await _run_libvmaf_pass(**common, json_path=str(tmp_path / "f.json"), progress_callback=progress,
                                       frames=(70 * 25, 250), ref_args=["-analyzeduration", "200M", "-probesize", "200M"])
    assert by_frame["error"] is None and by_frame["score"] >= 95
    assert said[0] == 0 and said[-1] >= 90  # counted up to the window's end
