"""v0.10.0: remote nodes only report on jobs they still have.

last_heartbeat is stored as ISO text ("…T12:00:00+00:00") and was compared
as a string against SQLite's datetime() ("… 11:55:00"): 'T' sorts after ' ',
so a dead node was only noticed once the UTC date changed. Its job then
went back to pending — and any node could report progress, failure or
completion for any job, so a node that had lost a job could mark it failed
(or completed) under whoever was running it by then.
"""
import json
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

import backend.routes.nodes as nodes_route
from backend.queue import JobQueue
from backend.tests.test_remote_worker_parity import node_api  # noqa: F401  (fixture)


def _ago(minutes):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


async def _node(db_path, node_id, heartbeat_minutes_ago, job_id=None):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO worker_nodes (id, name, capabilities, status, registered_at, last_heartbeat, current_job_id) "
            "VALUES (?, ?, ?, ?, '2026-10-08T00:00:00', ?, ?)",
            (node_id, node_id, json.dumps(["libx265"]), "working" if job_id else "online",
             _ago(heartbeat_minutes_ago), job_id))
        await db.commit()


async def _job(db_path, status="pending", node=None) -> int:
    queue = JobQueue(db_path)
    n = len(await queue.get_all_jobs())
    job_id = await queue.add_job(f"/media/Movie {n} (2009).mkv", "convert", encoder="libx265")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE jobs SET status = ?, assigned_node_id = ? WHERE id = ?", (status, node, job_id))
        await db.commit()
    return job_id


async def _state(db_path, job_id):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT status, assigned_node_id, progress FROM jobs WHERE id = ?", (job_id,)) as cur:
            return tuple(await cur.fetchone())


@pytest.mark.asyncio
async def test_a_dead_node_is_noticed_the_same_day(test_db, node_api):
    nm, _ = node_api
    nm._started_at -= 3600  # up for an hour
    dead = await _job(test_db, "running", "dead")
    alive = await _job(test_db, "running", "alive")
    await _node(test_db, "dead", 10, dead)
    await _node(test_db, "alive", 1, alive)
    assert await nm.release_stale_assignments(stale_timeout_seconds=300) == 1
    assert (await _state(test_db, dead))[0] == "pending"
    assert (await _state(test_db, alive))[0] == "running"


@pytest.mark.asyncio
async def test_a_node_that_lost_its_job_cannot_fail_it(test_db, node_api):
    _, request = node_api
    job_id = await _job(test_db, "running", "local")
    await _node(test_db, "node-1", 0)
    res = await nodes_route.report_complete(nodes_route.CompletionReport(
        node_id="node-1", job_id=job_id, success=False, error="cancelled"), request)
    assert res.get("ignored")
    assert await _state(test_db, job_id) == ("running", "local", 0)


@pytest.mark.asyncio
async def test_progress_from_a_node_that_lost_its_job_stops_it(test_db, node_api):
    _, request = node_api
    job_id = await _job(test_db, "running", "node-2")
    await _node(test_db, "node-1", 0)
    res = await nodes_route.report_progress(nodes_route.ProgressReport(
        node_id="node-1", job_id=job_id, progress=50), request)
    assert res["cancelled"] is True
    assert await _state(test_db, job_id) == ("running", "node-2", 0)


@pytest.mark.asyncio
async def test_a_released_job_goes_back_to_the_node_still_working_on_it(test_db, node_api):
    _, request = node_api
    job_id = await _job(test_db, "pending")
    await _node(test_db, "node-1", 0)
    res = await nodes_route.report_progress(nodes_route.ProgressReport(
        node_id="node-1", job_id=job_id, progress=50), request)
    assert res["cancelled"] is False
    assert await _state(test_db, job_id) == ("running", "node-1", 50)


@pytest.mark.asyncio
async def test_a_completion_after_release_is_kept(test_db, node_api):
    _, request = node_api
    job_id = await _job(test_db, "pending")
    await _node(test_db, "node-1", 0)
    await nodes_route.report_complete(nodes_route.CompletionReport(
        node_id="node-1", job_id=job_id, success=True, space_saved=0, replaced_source=False), request)
    assert (await _state(test_db, job_id))[0] == "completed"


@pytest.mark.asyncio
async def test_cancelling_a_job_on_a_remote_node_reaches_the_node(test_db, node_api):
    """M6: the Queue's Cancel only killed local ffmpeg processes, so a job
    running on a remote node kept encoding."""
    from backend.queue import QueueWorker
    from backend.routes.jobs import cancel_current_job, init_job_routes
    nm, request = node_api
    init_job_routes(QueueWorker(test_db), JobQueue(test_db))
    job_id = await _job(test_db, "running", "node-1")
    await _node(test_db, "node-1", 0, job_id)

    res = await cancel_current_job(request, job_id)

    assert res["status"] == "cancel_requested"
    progress = await nodes_route.report_progress(nodes_route.ProgressReport(
        node_id="node-1", job_id=job_id, progress=60), request)
    assert progress["cancelled"] is True


@pytest.mark.asyncio
async def test_a_dead_node_keeps_its_job_right_after_a_restart(test_db, node_api):
    """Every heartbeat is old right after a restart: a server down longer
    than the timeout gave running remote jobs to the local worker at once."""
    from backend.nodes import NodeManager
    job_id = await _job(test_db, "running", "node-1")
    await _node(test_db, "node-1", 10, job_id)
    assert await NodeManager().release_stale_assignments(stale_timeout_seconds=300) == 0
    assert (await _state(test_db, job_id))[0] == "running"


@pytest.mark.asyncio
async def test_a_remote_conversion_is_finalized(test_db, node_api, tmp_path, monkeypatch):
    import backend.queue as queue_mod
    from backend.queue import QueueWorker

    async def no_wait(_):
        return None

    monkeypatch.setattr(queue_mod.asyncio, "sleep", no_wait)  # the fake output can't be re-probed
    from backend.api_errors import ApiError
    from backend.routes.jobs import init_job_routes, retry_job
    _, request = node_api
    init_job_routes(QueueWorker(test_db), JobQueue(test_db))
    job_id = await _job(test_db, "running", "node-1")
    await _node(test_db, "node-1", 0, job_id)
    out = tmp_path / "Movie (2009) x265.mkv"
    out.write_bytes(b"x")
    await nodes_route.report_complete(nodes_route.CompletionReport(
        node_id="node-1", job_id=job_id, success=True, output_path=str(out), space_saved=10,
        replaced_source=True), request)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE jobs SET status = 'failed' WHERE id = ?", (job_id,))  # say a later step failed
        await db.commit()
    with pytest.raises(ApiError) as exc:
        await retry_job(job_id)
    assert exc.value.code == "jobs.retryAlreadyReplaced"


@pytest.mark.asyncio
async def test_a_worker_reports_success_when_the_cancel_came_too_late(tmp_path, monkeypatch):
    """A cancel during VMAF can't stop convert_file: it placed the output and
    disposed of the original, then the worker reported "cancelled", and the
    server let the job run again on the converted file."""
    import backend.converter
    import backend.scanner
    from backend import worker_mode
    from backend.tests.test_worker_audio_remux import PROBE, FakeClient

    class CancellingClient(FakeClient):
        async def report_progress(self, *a, **kw):
            return True

    src = tmp_path / "Movie (2009) h264.mkv"
    src.write_bytes(b"x")
    out = tmp_path / "Movie (2009) x265.mkv"

    async def fake_probe(path, *a, **kw):
        return dict(PROBE)

    async def fake_convert_file(**kwargs):
        await kwargs["progress_callback"](progress=100.0, step="VMAF analysis")
        out.write_bytes(b"converted")
        return {"success": True, "output_path": str(out), "space_saved": 10, "error": None}

    monkeypatch.setattr(backend.scanner, "probe_file", fake_probe)
    monkeypatch.setattr(backend.converter, "convert_file", fake_convert_file)
    client = CancellingClient()
    await worker_mode.execute_job(client, "node-1", {"id": 9, "file_path": str(src), "job_type": "convert",
                                                     "encoder": "libx265"}, ["libx265"])
    assert [(ok, kw.get("replaced_source")) for ok, kw in client.completed] == [(True, True)]
