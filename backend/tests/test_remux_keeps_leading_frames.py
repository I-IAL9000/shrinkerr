"""A stream-copy remux keeps the video before the first keyframe (v0.10.0).

A broadcast recording starts wherever the capture did, mid-GOP. ffmpeg's
stream copy drops every video packet before the first keyframe unless told
to keep them (-copyinkf): a clean-up of a season of Dragons' Den (1080p
HDTV h264) cut the start of each episode's video, and the health check then
read "mmco: unref short failure" — the frames after the first keyframe
refer to pictures the remux had removed — and marked them all corrupt."""
import json
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _capture(tmp_path):
    """An MKV of a recording that starts at an arbitrary transport stream
    packet: its first video packets aren't a keyframe."""
    ts = tmp_path / "broadcast.ts"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=6",
                    "-f", "lavfi", "-i", "sine=d=6", "-c:v", "libx264", "-preset", "ultrafast", "-x264-params",
                    "keyint=50:min-keyint=50:scenecut=0:bframes=3:repeat-headers=1", "-c:a", "ac3",
                    "-f", "mpegts", str(ts)], check=True)
    data = ts.read_bytes()
    start = int(len(data) // 188 * 0.45) * 188  # about 2.7 s in, between keyframes
    capture = tmp_path / "capture.ts"
    capture.write_bytes(data[start:])
    src = tmp_path / "Dragons' Den - S21E01 - Episode 1 - 1080p HDTV AC3 2.0 h264-FTP.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(capture), "-map", "0", "-c", "copy", "-copyinkf",
                    str(src)], check=True, capture_output=True)
    ts.unlink()
    capture.unlink()
    assert _first_video_packet_is_key(src) is False  # the recording starts mid-GOP
    return src


def _video_packets(path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                          "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return int(out.strip())


def _first_video_packet_is_key(path) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#1",
                          "-show_entries", "packet=flags", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)["packets"][0]["flags"].startswith("K")


def _audio_index(path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout
    return int(out.split()[0])


@pytest.mark.asyncio
async def test_a_cleanup_keeps_the_frames_before_the_first_keyframe(test_db, tmp_path):
    from backend.audio import remux_audio
    src = _capture(tmp_path)
    before = _video_packets(src)

    result = await remux_audio(str(src), [_audio_index(src)], duration=3.3)

    assert result["success"], result.get("error")
    assert _video_packets(result["output_path"]) == before
    assert _first_video_packet_is_key(result["output_path"]) is False


def test_the_language_writer_keeps_them_too(tmp_path):
    from backend.language_detection import _build_metadata_remux_cmd
    src = _capture(tmp_path)
    out = tmp_path / "tagged.mkv"

    subprocess.run(_build_metadata_remux_cmd(str(src), str(out), ["eng"], []), check=True, capture_output=True)

    assert _video_packets(out) == _video_packets(src)
