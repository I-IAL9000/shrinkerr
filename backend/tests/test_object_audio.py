"""Atmos / DTS:X guard (v0.10.0): "Convert lossless audio" re-encoded
TrueHD Atmos and DTS:X to a lossy codec, which keeps the 5.1 / 7.1 bed and
drops the objects. They stay lossless now (a setting, on by default)."""
import json
import shutil
import subprocess

import aiosqlite
import pytest

from backend.converter import (_build_audio_conversion_summary, build_ffmpeg_cmd, is_lossless_audio,
                               is_object_audio, lossless_to_convert)


def test_what_counts_as_object_audio():
    assert is_object_audio("Dolby TrueHD + Dolby Atmos") and is_object_audio("DTS-HD MA + DTS:X")
    assert is_object_audio("", "English TrueHD Atmos 7.1") and is_object_audio("", "DTS-X 7.1")
    assert not is_object_audio("DTS-HD MA", "English 5.1") and not is_object_audio("", "")
    # ffmpeg 6.1+ calls DTS:X "DTS-HD MA + DTS:X": it's lossless (it didn't count).
    assert is_lossless_audio("dts", "DTS-HD MA + DTS:X") and not is_lossless_audio("dts", "DTS")
    assert lossless_to_convert("truehd", "", "TrueHD 5.1")
    assert not lossless_to_convert("truehd", "Dolby TrueHD + Dolby Atmos")
    assert lossless_to_convert("truehd", "Dolby TrueHD + Dolby Atmos", keep_object_audio=False)
    assert not lossless_to_convert("eac3", "", "DD+ Atmos")  # lossy: never touched


def _codecs(cmd: list[str]) -> dict[str, str]:
    return {cmd[i]: cmd[i + 1] for i in range(len(cmd) - 1) if cmd[i].startswith("-c:a:")}


def test_the_command_copies_them():
    lossless = {"codec": "eac3", "bitrate": 640, "profiles": ["Dolby TrueHD + Dolby Atmos", "", ""],
                "titles": ["", "TrueHD 5.1", "Commentary"], "keep_objects": True}
    cmd = build_ffmpeg_cmd("/m/in.mkv", "/m/out.mkv", encoder="libx265", lossless_conversion=lossless,
                           audio_stream_codecs=["truehd", "truehd", "ac3"])
    assert _codecs(cmd) == {"-c:a:0": "copy", "-c:a:1": "eac3", "-c:a:2": "copy"}
    cmd = build_ffmpeg_cmd("/m/in.mkv", "/m/out.mkv", encoder="libx265",
                           lossless_conversion={**lossless, "keep_objects": False},
                           audio_stream_codecs=["truehd", "truehd", "ac3"])
    assert _codecs(cmd) == {"-c:a:0": "eac3", "-c:a:1": "eac3", "-c:a:2": "copy"}


def test_tracks_chosen_by_hand_too():
    from backend.converter import _build_ffmpeg_cmd_impl
    keep = [{"stream_index": 1, "codec": "dts", "profile": "DTS-HD MA + DTS:X"},
            {"stream_index": 2, "codec": "dts", "profile": "DTS-HD MA"}]
    cmd = _build_ffmpeg_cmd_impl("/m/in.mkv", "/m/out.mkv", encoder="libx265", audio_streams_to_keep=keep,
                                 lossless_conversion={"codec": "eac3", "bitrate": 640, "keep_objects": True})
    assert _codecs(cmd) == {"-c:a:0": "copy", "-c:a:1": "eac3"}


def test_the_job_report_lists_only_what_was_converted():
    tracks = [{"codec": "truehd", "profile": "Dolby TrueHD + Dolby Atmos"}, {"codec": "dts", "profile": "DTS-HD MA"}]
    assert _build_audio_conversion_summary(tracks, "copy", {"codec": "eac3", "keep_objects": True}) == ["DTS-HD MA"]


def _encoders() -> str:
    if not shutil.which("ffmpeg"):
        return ""
    return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


@pytest.mark.asyncio
@pytest.mark.skipif(not all(e in _encoders() for e in ("libx265", "truehd", "eac3")),
                    reason="needs ffmpeg with libx265, truehd and eac3")
@pytest.mark.parametrize("keep, expected", [("true", ["truehd", "eac3"]), ("false", ["eac3", "eac3"])])
async def test_a_real_encode(test_db, tmp_path, keep, expected):
    from backend.converter import convert_file
    clip = tmp_path / "Film (2020).mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=2",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=2",
         "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264", "-qp", "0", "-c:a", "truehd", "-strict", "-2",
         "-metadata:s:a:0", "title=TrueHD Atmos 7.1", "-metadata:s:a:1", "title=TrueHD 5.1", str(clip)],
        check=True)
    async with aiosqlite.connect(test_db) as db:
        for k, v in (("auto_convert_lossless", "true"), ("lossless_target_codec", "eac3"),
                     ("lossless_target_bitrate", "192"), ("lossless_keep_object_audio", keep)):
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()
    result = await convert_file(str(clip), "libx265", 2.0, override_libx265_preset="ultrafast")
    assert result["success"], result.get("error")
    out = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_name", "-of", "json",
         result["output_path"]], capture_output=True, text=True, check=True).stdout)
    assert [s["codec_name"] for s in out["streams"]] == expected
