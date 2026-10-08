"""v0.10.0 (M4): two discs in one movie folder got the same output name.

An ISO inside a movie folder is named after the folder, so "Disc 1.iso" and
"Disc 2.iso" both became "<Movie> 480p DVDRip … .mkv"; the second conversion
replaced the first one's output (whose original was already disposed). More
generally nothing stopped an encode from replacing an unrelated file that
already had the output's name.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.converter import build_disc_output_filename, convert_file

PROBE = {"video_width": 720, "video_height": 480, "audio_tracks": [{"codec": "ac3", "channels": 6}]}


@pytest.mark.asyncio
async def test_isos_sharing_a_folder_are_named_after_themselves(test_db, tmp_path):
    folder = tmp_path / "media" / "Movie (1999)"
    folder.mkdir(parents=True)
    for name in ("Movie (1999) Disc 1.iso", "Movie (1999) Disc 2.iso"):
        (folder / name).write_bytes(b"\0" * 2048)
    a = await build_disc_output_filename(str(folder / "Movie (1999) Disc 1.iso"), "dvd", PROBE, encoder="nvenc")
    b = await build_disc_output_filename(str(folder / "Movie (1999) Disc 2.iso"), "dvd", PROBE, encoder="nvenc")
    assert Path(a).name == "Movie (1999) Disc 1 480p DVDRip AC3 5.1 h265.mkv"
    assert Path(b).name == "Movie (1999) Disc 2 480p DVDRip AC3 5.1 h265.mkv"


@pytest.mark.asyncio
async def test_single_iso_still_named_after_the_folder(test_db, tmp_path):
    folder = tmp_path / "media" / "Movie (1999)"
    folder.mkdir(parents=True)
    (folder / "MOVIE_DVD.iso").write_bytes(b"\0" * 2048)
    out = await build_disc_output_filename(str(folder / "MOVIE_DVD.iso"), "dvd", PROBE, encoder="nvenc")
    assert Path(out).name == "Movie (1999) 480p DVDRip AC3 5.1 h265.mkv"


def _has_libx265() -> bool:
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "libx265" in out


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_libx265(), reason="needs ffmpeg with libx265")
async def test_an_existing_file_at_the_output_name_is_never_replaced(test_db, tmp_path):
    src = tmp_path / "Show - S01E01 - 1080p WEB h264.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=2",
                    "-c:v", "libx264", "-preset", "ultrafast", str(src)], check=True)
    taken = tmp_path / "Show - S01E01 - 1080p WEB x265.mkv"
    taken.write_bytes(b"someone else's encode")

    result = await convert_file(str(src), "libx265", 2.0, override_libx265_preset="ultrafast")

    assert result["success"] is False
    assert result["error_key"] == "errors.outputExists"
    assert taken.read_bytes() == b"someone else's encode"
    assert src.exists()
