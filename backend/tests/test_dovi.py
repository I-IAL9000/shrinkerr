"""Keep Dolby Vision (v0.10.0): profiles 7 (as 8.1) and 8 are converted
with their RPU put back into the new HEVC stream; profile 5, resizes, AV1,
Quick Sync / VAAPI, discs and installs without dovi_tool still aren't."""
import json
import shutil
import subprocess

import pytest

from backend import dovi


def _enc() -> str:
    if not shutil.which("ffmpeg"):
        return ""
    return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


needs_dv = pytest.mark.skipif(not (dovi.tools_available() and "libx265" in _enc()),
                              reason="needs dovi_tool, mkvmerge and ffmpeg with libx265")


def test_when_it_is_kept(monkeypatch):
    monkeypatch.setattr(dovi, "tools_available", lambda: True)
    ok = dict(encoder="libx265", output_codec=None, target_resolution="copy", disc_type=None)
    assert dovi.can_keep("dv8", **ok) and dovi.can_keep("dv7", **ok) and dovi.convertible("dv8")
    assert dovi.can_keep("dv8", **{**ok, "encoder": "nvenc"}) and dovi.can_keep("dv8", **{**ok, "output_codec": "hevc"})
    for not_kept in (dict(hdr_format="dv5"), dict(hdr_format="dv"), dict(encoder="qsv"), dict(encoder="vaapi"),
                     dict(output_codec="av1"), dict(target_resolution="1080p"), dict(disc_type="bdmv")):
        args = {**ok, **not_kept}
        assert not dovi.can_keep(args.pop("hdr_format", "dv8"), **args), not_kept
    assert not dovi.convertible("dv5")
    monkeypatch.setattr(dovi, "tools_available", lambda: False)
    assert not dovi.can_keep("dv8", **ok) and not dovi.convertible("dv8")


def test_scans_offer_what_is_kept(monkeypatch):
    from backend import scanner
    monkeypatch.setattr(dovi, "tools_available", lambda: True)
    assert [scanner._dv_stays(f) for f in ("dv8", "dv7", "dv5", "dv", "hdr10", None)] == [False, False, True, True,
                                                                                        False, False]
    monkeypatch.setattr(dovi, "tools_available", lambda: False)
    assert scanner._dv_stays("dv8")


@pytest.mark.asyncio
async def test_profile_7_becomes_8_1(monkeypatch, tmp_path):
    """mode 2: profile 7's RPU as 8.1 (its enhancement layer isn't kept)."""
    import backend.scanner as scanner
    calls = []

    async def hevc_into(source, *args):
        calls.append(args)
        return 1, "stop here"

    async def frame(path):
        return {"color_transfer": "smpte2084", "pix_fmt": "yuv420p10le"}
    monkeypatch.setattr(dovi, "_hevc_into", hevc_into)
    monkeypatch.setattr(scanner, "first_frame_info", frame)
    out = tmp_path / "out.mkv"
    out.write_bytes(b"x")
    assert "couldn't read" in await dovi.keep("/m/src.mkv", str(out), "dv7")
    assert "couldn't read" in await dovi.keep("/m/src.mkv", str(out), "dv8")
    assert calls == [("-m", "2", "extract-rpu", "-", "-o", calls[0][-1]), ("extract-rpu", "-", "-o", calls[1][-1])]
    assert out.read_bytes() == b"x" and list(tmp_path.iterdir()) == [out]  # untouched, no leftovers


@pytest.mark.asyncio
async def test_what_it_checks(monkeypatch, tmp_path):
    """The encode keeps the HDR base layer and 10 bits; the rebuilt file has
    Dolby Vision — else the job fails and the original stays."""
    import backend.scanner as scanner
    hdr = {"color_transfer": "smpte2084", "pix_fmt": "yuv420p10le"}
    frames = {"src": hdr, "out": hdr, "rebuilt": {**hdr, "side_data_list": []}}

    async def frame(path):
        return frames["rebuilt" if path.endswith("out.converting.mkv") else "out" if path.endswith("out.mkv") else "src"]

    async def hevc_into(source, *args):
        open(args[-1], "wb").write(b"rpu")
        return 0, ""

    async def run(*args, stdin=None):
        if args[0] == "mkvmerge":
            open(args[3], "wb").write(b"rebuilt")
        return 0, ""

    async def count(path):
        return 48
    monkeypatch.setattr(scanner, "first_frame_info", frame)
    monkeypatch.setattr(dovi, "_hevc_into", hevc_into)
    monkeypatch.setattr(dovi, "_run", run)
    monkeypatch.setattr(dovi, "rpu_frames", count)
    monkeypatch.setattr(dovi, "video_frames", count)
    out = tmp_path / "out.mkv"
    out.write_bytes(b"x")
    assert await dovi.keep("/m/src.mkv", str(out), "dv8") == "the rebuilt file has no Dolby Vision"
    frames["rebuilt"] = {**hdr, "side_data_list": [{"side_data_type": "DOVI configuration record", "dv_profile": 8}]}
    for lost in ({**hdr, "color_transfer": "bt709"}, {**hdr, "pix_fmt": "yuv420p"}, None):
        frames["out"] = lost
        assert await dovi.keep("/m/src.mkv", str(out), "dv8") == "the encode lost the HDR base layer", lost
    frames["out"] = hdr
    assert await dovi.keep("/m/src.mkv", str(out), "dv8") is None and out.read_bytes() == b"rebuilt"
    assert list(tmp_path.iterdir()) == [out]


@pytest.mark.asyncio
async def test_a_profile_5_file_is_not_converted(tmp_path):
    from backend.converter import convert_file
    src = tmp_path / "Film.mkv"
    src.write_bytes(b"x" * 1000)
    probe = {"duration": 10.0, "file_size": 1000, "video_codec": "hevc", "hdr_format": "dv5",
             "audio_tracks": [], "subtitle_tracks": []}
    result = await convert_file(str(src), "libx265", 10.0, pre_probe=probe)
    assert (result["success"], result["error_key"]) == (False, "errors.dolbyVisionKept.profile")
    assert "profile 5" in result["error"] and result["error_params"]["profile"] == "5"
    assert src.read_bytes() == b"x" * 1000


def _dv_source(path, frames=48, profile="8.1"):
    """A synthetic Dolby Vision file: an HDR10 x265 encode with a generated RPU."""
    work = path.parent
    base, rpu, dv, gen = work / "base.hevc", work / "rpu.bin", work / "dv.hevc", work / "gen.json"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=640x360:r=24:d={frames / 24}",
                    "-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-preset", "ultrafast", "-x265-params",
                    "log-level=error:hdr10=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:repeat-headers=1",
                    "-f", "hevc", str(base)], check=True)
    gen.write_text(json.dumps({"cm_version": "V40", "profile": profile, "length": frames, "level6": {
        "max_display_mastering_luminance": 1000, "min_display_mastering_luminance": 1,
        "max_content_light_level": 1000, "max_frame_average_light_level": 400}}))
    for cmd in (["dovi_tool", "generate", "-j", str(gen), "-o", str(rpu)],
                ["dovi_tool", "inject-rpu", "-i", str(base), "--rpu-in", str(rpu), "-o", str(dv)],
                ["mkvmerge", "-q", "-o", str(path), "--default-duration", "0:24p", str(dv)]):
        subprocess.run(cmd, check=True, capture_output=True)
    for f in (base, rpu, dv, gen):
        f.unlink()


def _dv_profile(path) -> tuple:
    out = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,pix_fmt,color_transfer:stream_side_data=dv_profile,dv_bl_signal_compatibility_id",
         "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout)["streams"][0]
    side = (out.get("side_data_list") or [{}])[0]
    return out["codec_name"], out["color_transfer"], side.get("dv_profile"), side.get("dv_bl_signal_compatibility_id")


@needs_dv
@pytest.mark.asyncio
async def test_a_conversion_keeps_dolby_vision(test_db, tmp_path, monkeypatch):
    import os
    from backend.converter import convert_file
    real_rename, onto_existing = os.rename, []

    def rename(src, dst):  # an SMB share's recycle bin keeps a file renamed over
        if os.path.exists(dst):
            onto_existing.append(dst)
        real_rename(src, dst)
    monkeypatch.setattr(os, "rename", rename)
    src = tmp_path / "media" / "Film (2020)" / "Film (2020) 2160p DV HEVC.mkv"
    src.parent.mkdir(parents=True)
    _dv_source(src)
    assert _dv_profile(src) == ("hevc", "smpte2084", 8, 1)
    result = await convert_file(str(src), "libx265", 2.0, override_libx265_preset="ultrafast", override_crf=40)
    assert result["success"], result.get("error")
    out = result["output_path"]
    assert _dv_profile(out) == ("hevc", "smpte2084", 8, 1)
    rpu = tmp_path / "out_rpu.bin"
    subprocess.run(["dovi_tool", "extract-rpu", out, "-o", str(rpu)], check=True, capture_output=True)
    assert await dovi.rpu_frames(str(rpu)) == 48 == await dovi.video_frames(out)
    assert not list(src.parent.glob(".shrinkerr-dovi-*"))  # no leftovers
    assert onto_existing == []


@needs_dv
@pytest.mark.asyncio
async def test_a_mismatch_keeps_the_original(test_db, tmp_path, monkeypatch):
    from backend.converter import convert_file
    src = tmp_path / "m" / "Film (2020).mkv"
    src.parent.mkdir()
    _dv_source(src)
    before = src.read_bytes()

    async def frames(path):
        return 47
    monkeypatch.setattr(dovi, "video_frames", frames)
    result = await convert_file(str(src), "libx265", 2.0, override_libx265_preset="ultrafast", override_crf=40)
    assert (result["success"], result["error_key"]) == (False, "errors.dolbyVisionLost")
    assert "47 frames" in result["error_params"]["reason"] and src.read_bytes() == before
    assert sorted(p.name for p in src.parent.iterdir()) == ["Film (2020).mkv"]


@needs_dv
@pytest.mark.asyncio
async def test_without_the_tools_it_is_not_converted(test_db, tmp_path, monkeypatch):
    from backend.converter import convert_file
    src = tmp_path / "Film (2020).mkv"
    _dv_source(src)
    monkeypatch.setattr(dovi, "tools_available", lambda: False)
    result = await convert_file(str(src), "libx265", 2.0, override_libx265_preset="ultrafast")
    assert result["error_key"] == "errors.dolbyVisionKept.tools"


@pytest.mark.asyncio
async def test_scanned_files_follow_the_tools(test_db, monkeypatch):
    import aiosqlite
    import backend.database as database

    async def needs():
        async with aiosqlite.connect(test_db) as db:
            async with db.execute("SELECT file_path, needs_conversion FROM scan_results ORDER BY file_path") as cur:
                return dict(await cur.fetchall())
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("source_codecs", '["hevc"]'))
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('dovi_tools', 'false')")  # before
        for path, fmt in (("/m/a.mkv", "dv8"), ("/m/b.mkv", "dv5"), ("/m/c.mkv", "dv7")):
            await db.execute("INSERT INTO scan_results (file_path, file_size, video_codec, hdr_format, needs_conversion, "
                             "scan_timestamp) VALUES (?, 1, 'hevc', ?, 0, 'x')", (path, fmt))
        await db.commit()
    monkeypatch.setattr(dovi, "tools_available", lambda: True)
    await database.init_db()
    assert await needs() == {"/m/a.mkv": 1, "/m/b.mkv": 0, "/m/c.mkv": 1}
    monkeypatch.setattr(dovi, "tools_available", lambda: False)  # dovi_tool gone
    await database.init_db()
    assert await needs() == {"/m/a.mkv": 0, "/m/b.mkv": 0, "/m/c.mkv": 0}


def test_it_says_why_dolby_vision_isnt_kept(monkeypatch):
    """One message listed every condition, so it didn't say what to change."""
    monkeypatch.setattr(dovi, "tools_available", lambda: True)
    ok = dict(encoder="nvenc", output_codec=None, target_resolution="copy", disc_type=None)
    assert dovi.why_not_kept("dv8", **ok) is None and dovi.can_keep("dv7", **ok)
    assert dovi.why_not_kept("dv5", **ok) == "profile"
    assert dovi.why_not_kept("dv8", **{**ok, "target_resolution": "1080p"}) == "resize"
    assert dovi.why_not_kept("dv8", **{**ok, "output_codec": "av1"}) == "codec"
    assert dovi.why_not_kept("dv8", **{**ok, "encoder": "qsv"}) == "encoder"
    assert dovi.why_not_kept("dv8", **{**ok, "disc_type": "bdmv"}) == "disc"
    monkeypatch.setattr(dovi, "tools_available", lambda: False)
    assert dovi.why_not_kept("dv8", **ok) == "tools" and not dovi.can_keep("dv8", **ok)
