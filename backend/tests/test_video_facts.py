"""Frame rate, bit depth, interlaced and variable frame rate (v0.10.0).

Read from the probe ffprobe already runs, stored on scan_results by every
scan path, and refreshed after a conversion: the Picture filters (10-bit,
Hi10P, interlaced, VFR) and Advanced Search's frame rate / bit depth."""
import shutil
import subprocess

import aiosqlite
import pytest

from backend.scanner import _frame_rate, bit_depth_of, probe_file, video_facts

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                                  reason="needs ffmpeg")


def test_the_helpers():
    assert [bit_depth_of(f) for f in ("yuv420p10le", "p010le", "yuv420p12le", "yuv420p", "yuv410p", "nv12", "")] \
        == [10, 10, 12, 8, 8, 8, 0]
    assert [_frame_rate(f) for f in ("24000/1001", "25", "0/0", "", None)] == [24000 / 1001, 25.0, 0.0, 0.0, 0.0]
    assert video_facts({"video_fps": 23.9760239, "video_pix_fmt": "yuv420p10le", "video_interlaced": True,
                        "video_dar": "16:9", "video_bitrate": 5000000}) == {
        "video_fps": 23.976, "video_bit_depth": 10, "video_interlaced": True, "video_vfr": None,
        "video_dar": "16:9", "video_bitrate": 5000000}


def _clip(path, *args, rate=24, seconds=2):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size=320x180:rate={rate}:duration={seconds}", *args, str(path)], check=True)
    return str(path)


@needs_ffmpeg
@pytest.mark.asyncio
async def test_the_probe_reads_them(tmp_path):
    inter = await probe_file(_clip(tmp_path / "i.mkv", "-c:v", "libx264", "-flags", "+ilme+ildct",
                                   "-field_order", "tt", rate=25))
    assert inter["video_interlaced"] is True and inter["video_fps"] == 25
    cfr = await probe_file(_clip(tmp_path / "c.mp4", "-c:v", "libx264"))
    assert (cfr["video_interlaced"], cfr["video_vfr"], cfr["video_dar"]) == (False, None, "16:9")
    assert cfr["video_bitrate"] and cfr["video_bitrate"] > 0
    # Frames dropped unevenly, in MP4 (which records the average rate).
    vfr = await probe_file(_clip(tmp_path / "v.mp4", "-vf", r"select='lt(mod(n\,24)\,12)+eq(mod(n\,48)\,30)'",
                                 "-fps_mode", "vfr", "-c:v", "libx264", seconds=4))
    assert vfr["video_vfr"] is True


@needs_ffmpeg
@pytest.mark.asyncio
async def test_ten_bit(tmp_path):
    try:
        path = _clip(tmp_path / "hi10.mkv", "-c:v", "libx264", "-pix_fmt", "yuv420p10le")
    except subprocess.CalledProcessError:
        pytest.skip("this libx264 has no 10-bit")
    assert video_facts(await probe_file(path))["video_bit_depth"] == 10


@pytest.mark.asyncio
async def test_a_scan_stores_them(test_db, monkeypatch):
    import backend.routes.scan as scan_route
    from backend.models import ScannedFile
    monkeypatch.setattr(scan_route, "DB_PATH", test_db)

    def scanned(path, **facts):
        return ScannedFile(file_path=path, file_name="f.mkv", folder_name="x", file_size=1, file_size_gb=0.0,
                           video_codec="h264", needs_conversion=True, native_language="eng",
                           has_removable_tracks=False, estimated_savings_bytes=0, estimated_savings_gb=0.0,
                           audio_tracks=[], **facts)
    scan_route._write_batch_sync(test_db, [
        scanned("/m/a.mkv", video_fps=29.97, video_bit_depth=10, video_interlaced=True, video_vfr=True,
                video_dar="2.39:1", video_bitrate=8000000, video_width=1920, video_height=800),
        scanned("/m/b.mkv", video_interlaced=False),  # nothing else known
    ], "2026-10-10")
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT file_path, video_fps, video_bit_depth, video_interlaced, video_vfr, video_dar, "
                              "video_bitrate FROM scan_results ORDER BY file_path") as cur:
            assert await cur.fetchall() == [("/m/a.mkv", 29.97, 10, 1, 1, "2.39:1", 8000000),
                                            ("/m/b.mkv", None, None, 0, None, None, None)]
    # The Scanner's file lists carry them, for the file panel.
    rows = await scan_route.get_scan_files("/m/")
    a = next(r for r in rows if r["file_path"] == "/m/a.mkv")
    assert (a["video_fps"], a["video_dar"], a["video_bitrate"], a["video_bit_depth"]) == (29.97, "2.39:1", 8000000, 10)
    assert a["resolution"] == "1080p"  # the resolution pills' tier: 1920 wide


@pytest.mark.asyncio
async def test_a_conversion_refreshes_them_from_the_output(test_db, tmp_path, monkeypatch):
    """An 8-bit interlaced source converted to 10-bit progressive HEVC."""
    import backend.scanner as scanner
    from backend.queue import JobQueue, refresh_converted_scan_row
    src = tmp_path / "Film (2009).mkv"
    src.write_bytes(b"converted")
    async with aiosqlite.connect(test_db) as db:
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, audio_tracks_json, "
            "scan_timestamp, video_fps, video_bit_depth, video_interlaced) "
            "VALUES (?, 1, 'h264', 1, '[]', '2026-10-10', 25, 8, 1)", (str(src),))
        await db.commit()

    async def probe(path, *a, **kw):
        return {"video_codec": "hevc", "video_pix_fmt": "yuv420p10le", "video_fps": 25.0, "video_interlaced": False,
                "video_vfr": None, "video_dar": "16:9", "video_bitrate": 3000000,
                "audio_tracks": [], "subtitle_tracks": [], "duration": 60, "file_size": 1}
    monkeypatch.setattr(scanner, "probe_file", probe)
    job_id = await JobQueue(test_db).add_job(str(src), "convert", encoder="libx265")
    await refresh_converted_scan_row(test_db, job_id, str(src), str(src))
    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT video_fps, video_bit_depth, video_interlaced, video_vfr, video_dar, video_bitrate "
                              "FROM scan_results") as cur:
            assert await cur.fetchone() == (25.0, 10, 0, None, "16:9", 3000000)


@pytest.mark.asyncio
async def test_the_watcher_reads_them_too(test_db, tmp_path):
    from backend.watcher import scanned_from_probe
    f = tmp_path / "new.mkv"
    f.write_bytes(b"x")
    s = await scanned_from_probe(str(f), {
        "video_codec": "h264", "video_pix_fmt": "yuv420p10le", "video_fps": 29.97, "video_interlaced": True,
        "video_vfr": True, "audio_tracks": [], "subtitle_tracks": [], "duration": 60, "file_size": 1}, ["h264"], 23)
    assert (s.video_fps, s.video_bit_depth, s.video_interlaced, s.video_vfr) == (29.97, 10, True, True)


@needs_ffmpeg
@pytest.mark.asyncio
async def test_a_full_scan_reads_them(test_db, tmp_path, monkeypatch):
    import backend.config
    import backend.scanner as scanner
    from backend.scanner import scan_directory
    monkeypatch.setattr(scanner, "settings", backend.config.settings)
    folder = tmp_path / "media" / "Show (2001)"
    folder.mkdir(parents=True)
    _clip(folder / "Show.S01E01.mkv", "-c:v", "libx264", "-flags", "+ilme+ildct", "-field_order", "tt", rate=25)
    [result] = await scan_directory(str(tmp_path / "media"))
    assert (result.video_fps, result.video_bit_depth, result.video_interlaced) == (25.0, 8, True)
