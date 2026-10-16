"""A file that no longer needs converting has nothing left to save (v0.10.0).

The Scanner's Savings sort adds up each title's stored estimate
(scan_results.video_conv_savings_bytes). The scan stores 0 for a file that
doesn't need converting, but the paths that later mark a file converted (or
its codec no longer converted) kept the estimate it had before, so titles
already converted kept their old savings and sorted first."""
import aiosqlite
import pytest

GB = 1024 ** 3


async def _savings(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path, needs_conversion, video_conv_savings_bytes "
                              "FROM scan_results ORDER BY file_path") as cur:
            return await cur.fetchall()


@pytest.mark.asyncio
async def test_a_conversion_clears_the_estimate(test_db, tmp_path, monkeypatch):
    import backend.scanner as scanner
    from backend.queue import JobQueue, refresh_converted_scan_row
    src = tmp_path / "Film (2009).mkv"
    src.write_bytes(b"converted")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, "
                         "video_conv_savings_bytes, audio_tracks_json, scan_timestamp) "
                         "VALUES (?, ?, 'h264', 1, ?, '[]', 't')", (str(src), 4 * GB, 2 * GB))
        await db.commit()

    async def probe(path, *a, **kw):
        return {"video_codec": "hevc", "audio_tracks": [], "subtitle_tracks": [], "duration": 60, "file_size": 1}
    monkeypatch.setattr(scanner, "probe_file", probe)
    job_id = await JobQueue(test_db).add_job(str(src), "convert", encoder="libx265")
    await refresh_converted_scan_row(test_db, job_id, str(src), str(src))

    assert await _savings(test_db) == [(str(src), 0, 0)]


@pytest.mark.asyncio
async def test_a_codec_no_longer_converted_has_nothing_to_save(test_db):
    from backend.scanner import recompute_needs_conversion
    async with aiosqlite.connect(test_db) as db:
        for path, codec in (("/m/a.mkv", "h264"), ("/m/b.mkv", "mpeg2video")):
            await db.execute("INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, "
                             "video_conv_savings_bytes, scan_timestamp) VALUES (?, ?, ?, 1, ?, 't')",
                             (path, 4 * GB, codec, 2 * GB))
        await db.commit()
        assert await recompute_needs_conversion(db, ["h264"]) == 1
        await db.commit()

    assert await _savings(test_db) == [("/m/a.mkv", 1, 2 * GB), ("/m/b.mkv", 0, 0)]


@pytest.mark.asyncio
async def test_start_up_clears_the_estimates_converted_files_kept(test_db):
    from backend.database import init_db
    async with aiosqlite.connect(test_db) as db:
        await db.execute("DELETE FROM settings WHERE key = '_v0_10_0_clear_converted_savings'")
        for path, needs, converted, saving in (("/m/converted.mkv", 0, 1, 2 * GB), ("/m/todo.mkv", 1, 0, 3 * GB)):
            await db.execute("INSERT INTO scan_results (file_path, file_size, needs_conversion, converted, "
                             "video_conv_savings_bytes, scan_timestamp) VALUES (?, ?, ?, ?, ?, 't')",
                             (path, 4 * GB, needs, converted, saving))
        await db.commit()

    await init_db()

    assert await _savings(test_db) == [("/m/converted.mkv", 0, 0), ("/m/todo.mkv", 1, 3 * GB)]
