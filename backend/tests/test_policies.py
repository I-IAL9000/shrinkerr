"""Policies and the planner (v0.10.0): a saved view + an action + a schedule
+ a budget, run on time or on demand; and the conversions that free N GB
fastest."""
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio

from backend.api_errors import ApiError
from backend.policies import within_budget

GB = 1024 ** 3
# name: (size, estimated savings, duration in seconds)
FILES = {"a": (10 * GB, 5 * GB, 7200), "b": (2 * GB, 1 * GB, 600), "c": (4 * GB, 3 * GB, 1200), "d": (6 * GB, 4 * GB, 3600)}


@pytest_asyncio.fixture
async def env(test_db, monkeypatch):
    import sys
    from backend.queue import JobQueue, QueueWorker
    from backend.routes.jobs import init_job_routes
    for name, module in list(sys.modules.items()):
        if name.startswith("backend.") and isinstance(getattr(module, "DB_PATH", None), str):
            monkeypatch.setattr(module, "DB_PATH", test_db)
    worker = QueueWorker(test_db)
    started = []
    monkeypatch.setattr(worker, "start_if_idle", lambda: started.append(True) or True)
    init_job_routes(worker, JobQueue(test_db))
    async with aiosqlite.connect(test_db) as db:
        for name, (size, savings, duration) in FILES.items():
            await db.execute(
                "INSERT INTO scan_results (file_path, file_size, duration, video_codec, needs_conversion, audio_tracks_json, "
                "scan_timestamp, video_conv_savings_bytes, health_checked_at) VALUES (?, ?, ?, 'h264', 1, '[]', 'x', ?, ?)",
                (f"/m/{name}.mkv", size, duration, savings, {"a": "2026-01-01", "c": "2025-01-01"}.get(name)))
        await db.execute("INSERT INTO saved_views (name, filter, created_at) VALUES ('Big x264', 'needs_conversion', 'x')")
        await db.execute("INSERT INTO jobs (file_path, job_type, status, created_at) VALUES ('/m/d.mkv', 'convert', 'pending', 'x')")
        await db.commit()
    return test_db, started


async def _policy(**kw) -> dict:
    from backend.routes import policies as route
    body = route.PolicyBody(**{"name": "Nightly", "view_id": 1, **kw})
    policy = await route.create(body)
    from backend.policies import unschedule
    unschedule(policy["id"])
    return policy


async def _queued(db_path) -> list[tuple]:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT file_path, job_type, priority FROM jobs WHERE file_path != '/m/d.mkv' "
                              "ORDER BY file_path") as cur:
            return await cur.fetchall()


def test_the_budget():
    rows = [{"file_size": s} for s in (10, 4, 2, 1)]
    assert within_budget(rows) == rows
    assert within_budget(rows, max_files=2) == rows[:2]
    assert within_budget(rows, max_bytes=6) == rows[1:3]  # 10 doesn't fit; 4 + 2 do; 1 more would pass 6
    assert within_budget(rows, max_files=1, max_bytes=6) == rows[1:2]


@pytest.mark.asyncio
async def test_a_policy_queues_the_best_savings_first(env):
    from backend.policies import run_policy
    db_path, started = env
    policy = await _policy(max_files=2)
    result = await run_policy(policy["id"])
    assert result == {"matched": 3, "chosen": 2, "queued": 2, "bytes": 14 * GB}  # d already waits
    assert [p for p, _, _ in await _queued(db_path)] == ["/m/a.mkv", "/m/c.mkv"]
    assert started == [True]
    assert (await run_policy(policy["id"]))["chosen"] == 1  # next time: what's left
    from backend.routes.policies import list_all
    [listed] = await list_all()
    assert listed["last_result"]["queued"] == 1 and listed["last_run_at"]


@pytest.mark.asyncio
async def test_a_size_budget(env):
    from backend.policies import run_policy
    db_path, started = env
    policy = await _policy(max_gb=5, start_queue=False)
    assert (await run_policy(policy["id"]))["chosen"] == 1
    assert [p for p, _, _ in await _queued(db_path)] == ["/m/c.mkv"]  # a (10 GB) never fits; c, then b would pass 5
    assert started == []


@pytest.mark.asyncio
async def test_a_health_check_policy(env):
    from backend.policies import run_policy
    db_path, _ = env
    policy = await _policy(action="health_check", max_files=2)
    await run_policy(policy["id"])
    assert await _queued(db_path) == [("/m/b.mkv", "health_check", 0), ("/m/c.mkv", "health_check", 0)]  # never, oldest


@pytest.mark.asyncio
async def test_a_policy_without_its_view(env):
    from backend.policies import run_policy
    db_path, _ = env
    policy = await _policy()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM saved_views")
        await db.commit()
    assert await run_policy(policy["id"]) == {"error": "viewMissing"}
    assert await _queued(db_path) == []


@pytest.mark.asyncio
async def test_policies_are_checked_and_scheduled(env):
    from backend.policies import unschedule
    from backend.routes import policies as route
    from backend.routes.views import delete_view
    from backend.scheduler import scheduler
    for bad, code in (({"at": "25:00"}, "policies.invalidTime"), ({"every": "weekly"}, "policies.weekdayRequired"),
                      ({"max_gb": 0}, "policies.invalidBudget"), ({"view_id": 9}, "policies.viewMissing"),
                      ({"name": " "}, "policies.nameRequired")):
        with pytest.raises(ApiError) as exc:
            await route.create(route.PolicyBody(**{"name": "x", "view_id": 1, **bad}))
        assert exc.value.code == code
    policy = await route.create(route.PolicyBody(name="Weekly", view_id=1, every="weekly", weekday=6, at="2:30"))
    try:
        assert policy["at"] == "02:30"
        trigger = scheduler.get_job(f"policy-{policy['id']}").trigger
        fields = {f.name: str(f) for f in trigger.fields}
        assert (fields["hour"], fields["minute"], fields["day_of_week"]) == ("2", "30", "6")
        await route.update(policy["id"], route.PolicyBody(name="Weekly", view_id=1, enabled=False))
        assert scheduler.get_job(f"policy-{policy['id']}") is None  # off: not scheduled
        with pytest.raises(ApiError) as exc:
            await delete_view(1)
        assert exc.value.code == "views.inUseByPolicies"
        await route.delete(policy["id"])
        assert await route.list_all() == []
    finally:
        unschedule(policy["id"])


@pytest.mark.asyncio
async def test_the_planner(env):
    from backend.routes import policies as route
    db_path, started = env
    # At NVENC's default speed (0.08 s per second of video), c saves the most per hour
    # of encoding, then b, then a.
    plan = await route.plan(route.PlanBody(target_gb=3.5))
    assert (plan["files"], plan["savings"], plan["reached"]) == (2, 4 * GB, True)
    assert plan["available"] == 9 * GB and "paths" not in plan
    assert plan["seconds"] == round(1200 * 0.08 + 600 * 0.08)  # c + b, one job at a time
    tight = datetime.now(timezone.utc) + timedelta(seconds=10)
    assert (await route.plan(route.PlanBody(target_gb=3.5, deadline=tight)))["on_time"] is False
    assert (await route.plan(route.PlanBody(target_gb=50)))["reached"] is False
    assert (await route.queue_plan(route.PlanBody(target_gb=3.5)))["queued"] == 2
    assert await _queued(db_path) == [("/m/b.mkv", "convert", 1), ("/m/c.mkv", "convert", 1)]
    assert started == [True]
    assert (await route.plan(route.PlanBody(target_gb=3.5)))["files"] == 1  # queued ones are left out now
