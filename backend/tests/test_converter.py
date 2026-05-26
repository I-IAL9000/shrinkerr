import pytest
from backend.converter import (
    build_ffmpeg_cmd,
    _build_ffmpeg_cmd_impl,
    rename_x264_to_x265,
    get_output_path,
    get_temp_path,
    parse_ffmpeg_progress,
)


def _last_input_index(cmd: list) -> int:
    """Index of the last `-i` flag in an ffmpeg argv."""
    return max(i for i, a in enumerate(cmd) if a == "-i")


def test_build_nvenc_command():
    cmd = build_ffmpeg_cmd("/media/movie.mkv", "/media/movie.converting.mkv", encoder="nvenc", cq=20)
    assert "hevc_nvenc" in cmd
    assert cmd[cmd.index("-preset") + 1] == "p6"
    assert cmd[cmd.index("-cq") + 1] == "20"
    assert "main10" in cmd
    assert cmd[-1] == "/media/movie.converting.mkv"


# ---------------------------------------------------------------------------
# v0.7.14: NVDEC-native vs software-decode command structure.
#
# Guards two regression classes:
#   - v0.7.12: -noautoscale placed BEFORE -i (an output option in the input
#     section) → ffmpeg exit 234 "Error parsing options for input file".
#   - The software-decode fallback must NOT emit scale_cuda (the filter that
#     crashes mid-stream on NVDEC partial-fallback).
# ---------------------------------------------------------------------------

def test_nvenc_cuda_native_command_structure():
    """NVDEC-native CUDA path: scale_cuda present, and -noautoscale is an
    OUTPUT option (after the last -i), not an input option."""
    cmd = _build_ffmpeg_cmd_impl(
        "/media/movie.mkv", "/media/movie.converting.mkv",
        encoder="nvenc",
        use_hw_decode=True,
        hw_decode_backend="cuda",
        hw_decode_keeps_on_device=True,
        nvenc_bit_depth="8bit",
    )
    # scale_cuda is the on-GPU format/scale filter.
    assert any("scale_cuda" in a for a in cmd), f"scale_cuda missing: {cmd}"
    # -noautoscale must appear AFTER the last input (output-option position).
    assert "-noautoscale" in cmd, f"-noautoscale missing: {cmd}"
    assert cmd.index("-noautoscale") > _last_input_index(cmd), (
        f"-noautoscale is before the last -i (would be parsed as an input "
        f"option → exit 234): {cmd}"
    )


def test_nvenc_software_decode_no_scale_cuda():
    """Software-decode fallback (use_hw_decode=False): no scale_cuda, no
    -noautoscale, but still the NVENC encoder. This is the path the v0.7.14
    retry rebuilds to dodge the NVDEC mid-stream reconfig crash."""
    cmd = _build_ffmpeg_cmd_impl(
        "/media/movie.mkv", "/media/movie.converting.mkv",
        encoder="nvenc",
        use_hw_decode=False,
        hw_decode_backend="cuda",
        hw_decode_keeps_on_device=False,
        nvenc_bit_depth="8bit",
    )
    assert not any("scale_cuda" in a for a in cmd), f"scale_cuda leaked into software path: {cmd}"
    assert "-noautoscale" not in cmd, f"-noautoscale should be CUDA-native only: {cmd}"
    assert "hevc_nvenc" in cmd, f"NVENC encoder missing: {cmd}"


def test_build_libx265_command():
    cmd = build_ffmpeg_cmd("/media/movie.mkv", "/media/movie.converting.mkv", encoder="libx265", crf=20)
    assert "libx265" in cmd
    assert cmd[cmd.index("-crf") + 1] == "20"


def test_rename_x264_to_x265():
    assert rename_x264_to_x265("Movie (2020) 1080p Bluray DTS 5.1 x264-GRP.mkv") == "Movie (2020) 1080p Bluray DTS 5.1 x265-GRP.mkv"
    assert rename_x264_to_x265("Movie h264-GRP.mkv") == "Movie x265-GRP.mkv"
    assert rename_x264_to_x265("Movie.x264.mkv") == "Movie.x265.mkv"


def test_rename_preserves_x265():
    assert rename_x264_to_x265("Movie x265-GRP.mkv") == "Movie x265-GRP.mkv"


def test_rename_no_codec_in_name():
    assert rename_x264_to_x265("Movie (2020) 1080p.mkv") == "Movie (2020) 1080p.mkv"


def test_output_always_mkv():
    assert get_output_path("/media/movie.mp4").endswith(".mkv")
    assert get_output_path("/media/movie.avi").endswith(".mkv")
    assert get_output_path("/media/movie.mkv").endswith(".mkv")


def test_get_temp_path():
    temp = get_temp_path("/media/movie.mkv")
    assert temp == "/media/movie.converting.mkv"


def test_get_temp_path_non_mkv():
    temp = get_temp_path("/media/show.mp4")
    assert temp == "/media/show.converting.mkv"


def test_parse_ffmpeg_progress_basic():
    line = "frame= 1234 fps= 45 q=28.0 size=   10240kB time=00:01:30.50 bitrate=..."
    result = parse_ffmpeg_progress(line, duration=300.0)
    assert result is not None
    assert abs(result["progress"] - 30.17) < 0.1
    assert result["fps"] == 45.0
    # Without start_time, ETA is None (no wall-clock reference)
    assert result["eta_seconds"] is None


def test_parse_ffmpeg_progress_with_start_time():
    import time
    line = "frame= 1234 fps= 45 q=28.0 size=   10240kB time=00:01:30.50 bitrate=..."
    # Simulate encoding started 30s ago (real-time), 30% through a 300s video
    start_time = time.monotonic() - 30.0
    result = parse_ffmpeg_progress(line, duration=300.0, start_time=start_time)
    assert result is not None
    assert abs(result["progress"] - 30.17) < 0.1
    assert result["eta_seconds"] is not None
    # ~30s elapsed for ~30% done → ~70s remaining (approximate)
    assert 50 < result["eta_seconds"] < 90


def test_parse_ffmpeg_progress_no_time():
    line = "ffmpeg version 6.0"
    result = parse_ffmpeg_progress(line, duration=300.0)
    assert result is None


def test_parse_ffmpeg_progress_zero_duration():
    line = "frame=  100 fps= 30 time=00:00:10.00 bitrate=..."
    result = parse_ffmpeg_progress(line, duration=0)
    assert result is not None
    assert result["progress"] == 0.0


def test_parse_ffmpeg_progress_at_end():
    import time
    line = "frame= 9000 fps= 30 time=00:05:00.00 bitrate=..."
    start_time = time.monotonic() - 120.0  # 2min wall-clock for 5min video
    result = parse_ffmpeg_progress(line, duration=300.0, start_time=start_time)
    assert result is not None
    assert result["progress"] == 100.0
    assert result["eta_seconds"] == 0
