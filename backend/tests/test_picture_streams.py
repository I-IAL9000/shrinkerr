"""SC-24: real Motion-JPEG videos were marked corrupt (the codec name decided
what counted as video), a cover image listed first became the file's video —
and the stream the encode took — and .flv/.webm/.mpg/camera codecs had no
family, so they could never be selected for conversion."""
import json
import shutil
import subprocess

import pytest

from backend.scanner import codec_matches_source, is_picture_stream

ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


@pytest.mark.parametrize("stream, picture", [
    ({"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}}, True),
    ({"codec_type": "video", "codec_name": "png", "nb_frames": "1"}, True),
    ({"codec_type": "video", "codec_name": "png", "tags": {"DURATION": "00:00:00.120000000"}}, True),
    ({"codec_type": "video", "codec_name": "png"}, True),
    ({"codec_type": "video", "codec_name": "mjpeg", "nb_frames": "50"}, False),     # camera AVI
    ({"codec_type": "video", "codec_name": "mjpeg", "tags": {"DURATION": "00:12:00.000000000"}}, False),
    ({"codec_type": "video", "codec_name": "mjpeg"}, False),
    ({"codec_type": "video", "codec_name": "h264", "nb_frames": "1"}, False),
    ({"codec_type": "audio", "codec_name": "mjpeg"}, False),
])
def test_picture_streams(stream, picture):
    assert is_picture_stream(stream) is picture


def test_the_new_codec_families_can_be_selected():
    for codec, family in (("vp8", "vp8"), ("mpeg1video", "mpeg1"), ("flv1", "flv"),
                          ("h263", "h263"), ("mjpeg", "mjpeg")):
        assert codec_matches_source(codec, [family])
        assert not codec_matches_source(codec, ["h264", "mpeg2", "mpeg4", "vc1"])  # defaults unchanged


def _run(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture
def cover_first(tmp_path):
    """An MKV whose first track is a one-frame PNG cover (no attached_pic
    flag), then the film."""
    cover = tmp_path / "cover.png"
    _run("-f", "lavfi", "-i", "color=red:s=60x90:d=1", "-frames:v", "1", str(cover))
    out = tmp_path / "Cover First (2020).mkv"
    _run("-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=2", "-i", str(cover),
         "-map", "1", "-map", "0", "-c:v:0", "png", "-c:v:1", "libx264", "-preset", "ultrafast", str(out))
    return out


@ffmpeg
@pytest.mark.asyncio
async def test_the_probe_skips_a_cover_listed_first(cover_first):
    from backend.scanner import probe_file
    probe = await probe_file(str(cover_first))
    assert (probe["video_codec"], probe["video_width"], probe["video_height"]) == ("h264", 320, 240)
    assert probe["video_stream_index"] == 1


@ffmpeg
@pytest.mark.asyncio
async def test_a_motion_jpeg_video_is_not_corrupt(tmp_path):
    from backend.scanner import probe_file
    clip = tmp_path / "MVI_0042.avi"
    _run("-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=2", "-c:v", "mjpeg", str(clip))
    probe = await probe_file(str(clip))
    assert probe is not None and probe["video_codec"] == "mjpeg"


def test_the_encode_maps_the_chosen_stream():
    from backend.converter import _build_ffmpeg_cmd_impl
    cmd = _build_ffmpeg_cmd_impl("/m/in.mkv", "/m/out.mkv", encoder="libx265", video_map="0:1")
    assert cmd[cmd.index("-map") + 1] == "0:1"
    cmd = _build_ffmpeg_cmd_impl("/m/in.mkv", "/m/out.mkv", encoder="libx265")
    assert cmd[cmd.index("-map") + 1] == "0:V:0"  # never an attached picture


def _has_libx265() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    return "libx265" in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                        capture_output=True, text=True).stdout


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_libx265(), reason="needs ffmpeg with libx265")
async def test_a_cover_first_file_converts_its_film(test_db, cover_first):
    from backend.converter import convert_file
    result = await convert_file(str(cover_first), "libx265", 2.0, override_libx265_preset="ultrafast")
    assert result["success"], result.get("error")
    out = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=codec_name,width,height",
         "-of", "json", result["output_path"]], capture_output=True, text=True, check=True).stdout)
    assert [(s["codec_name"], s["width"], s["height"]) for s in out["streams"]] == [("hevc", 320, 240)]


@ffmpeg
@pytest.mark.asyncio
async def test_an_audio_cleanup_keeps_the_film_not_the_cover(cover_first, tmp_path):
    """An MKV whose first track is a one-frame cover: the cleanup remux took
    the cover as the video, and its length check compared cover with cover."""
    from backend.audio import _probe_video_map, build_remux_cmd
    assert await _probe_video_map(str(cover_first)) == "0:1"
    out = tmp_path / "out.mkv"
    cmd = build_remux_cmd(str(cover_first), str(out), [], video_map=await _probe_video_map(str(cover_first)))
    subprocess.run(cmd, check=True, capture_output=True)
    streams = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=codec_name,width",
         "-of", "json", str(out)], capture_output=True, text=True, check=True).stdout)["streams"]
    assert [(s["codec_name"], s["width"]) for s in streams] == [("h264", 320)]
