"""Audio options (v0.10.0): a stereo compatibility track beside the original
audio (optionally loudness-normalised), and lossy DTS converted along with
lossless audio."""
import json
import shutil
import subprocess

import aiosqlite
import pytest

from backend.converter import COMPAT_TRACK_TITLE, _build_ffmpeg_cmd_impl, compat_track_plan, lossless_to_convert

ON = {"audio_compat_track": True, "audio_compat_codec": "eac3", "audio_compat_bitrate": 224}


def _t(index, lang, channels, codec="truehd"):
    return {"stream_index": index, "language": lang, "channels": channels, "codec": codec}


def test_when_a_compatibility_track_is_added():
    assert compat_track_plan([_t(1, "eng", 8), _t(2, "fre", 6)], ON) == {
        "source": 1, "language": "eng", "out_index": 2, "codec": "eac3", "bitrate": 224, "loudnorm": False}
    assert compat_track_plan([_t(1, "eng", 2)], ON) is None  # already stereo
    assert compat_track_plan([_t(1, "eng", 6), _t(2, "eng", 2, "aac")], ON) is None  # a stereo one is there
    assert compat_track_plan([_t(1, "eng", 6), _t(2, "fre", 2, "aac")], ON)["source"] == 1  # another language's
    assert compat_track_plan([_t(1, "eng", 6)], {}) is None  # off


def test_the_command():
    plan = compat_track_plan([_t(1, "eng", 6)], {**ON, "audio_compat_loudnorm": True})
    cmd = _build_ffmpeg_cmd_impl("/m/in.mkv", "/m/out.mkv", encoder="libx265", compat_track=plan)
    i = cmd.index("0:1")
    assert cmd[i - 1] == "-map" and cmd.index("0:a?") < i  # after the original audio
    tail = " ".join(cmd[i:])
    assert "-c:a:1 eac3 -b:a 224k -ac:a:1 2 -ar:a:1 48000 -filter:a:1 loudnorm=I=-16:TP=-1.5:LRA=11" in tail
    assert f"-metadata:s:a:1 title={COMPAT_TRACK_TITLE} -metadata:s:a:1 language=eng -disposition:a:1 0" in tail


def test_lossy_dts_converts_when_asked():
    assert not lossless_to_convert("dts", "DTS")
    assert lossless_to_convert("dts", "DTS", convert_dts=True)
    assert not lossless_to_convert("dts", "DTS-HD MA + DTS:X", convert_dts=True)  # objects stay
    assert not lossless_to_convert("ac3", "", convert_dts=True)


def test_scans_keep_the_compatibility_track():
    """A second track in a language is otherwise left to the standard rules
    once the best one is chosen — here: removable."""
    from backend import scanner
    tracks = [_t(1, "eng", 6, "eac3"), {**_t(2, "eng", 2, "aac"), "title": COMPAT_TRACK_TITLE}, _t(3, "eng", 2, "aac")]
    rules = scanner.TrackRules(audio_keep=frozenset({"eng"}), keep_native_audio=False, dedup=True)
    kept = scanner.classify_audio_tracks(tracks, "ice", 60, rules=rules)
    assert [t.keep for t in kept] == [True, True, False]


def _encoders() -> str:
    if not shutil.which("ffmpeg"):
        return ""
    return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


async def _settings(db_path, **values):
    async with aiosqlite.connect(db_path) as db:
        for k, v in values.items():
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()


def _streams(path):
    return json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_name,channels,sample_rate:stream_tags=title", "-of", "json", path],
        capture_output=True, text=True, check=True).stdout)["streams"]


@pytest.mark.asyncio
@pytest.mark.skipif(not all(e in _encoders() for e in ("libx265", " aac ", "eac3")), reason="needs ffmpeg encoders")
async def test_a_real_encode_adds_it(test_db, tmp_path):
    from backend.converter import convert_file
    clip = tmp_path / "Film (2020).mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                    "-c:v", "libx264", "-qp", "0", "-c:a", "aac", "-ac", "6", str(clip)], check=True)
    await _settings(test_db, audio_compat_track="true", audio_compat_codec="aac", audio_compat_bitrate="160",
                    audio_compat_loudnorm="true")
    result = await convert_file(str(clip), "libx265", 2.0, override_libx265_preset="ultrafast")
    assert result["success"], result.get("error")
    streams = _streams(result["output_path"])
    assert [(s["codec_name"], s["channels"]) for s in streams] == [("aac", 6), ("aac", 2)]
    assert streams[1]["tags"]["title"] == COMPAT_TRACK_TITLE and streams[1]["sample_rate"] == "48000"


@pytest.mark.asyncio
@pytest.mark.skipif(not all(e in _encoders() for e in ("libx265", " dca ", "eac3")), reason="needs ffmpeg encoders")
async def test_a_real_encode_converts_dts(test_db, tmp_path):
    from backend.converter import convert_file
    clip = tmp_path / "Film (2020).mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                    "-c:v", "libx264", "-qp", "0", "-c:a", "dca", "-strict", "-2", "-ac", "6", str(clip)], check=True)
    await _settings(test_db, auto_convert_lossless="true", lossless_target_codec="eac3",
                    lossless_target_bitrate="384", convert_dts="true")
    result = await convert_file(str(clip), "libx265", 2.0, override_libx265_preset="ultrafast")
    assert result["success"], result.get("error")
    assert [s["codec_name"] for s in _streams(result["output_path"])] == ["eac3"]
