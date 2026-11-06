"""Audio bitrates ffprobe doesn't give (v0.10.0): AAC, Opus, FLAC and TrueHD
in an MKV have none in their headers, so their tracks showed no bitrate and
removing one counted as saving nothing. They're measured from a slice of
the file; files scanned before are measured once in the background."""
import json
import shutil
import subprocess

import aiosqlite
import pytest

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


def _film(path, codec: str, bitrate: str, seconds: int = 20):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=25:duration={seconds}",
         "-f", "lavfi", "-i", f"anoisesrc=duration={seconds}:amplitude=0.3", "-c:v", "libx264", "-preset", "ultrafast",
         "-c:a", codec, "-b:a", bitrate, "-ac", "2", str(path)],
        check=True)
    return path


@pytest.mark.asyncio
async def test_an_aac_track_in_an_mkv_is_measured(tmp_path):
    from backend.scanner import probe_file
    film = _film(tmp_path / "Show - S02E01 - 1080p WEBDL AAC 2.0 h264.mkv", "aac", "128k")
    [track] = (await probe_file(str(film), detect_und_subs=False))["audio_tracks"]
    assert 100_000 < track["bitrate"] < 160_000


@pytest.mark.asyncio
async def test_a_bitrate_ffprobe_gives_is_kept(tmp_path):
    from backend.scanner import probe_file
    film = _film(tmp_path / "Film (2001).mkv", "ac3", "192k")
    [track] = (await probe_file(str(film), detect_und_subs=False))["audio_tracks"]
    assert track["bitrate"] == 192_000


@pytest.mark.asyncio
async def test_files_scanned_before_are_measured_once(test_db, tmp_path, monkeypatch):
    import sys
    from backend.routes.scan import measure_missing_audio_bitrates_once
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    film = _film(tmp_path / "Show - S02E02.mkv", "aac", "128k")
    tracks = [{"stream_index": 1, "language": "eng", "codec": "aac", "bitrate": None, "size_estimate_bytes": None,
               "keep": True}]
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, duration, audio_tracks_json, "
                         "subtitle_tracks_json, scan_timestamp) VALUES (?, 1, 20.0, ?, '[]', 't')",
                         (str(film), json.dumps(tracks)))
        await db.commit()

    assert await measure_missing_audio_bitrates_once() == 1

    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT audio_tracks_json FROM scan_results") as cur:
            [track] = json.loads((await cur.fetchone())[0])
    assert 100_000 < track["bitrate"] < 160_000
    assert track["size_estimate_bytes"] == int(track["bitrate"] * 20.0 / 8)
    assert await measure_missing_audio_bitrates_once() == 0  # once per install
