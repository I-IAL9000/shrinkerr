"""v0.10.0 (H2): one job could run on the local worker and a remote node at
once. Both picked it with a SELECT, then the local claim overwrote the
remote's unconditionally and the remote's conditional claim ignored that it
matched no row: two encoders wrote the same temp file and raced to replace
the original.
"""
import aiosqlite
import pytest

import backend.nodes as nodes
from backend.nodes import NodeManager
from backend.queue import JobQueue


async def _job_row(db_path, job_id):
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT status, assigned_node_id FROM jobs WHERE id = ?", (job_id,)) as cur:
            return dict(await cur.fetchone())


@pytest.mark.asyncio
async def test_local_claim_only_takes_a_pending_job(test_db):
    queue = JobQueue(test_db)
    job_id = await queue.add_job("/media/a.mkv", "convert")
    assert await queue.claim_job(job_id) is True
    assert await _job_row(test_db, job_id) == {"status": "running", "assigned_node_id": "local"}
    assert await queue.claim_job(job_id) is False


@pytest.mark.asyncio
async def test_local_claim_loses_to_a_remote_node(test_db, monkeypatch):
    monkeypatch.setattr(nodes, "DB_PATH", test_db)
    queue = JobQueue(test_db)
    job_id = await queue.add_job("/media/a.mkv", "convert")
    job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)
    assert await NodeManager().assign_job_to_node("node-1", job) is not None
    assert await queue.claim_job(job_id) is False
    assert await _job_row(test_db, job_id) == {"status": "running", "assigned_node_id": "node-1"}


@pytest.mark.asyncio
async def test_remote_claim_loses_to_the_local_worker(test_db, monkeypatch):
    monkeypatch.setattr(nodes, "DB_PATH", test_db)
    queue = JobQueue(test_db)
    job_id = await queue.add_job("/media/a.mkv", "convert")
    job = next(j for j in await queue.get_all_jobs() if j["id"] == job_id)
    assert await queue.claim_job(job_id) is True
    assert await NodeManager().assign_job_to_node("node-1", job) is None
    assert await _job_row(test_db, job_id) == {"status": "running", "assigned_node_id": "local"}
