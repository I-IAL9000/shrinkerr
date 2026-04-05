import pytest
from backend.converter import (
    build_ffmpeg_cmd,
    rename_x264_to_x265,
    get_output_path,
    get_temp_path,
    parse_ffmpeg_progress,
)


def test_build_nvenc_command():
    cmd = build_ffmpeg_cmd("/media/movie.mkv", "/media/movie.converting.mkv", encoder="nvenc", cq=20)
    assert "hevc_nvenc" in cmd
    assert cmd[cmd.index("-preset") + 1] == "p6"
    assert cmd[cmd.index("-cq") + 1] == "20"
    assert "main10" in cmd
    assert cmd[-1] == "/media/movie.converting.mkv"


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
