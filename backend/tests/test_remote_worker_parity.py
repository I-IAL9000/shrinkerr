"""v0.10.0: remote workers follow the same safety rules as the local one.

Workers hard-coded "no backup, no trash" (so originals were permanently
deleted however the server was set up), ran health-check jobs as a no-op
reported as success, and a remote completion never updated the Scanner row
or the job's paths (H7) — the converted file kept showing as needing
conversion, with stale track data.
"""
import json
import shutil
from types import SimpleNamespace

import aiosqlite
import pytest

import backend.nodes as nodes
import backend.routes.nodes as nodes_route
from backend.nodes import NodeManager
from backend.queue import JobQueue


@pytest.fixture
def node_api(test_db, monkeypatch):
    monkeypatch.setattr(nodes, "DB_PATH", test_db)

    async def no_token(*args, **kwargs):
        return None

    monkeypatch.setattr(nodes_route, "_require_node_token", no_token)
    nm = NodeManager()
    return nm, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(node_manager=nm)))


async def _setup(db_path, settings: dict, mappings=None):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO worker_nodes (id, name, capabilities, status, registered_at, path_mappings) "
            "VALUES ('node-1', 'node-1', ?, 'online', '2026-10-08T00:00:00', ?)",
            (json.dumps(["libx265"]), json.dumps(mappings or [])))
        for k, v in settings.items():
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        await db.commit()


@pytest.mark.asyncio
async def test_workers_get_the_originals_policy_and_no_health_checks(node_api, test_db):
    nm, request = node_api
    await _setup(test_db, {"backup_original_days": "7", "trash_original_after_conversion": "false"})
    queue = JobQueue(test_db)
    await queue.add_job("/media/a.mkv", "health_check")
    await queue.add_job("/media/b.mkv", "convert", encoder="libx265")

    res = await nodes_route.request_job(nodes_route.RequestJobBody(node_id="node-1"), request)

    job = res["job"]
    assert job["file_path"] == "/media/b.mkv"
    assert job["backup_original_days"] == 7
    assert job["trash_original_after_conversion"] is False


@pytest.mark.asyncio
async def test_unmapped_backup_folder_falls_back_to_beside_the_file(node_api, test_db):
    nm, request = node_api
    await _setup(test_db, {"backup_original_days": "7", "backup_folder": "/backups"},
                 mappings=[{"server": "/media", "worker": "/mnt/media"}])
    await JobQueue(test_db).add_job("/media/b.mkv", "convert", encoder="libx265")

    job = (await nodes_route.request_job(nodes_route.RequestJobBody(node_id="node-1"), request))["job"]

    assert job["file_path"] == "/mnt/media/b.mkv"
    assert job["backup_folder"] == ""


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
async def test_remote_completion_updates_the_scan_row_and_job(node_api, test_db, tmp_path):
    from backend.tests.test_same_name_conversion import _mkv
    nm, request = node_api
    await _setup(test_db, {})
    src = tmp_path / "Movie (2009) 1080p x264.mkv"
    out = tmp_path / "Movie (2009) 1080p x265.mkv"
    _mkv(out, ["eng"])  # what the worker produced (the source is gone)
    tracks = [{"stream_index": 1, "language": "fre", "codec": "aac", "channels": 1, "keep": False},
              {"stream_index": 2, "language": "eng", "codec": "aac", "channels": 1, "keep": True}]
    async with aiosqlite.connect(test_db) as db:
        await db.execute(
            "INSERT INTO scan_results (file_path, file_size, video_codec, needs_conversion, audio_tracks_json, "
            "native_language, scan_timestamp, has_removable_tracks_flag) "
            "VALUES (?, 10000000, 'h264', 1, ?, 'eng', '2026-10-08T00:00:00', 1)", (str(src), json.dumps(tracks)))
        await db.commit()
    queue = JobQueue(test_db)
    job_id = await queue.add_job(str(src), "combined", encoder="libx265", audio_tracks_to_remove=[1])
    job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)
    assert await nm.assign_job_to_node("node-1", job) is not None

    await nodes_route.report_complete(nodes_route.CompletionReport(
        node_id="node-1", job_id=job_id, success=True, output_path=str(out), space_saved=5000,
        replaced_source=True), request)

    async with aiosqlite.connect(test_db) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM scan_results") as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        async with db.execute("SELECT file_path, original_file_path, status FROM jobs WHERE id = ?", (job_id,)) as cur:
            jrow = dict(await cur.fetchone())
    assert [r["file_path"] for r in rows] == [str(out)]
    assert rows[0]["video_codec"] == "hevc" and rows[0]["needs_conversion"] == 0
    assert [(t["stream_index"], t["language"]) for t in json.loads(rows[0]["audio_tracks_json"])] == [(1, "eng")]
    assert jrow == {"file_path": str(out), "original_file_path": str(src), "status": "completed"}
