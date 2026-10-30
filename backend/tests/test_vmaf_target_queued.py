"""A queued job keeps the VMAF target it was queued with (v0.10.0).

The target used to be read from Settings as each job started, so turning it
on changed every job already in the queue: 7,745 jobs queued at P6 / CQ 36
started searching. Now a conversion records the target when it's queued — a
rule's own, else Settings' then (0: off) — and a job queued before that has
none and keeps its own quality.
"""
import aiosqlite
import pytest

from backend.queue import JobQueue
from backend.rule_actions import queued_settings


async def _settings(db_path, **values):
    async with aiosqlite.connect(db_path) as db:
        for k, v in values.items():
            await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
        await db.commit()


async def _targets(db_path):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path, vmaf_target FROM jobs") as cur:
            return dict(await cur.fetchall())


@pytest.mark.asyncio
async def test_conversions_keep_settings_target_from_when_they_were_queued(test_db):
    queue = JobQueue(test_db)
    await _settings(test_db, vmaf_target_enabled="true", vmaf_target_score="93")
    await queue.add_jobs_bulk([
        {"file_path": "/m/a.mkv", "job_type": "convert"},
        {"file_path": "/m/b.mkv", "job_type": "combined"},
        {"file_path": "/m/c.mkv", "job_type": "audio"},          # no conversion: no target
        {"file_path": "/m/d.mkv", "job_type": "convert", "vmaf_target": 95},  # a rule's own
        {"file_path": "/m/e.mkv", "job_type": "convert", "vmaf_target": 0},   # a rule's Off
    ])
    await queue.add_job("/m/f.mkv", "convert")
    await _settings(test_db, vmaf_target_enabled="false")  # turned off later: the queue keeps its targets
    await queue.add_job("/m/g.mkv", "convert")
    await queue.add_jobs_bulk([{"file_path": "/m/h.mkv", "job_type": "convert"}])

    assert await _targets(test_db) == {
        "/m/a.mkv": 93.0, "/m/b.mkv": 93.0, "/m/c.mkv": None, "/m/d.mkv": 95.0, "/m/e.mkv": 0.0,
        "/m/f.mkv": 93.0, "/m/g.mkv": 0.0, "/m/h.mkv": 0.0}


def test_a_queued_job_runs_with_the_target_it_was_queued_with():
    assert queued_settings({"vmaf_target": 93})["vmaf_target_enabled"] is True
    assert queued_settings({"vmaf_target": 93})["vmaf_target_score"] == 93.0
    assert queued_settings({"vmaf_target": 0})["vmaf_target_enabled"] is False
    # queued before jobs recorded it: their own quality, whatever Settings say
    assert queued_settings({"vmaf_target": None})["vmaf_target_enabled"] is False
    assert queued_settings({})["vmaf_target_enabled"] is False
