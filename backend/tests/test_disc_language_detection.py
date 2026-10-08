"""v0.10.0: spoken-language detection on a Blu-ray / DVD folder handed
ffmpeg the disc's marker file (VIDEO_TS.IFO / index.bdmv) instead of the
video, so it failed on every disc track and they all stayed "und".
"""
import os
import shutil
import subprocess
import wave

import pytest

from backend.language_detection import _extract_audio_clip
from backend.scanner import probe_file
from backend.tests.test_disc_release_folder import _dvd

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


@pytest.mark.asyncio
async def test_audio_clip_is_read_from_the_disc_not_its_marker(tmp_path):
    marker = _dvd(tmp_path / "Fast-Walking (1982)")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=720x480:rate=30:duration=4",
         "-f", "lavfi", "-i", "sine=duration=4", "-c:v", "mpeg2video", "-b:v", "6M",
         "-c:a", "ac3", "-ac", "2", "-f", "vob", str(marker.parent / "VTS_01_1.VOB")],
        check=True,
    )
    probe = await probe_file(str(marker))
    audio = probe["audio_tracks"][0]["stream_index"]

    clip = await _extract_audio_clip(str(marker), audio, 1.0)
    try:
        with wave.open(clip) as w:
            assert w.getframerate() == 16000 and w.getnframes() > 16000  # over a second of audio
    finally:
        os.unlink(clip)
