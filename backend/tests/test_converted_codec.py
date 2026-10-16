"""A converted file's codec is the one its output has (v0.10.0).

The paths that point a scan row at a conversion's output wrote
video_codec = 'hevc', from before a conversion could output AV1 (the
output_codec setting, or a rule's). An AV1 output was labelled and filtered
as HEVC in the Scanner until a rescan."""
import json
from types import SimpleNamespace

import aiosqlite
import pytest

from backend.queue import JobQueue, QueueWorker, refresh_converted_scan_row


async def _codec(db_path, path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT video_codec FROM scan_results WHERE file_path = ?", (str(path),)) as cur:
            row = await cur.fetchone()
    return row and row[0]


async def _row(db_path, path, codec="h264", converted=0):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, converted, "
                         "audio_tracks_json, scan_timestamp) VALUES (?, 1, ?, 0, ?, '[]', 't')",
                         (str(path), codec, converted))
        await db.commit()


def _probe(monkeypatch, codecs):
    """probe_file reads the files in `codecs` ({path: video codec}) and fails on any other."""
    import backend.queue as queue_mod
    import backend.scanner as scanner

    async def probe(path, *a, **kw):
        if str(path) not in codecs:
            return None
        return {"video_codec": codecs[str(path)], "audio_tracks": [], "subtitle_tracks": [],
                "duration": 60, "file_size": 1}

    async def no_wait(_):
        return None
    monkeypatch.setattr(scanner, "probe_file", probe)
    monkeypatch.setattr(queue_mod.asyncio, "sleep", no_wait)


@pytest.mark.asyncio
async def test_a_conversion_records_the_codec_its_output_has(test_db, tmp_path, monkeypatch):
    src = tmp_path / "Film (2009).mkv"
    src.write_bytes(b"converted")
    await _row(test_db, src)
    _probe(monkeypatch, {str(src): "av1"})
    job_id = await JobQueue(test_db).add_job(str(src), "convert", encoder="libx265")

    await refresh_converted_scan_row(test_db, job_id, str(src), str(src))

    assert await _codec(test_db, src) == "av1"


@pytest.mark.asyncio
async def test_an_output_it_cannot_probe_gets_the_codec_the_job_encoded(test_db, tmp_path, monkeypatch):
    src = tmp_path / "Film (2009).mkv"
    src.write_bytes(b"converted")
    await _row(test_db, src)
    _probe(monkeypatch, {})
    job_id = await JobQueue(test_db).add_job(str(src), "convert", encoder="nvenc")

    await refresh_converted_scan_row(test_db, job_id, str(src), str(src), output_codec="av1")

    assert await _codec(test_db, src) == "av1"


@pytest.mark.asyncio
async def test_the_jobs_last_update_records_the_codec_it_encoded(test_db, tmp_path, monkeypatch):
    """The update made as the output is placed didn't land: the one at the
    end of the job moves the row, with the codec the converter reported."""
    import backend.converter as converter
    import backend.queue as queue_mod
    src = tmp_path / "Film (2009) x264.mkv"
    out = tmp_path / "Film (2009) AV1.mkv"
    src.write_bytes(b"source")
    await _row(test_db, src)
    _probe(monkeypatch, {str(src): "h264"})

    async def not_landed(*a, **kw):
        return None

    async def converted(**kwargs):
        out.write_bytes(b"converted")
        src.unlink()
        return {"success": True, "output_path": str(out), "space_saved": 1, "error": None,
                "encoding_stats": {"output_codec": "av1"}}
    monkeypatch.setattr(queue_mod, "refresh_converted_scan_row", not_landed)
    monkeypatch.setattr(converter, "convert_file", converted)
    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(src), "convert", encoder="libx265")
    job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)

    await QueueWorker(test_db)._process_job(job)

    assert await _codec(test_db, out) == "av1"


@pytest.mark.asyncio
async def test_a_remote_conversion_records_the_codec_the_worker_encoded(test_db, monkeypatch):
    import backend.arr as arr
    import backend.nodes as nodes
    import backend.routes.nodes as nodes_route
    from backend import bazarr
    monkeypatch.setattr(nodes, "DB_PATH", test_db)

    async def no_token(*a, **kw):
        return None

    async def no_rescan(path):
        return {}
    monkeypatch.setattr(nodes_route, "_require_node_token", no_token)
    monkeypatch.setattr(arr, "trigger_arr_rescan", no_rescan)
    monkeypatch.setattr(bazarr, "schedule_rescan", lambda path, delay=60: None)
    _probe(monkeypatch, {})  # the server can't read the output
    await _row(test_db, "/media/Film.mkv")
    job_id = await JobQueue(test_db).add_job("/media/Film.mkv", "convert", encoder="nvenc")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO worker_nodes (id, name, status, registered_at, path_mappings) VALUES "
                         "('n1', 'n1', 'online', 'x', ?)", (json.dumps([{"server": "/media", "worker": "/mnt"}]),))
        await db.execute("UPDATE jobs SET status = 'running', assigned_node_id = 'n1' WHERE id = ?", (job_id,))
        await db.commit()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(node_manager=nodes.NodeManager())))

    await nodes_route.report_complete(nodes_route.CompletionReport(
        node_id="n1", job_id=job_id, success=True, output_path="/mnt/Film AV1.mkv", space_saved=500,
        replaced_source=True, encoding_stats={"output_codec": "av1"}), request)

    assert await _codec(test_db, "/media/Film AV1.mkv") == "av1"


@pytest.mark.asyncio
async def test_retry_records_the_codec_of_the_output_it_finds(test_db, tmp_path, monkeypatch):
    from backend.converter import rename_x264_to_x265
    from backend.routes.jobs import init_job_routes, retry_job
    src = tmp_path / "Film (2009) x264.mkv"
    out = tmp_path / rename_x264_to_x265(src.name)
    out.write_bytes(b"converted in a run that failed afterwards")
    await _row(test_db, src)
    _probe(monkeypatch, {str(out): "av1"})
    queue = JobQueue(test_db)
    init_job_routes(QueueWorker(test_db), queue)
    job_id = await queue.add_job(str(src), "convert", encoder="libx265")
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE jobs SET status = 'failed' WHERE id = ?", (job_id,))
        await db.commit()

    assert (await retry_job(job_id))["status"] == "completed"
    assert await _codec(test_db, out) == "av1"


@pytest.mark.asyncio
async def test_start_up_relabels_the_files_converted_to_av1(test_db):
    from backend.database import init_db
    for path in ("/m/av1.mkv", "/m/hevc.mkv", "/m/kept.mkv", "/m/again.mkv", "/m/cleaned.mkv"):
        await _row(test_db, path, codec="hevc", converted=1)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("DELETE FROM settings WHERE key = '_v0_10_0_av1_converted_codec'")

        async def job(path, job_type, stats, completed, finalized=True, saved=1):
            await db.execute(
                "INSERT INTO jobs (file_path, job_type, status, created_at, completed_at, finalized_at, "
                "space_saved, encoding_stats) VALUES (?, ?, 'completed', 't', ?, ?, ?, ?)",
                (path, job_type, completed, completed if finalized else None, saved, stats and json.dumps(stats)))
        await job("/m/av1.mkv", "convert", {"output_codec": "av1"}, "2026-10-01")
        await job("/m/hevc.mkv", "convert", {"output_codec": "hevc"}, "2026-10-01")
        # Converted to HEVC before v0.10.0 (no finalized_at then), then an AV1
        # encode thrown away (larger, VMAF): the HEVC file stayed.
        await job("/m/kept.mkv", "convert", None, "2025-01-01", finalized=False)
        await job("/m/kept.mkv", "convert", {"output_codec": "av1"}, "2026-10-01", finalized=False, saved=0)
        # Converted to AV1, then again to HEVC.
        await job("/m/again.mkv", "convert", {"output_codec": "av1"}, "2026-10-01")
        await job("/m/again.mkv", "convert", {"output_codec": "hevc"}, "2026-10-02")
        # Converted to AV1, then its audio cleaned up.
        await job("/m/cleaned.mkv", "combined", {"output_codec": "av1"}, "2026-10-01")
        await job("/m/cleaned.mkv", "audio", {"removed_audio_count": 1}, "2026-10-02")
        await db.commit()

    await init_db()

    async with aiosqlite.connect(test_db) as db:
        async with db.execute("SELECT file_path, video_codec FROM scan_results ORDER BY file_path") as cur:
            assert await cur.fetchall() == [("/m/again.mkv", "hevc"), ("/m/av1.mkv", "av1"),
                                            ("/m/cleaned.mkv", "av1"), ("/m/hevc.mkv", "hevc"),
                                            ("/m/kept.mkv", "hevc")]
