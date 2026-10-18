"""Settings → Video → Dolby Vision (v0.10.0): keep it (as before), keep it
when the conversion can and otherwise convert the HDR under it, or always
convert the HDR under it. A 4K Dolby Vision remux resized to 1080p came out
as nothing for someone who wanted 1080p HDR10. Profile 5 has no HDR under
its Dolby Vision (it turns purple and green) and is never converted."""
import shutil

import aiosqlite
import pytest

from backend import dovi
from backend.scanner import dv_base_of


def test_what_is_under_dolby_vision():
    def config(compat, transfer="smpte2084"):
        return {"color_transfer": transfer, "side_data_list": [
            {"side_data_type": "DOVI configuration record", "dv_profile": 8, "dv_bl_signal_compatibility_id": compat}]}
    assert [dv_base_of(config(c)) for c in (1, 6, 4, 2, 0)] == ["hdr10", "hdr10", "hlg", None, None]
    assert dv_base_of({"color_transfer": "smpte2084"}) == "hdr10"  # no configuration record: the transfer
    assert dv_base_of({"color_transfer": "arib-std-b67"}) == "hlg"
    assert dv_base_of({"color_transfer": "bt709"}) is None


def test_only_profiles_7_and_8_can_be_dropped():
    assert dovi.drop_to("dv8", "hdr10") == "hdr10" and dovi.drop_to("dv7", "hdr10") == "hdr10"
    assert dovi.drop_to("dv8", "hlg") == "hlg"
    assert dovi.drop_to("dv5", "hdr10") is None  # purple and green
    assert dovi.drop_to("dv8", None) is None  # an SDR base: no HDR left


def test_scans_offer_them_without_the_tools_when_it_is_dropped(monkeypatch):
    monkeypatch.setattr(dovi, "tools_available", lambda: False)
    monkeypatch.setattr(dovi, "_mode", "keep")
    assert not dovi.convertible("dv8")
    dovi.set_mode("hdr10")
    assert dovi.convertible("dv8") and dovi.convertible("dv7") and not dovi.convertible("dv5")
    dovi.set_mode("nonsense")
    assert dovi._mode == "keep"


def test_what_strips_it(monkeypatch):
    monkeypatch.setattr(dovi, "_strip_filter", lambda: True)
    monkeypatch.setattr(dovi, "_x265_writes_it", lambda: True)
    assert dovi.strip_args("libx265") == ["-dolbyvision", "0", "-bsf:v", "dovi_rpu=strip=1"]
    assert dovi.strip_args("nvenc") == ["-bsf:v", "dovi_rpu=strip=1"]
    monkeypatch.setattr(dovi, "_strip_filter", lambda: False)
    assert dovi.strip_args("libx265") == ["-dolbyvision", "0"] and dovi.strip_args("nvenc") == []
    monkeypatch.setattr(dovi, "_x265_writes_it", lambda: False)  # ffmpeg before 7.1: neither
    assert dovi.strip_args("libx265") == []


async def _command(test_db, tmp_path, mode, hdr_format, resolution, tools=True, monkeypatch=None):
    from backend.converter import convert_file
    from backend.tests.test_same_name_conversion import _mkv
    monkeypatch.setattr(dovi, "tools_available", lambda: tools)
    monkeypatch.setattr(dovi, "_strip_filter", lambda: True)
    monkeypatch.setattr(dovi, "_x265_writes_it", lambda: True)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('dolby_vision_mode', ?)", (mode,))
        await db.commit()
    src = tmp_path / "Stolen Kisses (1968) 2160p Remux.mkv"
    _mkv(src, ["fre"])
    probe = {"duration": 2.0, "file_size": src.stat().st_size, "video_codec": "hevc", "hdr_format": hdr_format,
             "dv_base": "hdr10", "video_pix_fmt": "yuv420p10le", "video_width": 3840, "video_height": 2160,
             "audio_tracks": [], "subtitle_tracks": []}
    return await convert_file(str(src), "libx265", 2.0, pre_probe=probe, command_only=True,
                              override_target_resolution=resolution)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
@pytest.mark.asyncio
@pytest.mark.parametrize("mode, hdr_format, resolution, outcome", [
    ("keep", "dv8", "1080p", "errors.dolbyVisionKept.resize"),  # as before
    ("keep_or_hdr10", "dv8", "1080p", "dropped"),                # can't be kept: HDR10
    ("keep_or_hdr10", "dv8", "copy", "kept"),                    # can be kept: kept
    ("hdr10", "dv8", "copy", "dropped"),                         # always HDR10
    ("hdr10", "dv5", "1080p", "errors.dolbyVisionKept.profile"),  # never profile 5
])
async def test_the_setting(test_db, tmp_path, monkeypatch, mode, hdr_format, resolution, outcome):
    result = await _command(test_db, tmp_path, mode, hdr_format, resolution, monkeypatch=monkeypatch)
    if outcome.startswith("errors."):
        assert (result["success"], result["error_key"]) == (False, outcome)
        return
    assert result["success"], result.get("error")
    command = " ".join(result["command"])
    assert ("dovi_rpu=strip=1" in command and "-dolbyvision 0" in command) == (outcome == "dropped"), command


@pytest.mark.skipif(not (dovi.tools_available() and shutil.which("ffmpeg")), reason="needs dovi_tool and mkvmerge")
@pytest.mark.asyncio
async def test_a_dropped_dolby_vision_leaves_hdr10(test_db, tmp_path):
    """The real thing: a profile 8.1 file converted with "Convert to HDR10"
    comes out HDR10, with no Dolby Vision left in it."""
    from backend.converter import convert_file
    from backend.scanner import probe_file
    from backend.tests.test_dovi import _dv_source
    src = tmp_path / "m" / "Film (2020).mkv"
    src.parent.mkdir()
    _dv_source(src)
    probe = await probe_file(str(src))
    assert (probe["hdr_format"], probe["dv_base"]) == ("dv8", "hdr10")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('dolby_vision_mode', 'hdr10')")
        await db.commit()
    result = await convert_file(str(src), "libx265", 2.0, override_libx265_preset="ultrafast", override_crf=40)
    assert result["success"], result.get("error")
    out = result["output_path"]
    assert (await probe_file(out))["hdr_format"] == "hdr10" and not await dovi.left_in(out)
