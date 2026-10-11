"""Width, frame rate, aspect ratio and the other video details for files
scanned before 0.10.0, which stored only the height: read when a file's panel
opens, and for the rest in the background, without a full rescan."""
import shutil
import subprocess

import aiosqlite
import pytest

import backend.scanner as scanner
from backend import video_details

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                                  reason="needs ffmpeg")

PROBE = {"video_codec": "h264", "video_width": 1920, "video_height": 800, "video_fps": 23.976,
         "video_pix_fmt": "yuv420p10le", "video_interlaced": False, "video_vfr": None, "video_dar": "12:5",
         "video_bitrate": 5000000, "hdr_format": "hdr10", "audio_tracks": [], "subtitle_tracks": [],
         "duration": 60, "file_size": 1}


async def _row(db_path, path, **cols):
    cols = {"file_size": 1, "video_codec": "h264", "needs_conversion": 1, "audio_tracks_json": "[]",
            "scan_timestamp": "2026-10-11", "video_height": 800, **cols}
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(f"INSERT INTO scan_results ({', '.join(cols)}, file_path) "
                               f"VALUES ({', '.join('?' * len(cols))}, ?)", [*cols.values(), path])
        await db.commit()
        return cur.lastrowid


async def _details(db_path, path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT video_width, video_fps, video_dar, video_bit_depth, hdr_format "
                              "FROM scan_results WHERE file_path = ?", (path,)) as cur:
            return await cur.fetchone()


def _fake_probe(monkeypatch, probe=PROBE):
    calls = []

    async def fake(path, detect_und_subs=True):
        calls.append((path, detect_und_subs))
        return None if "missing" in path else dict(probe)
    monkeypatch.setattr(scanner, "probe_file", fake)
    return calls


def test_the_columns_a_probe_gives():
    assert video_details.detail_columns(PROBE) == {
        "video_width": 1920, "video_height": 800, "hdr_format": "hdr10", "video_fps": 23.976,
        "video_bit_depth": 10, "video_interlaced": 0, "video_vfr": None, "video_dar": "12:5",
        "video_bitrate": 5000000}
    # Unknown is NULL, as a scan stores it.
    bare = video_details.detail_columns({"video_width": 640, "video_height": 480})
    assert (bare["video_fps"], bare["video_bit_depth"], bare["video_interlaced"], bare["video_dar"]) \
        == (None, None, None, None)


@pytest.mark.asyncio
async def test_opening_a_file_reads_what_its_scan_didnt(test_db, monkeypatch):
    calls = _fake_probe(monkeypatch)
    await _row(test_db, "/m/Film (2009)/Film.mkv")
    got = await video_details.read_details("/m/Film (2009)/Film.mkv")
    assert (got["video_width"], got["video_fps"], got["video_dar"]) == (1920, 23.976, "12:5")
    assert got["resolution"] == "1080p"  # 1920 x 800 is 1080p, by its width
    assert await _details(test_db, "/m/Film (2009)/Film.mkv") == (1920, 23.976, "12:5", 10, "hdr10")
    # Just the probe: no subtitle language detection (it reads 30 minutes of the file).
    assert calls == [("/m/Film (2009)/Film.mkv", False)]


@pytest.mark.asyncio
async def test_a_file_that_has_them_isnt_probed(test_db, monkeypatch):
    calls = _fake_probe(monkeypatch)
    await _row(test_db, "/m/a.mkv", video_width=1280, video_height=720, video_fps=25.0)
    await _row(test_db, "/m/no-rate.mkv", video_width=1920, video_height=800)  # the file doesn't say
    await _row(test_db, "/m/BDMV/index.bdmv", disc_type="bdmv")  # a disc: minutes over the network
    await _row(test_db, "/m/bad.mkv", probe_status="corrupt")
    got = await video_details.read_details("/m/a.mkv")
    assert (got["video_width"], got["video_fps"], got["resolution"]) == (1280, 25.0, "720p")
    assert (await video_details.read_details("/m/BDMV/index.bdmv"))["video_width"] == 0
    assert (await video_details.read_details("/m/bad.mkv"))["video_width"] == 0
    assert (await video_details.read_details("/m/no-rate.mkv"))["video_fps"] is None
    assert calls == []
    # Only files in the list: never a path someone makes up.
    assert await video_details.read_details("/etc/passwd") is None


@pytest.mark.asyncio
async def test_a_failed_probe_keeps_what_was_there(test_db, monkeypatch):
    _fake_probe(monkeypatch)
    await _row(test_db, "/m/missing.mkv")
    got = await video_details.read_details("/m/missing.mkv")
    assert (got["video_width"], got["video_fps"]) == (0, None)


@pytest.mark.asyncio
async def test_the_background_fill_reads_each_old_file_once(test_db, monkeypatch):
    calls = _fake_probe(monkeypatch)
    await _row(test_db, "/m/old1.mkv")
    await _row(test_db, "/m/done.mkv", video_width=1280, video_height=720, video_fps=25.0)
    await _row(test_db, "/m/missing.mkv")  # gone from disk: tried once, not again
    await _row(test_db, "/m/BDMV/index.bdmv", disc_type="bdmv")
    await _row(test_db, "/m/hidden.mkv", removed_from_list=1)
    # Converted on 0.10: the conversion stored the frame rate, not the width.
    await _row(test_db, "/m/old2.mkv", video_fps=25.0, video_bit_depth=8)
    # Scanned on 0.10, the file just doesn't say its frame rate: not again.
    await _row(test_db, "/m/no-rate.mkv", video_width=1920, video_height=800)
    assert await video_details.fill_missing(batch=2) == 2
    assert sorted(p for p, _ in calls) == ["/m/missing.mkv", "/m/old1.mkv", "/m/old2.mkv"]
    assert await _details(test_db, "/m/old1.mkv") == (1920, 23.976, "12:5", 10, "hdr10")
    assert await _details(test_db, "/m/old2.mkv") == (1920, 23.976, "12:5", 10, "hdr10")

    # A restart: nothing left, nothing probed again, and a file added since is read.
    calls.clear()
    assert await video_details.fill_missing() == 0
    assert calls == []
    await _row(test_db, "/m/old3.mkv")
    assert await video_details.fill_missing() == 1
    assert [p for p, _ in calls] == ["/m/old3.mkv"]


@pytest.mark.asyncio
async def test_the_fill_waits_while_a_scan_runs(test_db, monkeypatch):
    calls = _fake_probe(monkeypatch)
    await _row(test_db, "/m/old.mkv")
    busy = iter([True, True, False])
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        assert calls == []  # nothing probed while the scan runs
    monkeypatch.setattr(video_details.asyncio, "sleep", sleep)
    assert await video_details.fill_missing(busy=lambda: next(busy, False)) == 1
    assert waits == [60, 60]


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_real_file(test_db, tmp_path):
    path = tmp_path / "Show.S01E01.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=2",
                    "-c:v", "libx264", str(path)], check=True)
    await _row(test_db, str(path), video_height=180)
    got = await video_details.read_details(str(path))
    assert (got["video_width"], got["video_height"], got["video_fps"], got["video_dar"], got["video_bit_depth"]) \
        == (320, 180, 25.0, "16:9", 8)


@pytest.mark.asyncio
async def test_the_endpoint(test_db, monkeypatch):
    from backend.api_errors import ApiError
    from backend.routes.scan import VideoDetailsRequest, read_video_details
    _fake_probe(monkeypatch)
    await _row(test_db, "/m/Film.mkv")
    got = await read_video_details(VideoDetailsRequest(file_path="/m/Film.mkv"))
    assert got["video_width"] == 1920
    with pytest.raises(ApiError) as err:
        await read_video_details(VideoDetailsRequest(file_path="/m/nope.mkv"))
    assert err.value.status_code == 404
