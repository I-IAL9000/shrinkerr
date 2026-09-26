"""v0.9.133: Apple VideoToolbox (hevc_videotoolbox) encoder support."""
import asyncio
import json
import shutil
import subprocess
import sys

import pytest

from backend import encoder_caps
from backend.converter import _ENCODING_SETTINGS, _build_ffmpeg_cmd_impl, hw_decode_supports
from backend.encoder_caps import EncoderCaps


def _vt_cmd(**kw):
    return _build_ffmpeg_cmd_impl("/in.mkv", "/out.mkv", encoder="videotoolbox", **kw)


def _after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_software_decode_8bit_command():
    cmd = _vt_cmd(videotoolbox_quality=60, nvenc_bit_depth="8bit")
    assert _after(cmd, "-c:v") == "hevc_videotoolbox"
    assert _after(cmd, "-q:v") == "60"
    assert _after(cmd, "-profile:v") == "main"
    assert _after(cmd, "-pix_fmt") == "nv12"
    assert "-hwaccel" not in cmd
    assert "-allow_sw" not in cmd


def test_10bit_source_uses_main10():
    cmd = _vt_cmd(nvenc_bit_depth="10bit")
    assert _after(cmd, "-profile:v") == "main10"
    assert _after(cmd, "-pix_fmt") == "p010le"


def test_hw_decode_downloads_frames_and_scales_in_software():
    cmd = _vt_cmd(use_hw_decode=True, hw_decode_backend="videotoolbox",
                  hw_decode_keeps_on_device=True, target_resolution="720p")
    assert _after(cmd, "-hwaccel") == "videotoolbox"
    # No on-device surfaces: frames land in system memory so plain `scale`
    # works and unsupported codecs fall back to software on their own.
    assert "-hwaccel_output_format" not in cmd
    assert _after(cmd, "-vf") == "scale=1280:-2"
    assert cmd.index("-hwaccel") < cmd.index("-i")


def test_hw_decode_table():
    assert hw_decode_supports("videotoolbox", "hevc")
    assert hw_decode_supports("videotoolbox", "h264")
    assert not hw_decode_supports("videotoolbox", "mpeg2video")
    assert not hw_decode_supports("videotoolbox", "h264", "yuv420p10le")


def test_settings_defaults_registered():
    keys = {k: d for k, d, _ in _ENCODING_SETTINGS}
    assert keys["videotoolbox_quality"] == 55
    assert keys["videotoolbox_hw_decode"] is True


def test_caps_available_lists_videotoolbox():
    caps = EncoderCaps(nvenc=False, qsv=False, vaapi=False, videotoolbox=True)
    assert caps.available == ["libx265", "videotoolbox"]
    assert "videotoolbox" not in EncoderCaps(nvenc=True, qsv=False, vaapi=False).available


@pytest.mark.parametrize("platform,expected", [("darwin", True), ("linux", False)])
def test_detect_encoders_only_on_macos(monkeypatch, platform, expected):
    monkeypatch.setattr(encoder_caps, "_cached", None)
    monkeypatch.setattr(encoder_caps.sys, "platform", platform)
    monkeypatch.setattr(encoder_caps, "_ffmpeg_encoders", lambda: {"libx265", "hevc_videotoolbox"})
    monkeypatch.setattr(encoder_caps, "_ffmpeg_hwaccels", lambda: {"videotoolbox"})
    monkeypatch.setattr(encoder_caps, "_nvidia_present", lambda: False)
    monkeypatch.setattr(encoder_caps, "_list_render_nodes", lambda: [])
    caps = encoder_caps.detect_encoders(force=True)
    assert caps.videotoolbox is expected
    assert caps.videotoolbox_decode_available is expected


def test_encode_probe_refuses_off_macos(monkeypatch):
    monkeypatch.setattr(encoder_caps.sys, "platform", "linux")
    assert asyncio.run(encoder_caps.videotoolbox_encode_works()) is False


def _vt_on_host() -> bool:
    if sys.platform != "darwin" or not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "hevc_videotoolbox" in out


@pytest.mark.skipif(not _vt_on_host(), reason="needs macOS with a VideoToolbox-enabled ffmpeg")
@pytest.mark.parametrize("hw", [False, True])
def test_real_encode_on_this_mac(tmp_path, hw):
    """End-to-end: the argv Shrinkerr builds actually runs on VideoToolbox."""
    src = tmp_path / "src.mkv"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
         "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)],
        check=True,
    )
    out = tmp_path / "out.mkv"
    cmd = _build_ffmpeg_cmd_impl(
        str(src), str(out), encoder="videotoolbox", videotoolbox_quality=55,
        use_hw_decode=hw, hw_decode_backend="videotoolbox" if hw else None,
        source_codec="h264", nvenc_bit_depth="8bit",
    )
    run = subprocess.run(cmd, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr[-800:]
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,codec_type",
         "-of", "json", str(out)], capture_output=True, text=True, check=True,
    ).stdout)["streams"]
    assert {s["codec_type"]: s["codec_name"] for s in probe} == {"video": "hevc", "audio": "aac"}
