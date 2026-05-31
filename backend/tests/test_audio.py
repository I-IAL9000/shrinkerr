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


# ---------------------------------------------------------------------------
# v0.7.18: mov_text → srt transcoding in remux. Matroska's muxer rejects
# `mov_text` on `-c copy` ("Subtitle codec 94213 is not supported"); the
# remux pass must convert those streams to srt instead of blanket-copying.
# ---------------------------------------------------------------------------

def test_build_remux_movtext_transcoded_to_srt_when_keeping_all():
    """Keep-all-subs path with codec info: mov_text gets per-stream `srt`,
    other codecs get `copy`. The `-map 0:s?` shortcut must NOT be used
    (it loses per-stream codec control)."""
    cmd = build_remux_cmd(
        "/media/movie.mkv", "/media/movie.remuxing.mkv",
        keep_audio_indices=[1],
        subtitle_stream_codecs={2: "subrip", 3: "mov_text", 4: "ass"},
    )
    maps = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-map"]
    # Explicit per-sub maps replace the keep-all wildcard.
    assert "0:s?" not in maps, f"-map 0:s? leaked into per-stream path: {maps}"
    assert "0:2" in maps and "0:3" in maps and "0:4" in maps
    # Per-stream codec args: stream-0 (sub idx 2, subrip) → copy;
    # stream-1 (idx 3, mov_text) → srt; stream-2 (idx 4, ass) → copy.
    cs_args = [(cmd[i], cmd[i + 1]) for i, x in enumerate(cmd) if x.startswith("-c:s:")]
    assert ("-c:s:0", "copy") in cs_args, cs_args
    assert ("-c:s:1", "srt") in cs_args, cs_args
    assert ("-c:s:2", "copy") in cs_args, cs_args


def test_build_remux_movtext_transcoded_with_explicit_keep_list():
    """When the caller passes a keep_subtitle_indices list AND codec info,
    mov_text in the kept set still gets transcoded to srt."""
    cmd = build_remux_cmd(
        "/media/movie.mkv", "/media/movie.remuxing.mkv",
        keep_audio_indices=[1],
        keep_subtitle_indices=[3],
        subtitle_stream_codecs={2: "subrip", 3: "mov_text", 4: "ass"},
    )
    cs_args = [(cmd[i], cmd[i + 1]) for i, x in enumerate(cmd) if x.startswith("-c:s:")]
    # Only one output sub stream (idx 0) — and it MUST be srt since the
    # kept source is mov_text.
    assert cs_args == [("-c:s:0", "srt")], cs_args


def test_build_remux_no_codec_info_keeps_legacy_behavior():
    """Without subtitle_stream_codecs (the pre-v0.7.18 caller contract),
    fall through to the old `-map 0:s?` + global `-c copy` path so
    existing call sites (and the test_build_remux_keeps_specified_streams
    suite above) stay green."""
    cmd = build_remux_cmd(
        "/media/movie.mkv", "/media/movie.remuxing.mkv",
        keep_audio_indices=[1],
    )
    assert "0:s?" in cmd
    assert cmd[cmd.index("-c") + 1] == "copy"
