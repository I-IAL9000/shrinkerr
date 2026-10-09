"""v0.10.0: HDR / Dolby Vision guardrails (SC-24).

Nothing looked at HDR: a re-encode of Dolby Vision profile 5 turns purple
and green, every Dolby Vision profile loses its Dolby Vision layer, Quick
Sync / VAAPI encode HDR to 8-bit, and an encoder that drops the HDR
signalling leaves washed-out colours — all replaced the original.
"""
import shutil
import subprocess

import pytest

import backend.converter as converter
from backend.scanner import hdr_format_of, is_dolby_vision, probe_file

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _has_libx265() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "libx265" in out


def _dovi(profile, compat):
    return {"side_data_list": [{"side_data_type": "DOVI configuration record", "dv_profile": profile,
                                "dv_bl_signal_compatibility_id": compat}]}


@pytest.mark.parametrize("stream, expected", [
    (_dovi(5, 0), "dv5"),
    ({**_dovi(8, 1), "color_transfer": "smpte2084"}, "dv8"),
    ({"color_transfer": "smpte2084"}, "hdr10"),
    ({"color_transfer": "arib-std-b67"}, "hlg"),
    ({"color_transfer": "bt709"}, None),
    ({}, None),
    ({"side_data_list": [{"side_data_type": "Dolby Vision RPU Data"}]}, "dv"),
])
def test_hdr_format_of(stream, expected):
    assert hdr_format_of(stream) == expected
    assert is_dolby_vision(expected) is bool(expected and expected.startswith("dv"))


def _hdr10_clip(path, seconds=2):
    """HDR10 signalled only in the HEVC bitstream, as many encodes are."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=24:duration={seconds}",
         "-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-preset", "ultrafast",
         "-x265-params", "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
                         "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1):"
                         "max-cll=1000,400:hdr10=1:log-level=error",
         str(path)], check=True)


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_libx265(), reason="needs ffmpeg with libx265")
async def test_hdr10_in_the_bitstream_is_detected(tmp_path):
    clip = tmp_path / "Movie (2020) 2160p HDR.mkv"
    _hdr10_clip(clip)
    assert (await probe_file(str(clip)))["hdr_format"] == "hdr10"


async def _probe_says(monkeypatch, hdr_format):
    import backend.scanner as scanner
    real = scanner.probe_file

    async def probe(path, *args, **kwargs):
        data = await real(path, *args, **kwargs)
        if data:
            data["hdr_format"] = hdr_format
        return data

    monkeypatch.setattr(scanner, "probe_file", probe)


@pytest.mark.asyncio
@needs_ffmpeg
@pytest.mark.parametrize("hdr, encoder, key", [("dv5", "libx265", "errors.dolbyVisionSkipped"),
                                               ("dv8", "nvenc", "errors.dolbyVisionSkipped"),
                                               ("hdr10", "qsv", "errors.hdrNeeds10Bit"),
                                               ("hlg", "vaapi", "errors.hdrNeeds10Bit")])
async def test_unsafe_hdr_encodes_are_refused_up_front(test_db, tmp_path, monkeypatch, hdr, encoder, key):
    from backend.tests.test_same_name_conversion import _mkv
    src = tmp_path / "Movie (2020) 2160p WEB h264.mkv"
    _mkv(src, ["eng"])
    before = src.read_bytes()
    await _probe_says(monkeypatch, hdr)
    result = await converter.convert_file(str(src), encoder, 2.0)
    assert result["success"] is False and result["error_key"] == key
    assert src.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_libx265(), reason="needs ffmpeg with libx265")
async def test_hdr10_survives_a_libx265_conversion(test_db, tmp_path):
    src = tmp_path / "Movie (2020) 2160p HDR.mkv"
    _hdr10_clip(src)
    result = await converter.convert_file(str(src), "libx265", 2.0, override_libx265_preset="ultrafast")
    assert result["success"], result.get("error")
    assert (await probe_file(result["output_path"]))["hdr_format"] == "hdr10"


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_libx265(), reason="needs ffmpeg with libx265")
async def test_an_output_that_lost_its_hdr_is_rejected(test_db, tmp_path, monkeypatch):
    src = tmp_path / "Movie (2020) 2160p HDR.mkv"
    _hdr10_clip(src)
    before = src.read_bytes()
    import backend.scanner as scanner
    real = scanner.first_frame_info

    async def sdr_output(path, *args, **kwargs):
        frame = await real(path, *args, **kwargs)
        if frame and ".converting." in path:
            frame = {**frame, "color_transfer": "bt709", "side_data_list": []}
        return frame

    monkeypatch.setattr(scanner, "first_frame_info", sdr_output)
    result = await converter.convert_file(str(src), "libx265", 2.0, override_libx265_preset="ultrafast")
    assert result["success"] is False and result["error_key"] == "errors.hdrLost"
    assert src.read_bytes() == before
    assert not list(tmp_path.glob("*.converting.*"))
