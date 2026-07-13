import pytest
from backend.converter import (
    build_ffmpeg_cmd,
    _build_ffmpeg_cmd_impl,
    rename_x264_to_x265,
    rename_resolution_in_filename,
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


# ---------------------------------------------------------------------------
# v0.7.24: resolution tag rewriting when downscaling.
# ---------------------------------------------------------------------------

def test_rename_resolution_2160_to_1080():
    """The classic 2160p → 1080p downscale should rewrite the filename tag."""
    assert rename_resolution_in_filename(
        "Movie (2024) 2160p UHD x265-GRP", "1080p"
    ) == "Movie (2024) 1080p x265-GRP", (
        "2160p and the redundant UHD marker must both collapse to the target."
    )


def test_rename_resolution_4k_marker_replaced():
    """Colloquial 4K marker (without a pixel suffix) gets rewritten too."""
    assert rename_resolution_in_filename(
        "Movie 4K Bluray x265", "1080p"
    ) == "Movie 1080p Bluray x265"


def test_rename_resolution_copy_leaves_filename_alone():
    """target_resolution='copy' (no scaling) must NOT touch the resolution."""
    assert rename_resolution_in_filename(
        "Movie 2160p UHD x265", "copy"
    ) == "Movie 2160p UHD x265"
    # None should also be a no-op.
    assert rename_resolution_in_filename(
        "Movie 1080p x264", None
    ) == "Movie 1080p x264"


def test_rename_resolution_dot_separated():
    """Scene-style dot-separated tokens (`Show.S01E01.2160p.UHD.x264-GRP`)
    must be rewritten too — `\\b` boundaries handle non-letter separators."""
    assert rename_resolution_in_filename(
        "Show.S01E01.2160p.UHD.x264-GRP", "720p"
    ) == "Show.S01E01.720p.x264-GRP", (
        "dot-separated 2160p + UHD must collapse to a single 720p token."
    )


def test_rename_resolution_no_token_present():
    """If the filename has no resolution token, leave it alone."""
    assert rename_resolution_in_filename(
        "Movie (2020) Bluray x264.mkv", "1080p"
    ) == "Movie (2020) Bluray x264.mkv"


def test_get_output_path_downscale_renames_resolution_and_codec():
    """End-to-end: get_output_path should rename both codec AND resolution
    when downscaling. Pre-v0.7.24 only the codec was renamed."""
    out = get_output_path(
        "/media/Movie (2024) 2160p UHD x264-GRP.mkv",
        encoder="libx265",
        target_resolution="1080p",
    )
    assert out.endswith("Movie (2024) 1080p x265-GRP.mkv"), (
        f"got {out!r}, expected …Movie (2024) 1080p x265-GRP.mkv"
    )


def test_get_output_path_no_resolution_param_is_back_compat():
    """Callers that don't pass target_resolution (back-compat) must get
    pre-v0.7.24 behaviour: codec renamed, resolution untouched."""
    out = get_output_path(
        "/media/Movie 2160p x264.mkv",
        encoder="libx265",
    )
    assert out.endswith("Movie 2160p x265.mkv"), f"got {out!r}"


# ---------------------------------------------------------------------------
# v0.7.29: disc audio-language injection. The bluray:/concat: input
# protocol strips per-stream language tags, so disc conversions must
# explicitly stamp `-metadata:s:a:N language=X` on the output or the
# audio track shows as `und`.
# ---------------------------------------------------------------------------

def _metadata_lang_args(cmd):
    """Extract [(flag, value), …] for every -metadata:s:a:N in a cmd."""
    return [
        (cmd[i], cmd[i + 1])
        for i, x in enumerate(cmd)
        if x.startswith("-metadata:s:a:")
    ]


def test_disc_audio_language_injected_default_path():
    """Disc conversion, map-all-audio path: each detected language is
    stamped on the matching output audio stream."""
    cmd = _build_ffmpeg_cmd_impl(
        "bluray:/media/Movie/disc.iso", "/media/Movie/out.converting.mkv",
        encoder="nvenc",
        disc_audio_languages=["fre", "eng"],
    )
    args = _metadata_lang_args(cmd)
    assert ("-metadata:s:a:0", "language=fre") in args, args
    assert ("-metadata:s:a:1", "language=eng") in args, args


def test_disc_audio_language_skips_und_and_empty():
    """und / empty entries must NOT emit a metadata arg (leave the stream
    untagged rather than writing a bogus 'und')."""
    cmd = _build_ffmpeg_cmd_impl(
        "bluray:/media/Movie/disc.iso", "/media/Movie/out.converting.mkv",
        encoder="nvenc",
        disc_audio_languages=["und", "eng", ""],
    )
    args = _metadata_lang_args(cmd)
    # Only the eng track (output index 1) gets a tag.
    assert args == [("-metadata:s:a:1", "language=eng")], args


def test_disc_audio_language_keep_list_uses_track_language():
    """Keep-list path (track removal / reorder): language comes from each
    kept track's own `language` field, in output order."""
    cmd = _build_ffmpeg_cmd_impl(
        "bluray:/media/Movie/disc.iso", "/media/Movie/out.converting.mkv",
        encoder="nvenc",
        audio_streams_to_keep=[
            {"stream_index": 2, "codec": "eac3", "language": "eng"},
            {"stream_index": 1, "codec": "dts", "language": "fre"},
        ],
        disc_audio_languages=["fre", "eng"],  # source order; keep-list overrides
    )
    args = _metadata_lang_args(cmd)
    # Output order follows the keep-list: a:0=eng, a:1=fre.
    assert ("-metadata:s:a:0", "language=eng") in args, args
    assert ("-metadata:s:a:1", "language=fre") in args, args


def test_regular_file_no_audio_language_injection():
    """Non-disc conversion (disc_audio_languages=None) must NOT inject any
    audio language metadata — ffmpeg's tag-copy handles it, and injecting
    would risk overwriting correct source tags."""
    cmd = _build_ffmpeg_cmd_impl(
        "/media/Movie 1080p x264.mkv", "/media/Movie.converting.mkv",
        encoder="nvenc",
    )
    assert _metadata_lang_args(cmd) == [], _metadata_lang_args(cmd)


def test_regular_file_auto_detected_audio_language_injected():
    """v0.8.0: a REGULAR (non-disc) input whose und audio was upgraded by
    auto-detect must still get the detected language stamped. The metadata
    injection is gated on `disc_audio_languages is not None`, not on a disc
    URL — so building the list (as convert_file now does when
    _auto_detected_audio is True) injects the tag on regular files too."""
    cmd = _build_ffmpeg_cmd_impl(
        "/media/Movie 1080p x264.mkv", "/media/Movie.converting.mkv",
        encoder="nvenc",
        disc_audio_languages=["swe"],
    )
    args = _metadata_lang_args(cmd)
    assert ("-metadata:s:a:0", "language=swe") in args, args


# ---------------------------------------------------------------------------
# v0.7.30: disc SUBTITLE language injection (same root cause as v0.7.29's
# audio fix — bluray:/concat: strips embedded-sub language tags too).
# ---------------------------------------------------------------------------

def _metadata_sub_lang_args(cmd):
    """Extract [(flag, value), …] for every -metadata:s:s:N in a cmd."""
    return [
        (cmd[i], cmd[i + 1])
        for i, x in enumerate(cmd)
        if x.startswith("-metadata:s:s:")
    ]


def test_disc_subtitle_language_injected():
    """Disc conversion: each embedded subtitle's detected language is
    stamped on the matching output subtitle stream."""
    cmd = _build_ffmpeg_cmd_impl(
        "bluray:/media/Movie/disc.iso", "/media/Movie/out.converting.mkv",
        encoder="nvenc",
        subtitle_streams=[
            {"codec_name": "hdmv_pgs_subtitle", "index": 3, "language": "fre"},
            {"codec_name": "hdmv_pgs_subtitle", "index": 4, "language": "eng"},
        ],
        disc_audio_languages=[],  # disc marker (no audio, but still a disc)
    )
    args = _metadata_sub_lang_args(cmd)
    assert ("-metadata:s:s:0", "language=fre") in args, args
    assert ("-metadata:s:s:1", "language=eng") in args, args


def test_disc_subtitle_language_skips_und():
    """und subtitle language must not emit a metadata arg."""
    cmd = _build_ffmpeg_cmd_impl(
        "bluray:/media/Movie/disc.iso", "/media/Movie/out.converting.mkv",
        encoder="nvenc",
        subtitle_streams=[
            {"codec_name": "hdmv_pgs_subtitle", "index": 3, "language": "und"},
            {"codec_name": "hdmv_pgs_subtitle", "index": 4, "language": "spa"},
        ],
        disc_audio_languages=[],
    )
    args = _metadata_sub_lang_args(cmd)
    assert args == [("-metadata:s:s:1", "language=spa")], args


def test_regular_file_no_subtitle_language_injection():
    """Non-disc conversion must NOT inject subtitle language metadata for
    embedded subs — ffmpeg copies the container's tag."""
    cmd = _build_ffmpeg_cmd_impl(
        "/media/Movie.mkv", "/media/Movie.converting.mkv",
        encoder="nvenc",
        subtitle_streams=[
            {"codec_name": "subrip", "index": 2, "language": "eng"},
        ],
    )
    assert _metadata_sub_lang_args(cmd) == [], _metadata_sub_lang_args(cmd)


# ---------------------------------------------------------------------------
# v0.7.31: no-audio sources. `-map 0:a` hard-fails ("Stream map matches no
# streams", exit 234) on a video-only file; must be `0:a?`.
# ---------------------------------------------------------------------------

def test_default_audio_map_uses_optional_selector():
    """The default audio map must be `0:a?` (optional), not `0:a`, so a
    source with no audio stream (video-only Blu-ray remux, sample clip
    with only video + PGS subs) doesn't hard-fail."""
    cmd = _build_ffmpeg_cmd_impl(
        "/media/Movie.mkv", "/media/Movie.converting.mkv",
        encoder="nvenc",
    )
    maps = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-map"]
    assert "0:a?" in maps, f"default audio map not optional: {maps}"
    assert "0:a" not in maps, (
        f"bare -map 0:a present — hard-fails on no-audio sources "
        f"(exit 234): {maps}"
    )


@pytest.mark.asyncio
async def test_auto_detect_patches_und_audio_before_convert(monkeypatch):
    from backend import converter as conv
    tracks = [{"stream_index": 1, "language": "und", "codec": "aac"}]
    async def fake_audio_detect(fp, idx, duration=0.0):
        return ("swe", 0.88)
    monkeypatch.setattr(conv, "_auto_detect_enabled", lambda: True)
    monkeypatch.setattr("backend.language_detection.detect_audio_language", fake_audio_detect)
    patched = await conv._detect_und_track_languages("/media/movie.mkv", tracks, [], duration=1800.0)
    assert patched[0]["language"] == "swe"


@pytest.mark.asyncio
async def test_auto_detect_disabled_leaves_und(monkeypatch):
    from backend import converter as conv
    tracks = [{"stream_index": 1, "language": "und", "codec": "aac"}]
    monkeypatch.setattr(conv, "_auto_detect_enabled", lambda: False)
    patched = await conv._detect_und_track_languages("/media/movie.mkv", tracks, [], duration=1800.0)
    assert patched[0]["language"] == "und"


@pytest.mark.asyncio
async def test_auto_detect_skips_already_tagged(monkeypatch):
    from backend import converter as conv
    called = []
    async def fake_audio_detect(fp, idx, duration=0.0):
        called.append(idx); return ("swe", 0.9)
    monkeypatch.setattr(conv, "_auto_detect_enabled", lambda: True)
    monkeypatch.setattr("backend.language_detection.detect_audio_language", fake_audio_detect)
    tracks = [{"stream_index": 1, "language": "eng", "codec": "aac"}]
    patched = await conv._detect_und_track_languages("/media/movie.mkv", tracks, [], duration=1800.0)
    assert patched[0]["language"] == "eng"
    assert called == []  # already tagged → no detection call
