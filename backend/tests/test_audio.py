import pytest
from backend.audio import build_remux_cmd


def test_build_remux_keeps_specified_streams():
    cmd = build_remux_cmd("/media/movie.mkv", "/media/movie.remuxing.mkv", keep_audio_indices=[1, 3])
    assert "0:v?" in cmd
    assert "0:s?" in cmd
    assert "0:t?" in cmd
    assert "0:1" in cmd
    assert "0:3" in cmd
    assert "0:2" not in cmd
    assert cmd[cmd.index("-c") + 1] == "copy"


def test_remux_output_is_mkv():
    cmd = build_remux_cmd("/media/movie.mp4", "/media/movie.remuxing.mkv", [1])
    assert cmd[-1].endswith(".mkv")


def test_build_remux_single_audio_stream():
    cmd = build_remux_cmd("/media/film.mkv", "/media/film.remuxing.mkv", keep_audio_indices=[2])
    assert "0:2" in cmd
    assert "0:1" not in cmd
    assert "0:3" not in cmd


def test_build_remux_no_audio_streams():
    cmd = build_remux_cmd("/media/film.mkv", "/media/film.remuxing.mkv", keep_audio_indices=[])
    assert "0:v?" in cmd
    assert "0:s?" in cmd
    # No audio map entries beyond video/subs/attachments
    audio_maps = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-map" and cmd[i + 1].startswith("0:") and cmd[i + 1] not in ("0:v?", "0:s?", "0:t?")]
    assert audio_maps == []


def test_build_remux_preserves_map_order():
    cmd = build_remux_cmd("/media/movie.mkv", "/media/movie.remuxing.mkv", keep_audio_indices=[1, 2, 5])
    # Find all -map values
    maps = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-map"]
    assert maps.index("0:v?") < maps.index("0:s?")
    assert maps.index("0:s?") < maps.index("0:t?")
    # All requested audio indices present
    assert "0:1" in maps
    assert "0:2" in maps
    assert "0:5" in maps
