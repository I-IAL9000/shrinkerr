"""NVENC's HEVC level (v0.10.0).

Left to choose, NVENC picked level 4.0 for 1080p and held every encode
under its bitrate, about 10 Mbps: a Blu-ray sample came out at 10.1 Mbps
at CQ 14 and at CQ 23 alike, 34.2 Mbps at CQ 14 with level 5.1. Grainy
films missed the VMAF target whatever the CQ (Silent Friend: 86.7 at CQ
26, 86.9 at CQ 19). Measured on the maintainer's Quadro P2200, 31 Oct 2026.
"""
from backend.converter import _build_ffmpeg_cmd_impl


def _arg(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def test_hd_is_encoded_at_level_5_1():
    cmd = _build_ffmpeg_cmd_impl("in.mkv", "out.mkv", encoder="nvenc", cq=14, output_width=1920)
    assert _arg(cmd, "-level") == "5.1" and "-tier" not in cmd


def test_4k_takes_the_high_tier_as_uhd_blu_rays_do():
    cmd = _build_ffmpeg_cmd_impl("in.mkv", "out.mkv", encoder="nvenc", cq=14, output_width=3840)
    assert _arg(cmd, "-level") == "5.1" and _arg(cmd, "-tier") == "high"


def test_other_encoders_and_av1_are_unchanged():
    for encoder in ("libx265", "qsv", "vaapi", "videotoolbox"):
        assert "-level" not in _build_ffmpeg_cmd_impl("in.mkv", "out.mkv", encoder=encoder, output_width=1920)
    assert "-level" not in _build_ffmpeg_cmd_impl("in.mkv", "out.mkv", encoder="nvenc", output_codec="av1",
                                                  output_width=1920)
