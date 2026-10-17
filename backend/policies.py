"""Policies (v0.10.0): a saved view + an action + a schedule + a budget —
"every night at 01:00, queue up to 200 GB of the view 'Big x264 films',
best savings first" — automation without building flows. And the planner:
which files to convert to free N GB, and when that would be done."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from backend.database import connect_db

ACTIONS = ("queue", "health_check")
GIB = 1024 ** 3  # "GB" as the UI shows sizes
_CHUNK = 500


async def get_policy(policy_id: int) -> dict | None:
    db = await connect_db()
    try:
        async with db.execute("SELECT * FROM policies WHERE id = ?", (policy_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def list_policies() -> list[dict]:
    db = await connect_db()
    try:
        async with db.execute("SELECT * FROM policies ORDER BY name COLLATE NOCASE") as cur:
            return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _candidates(paths: list[str]) -> list[dict]:
    """The Scanner rows of `paths` that have no job waiting or running."""
    rows: list[dict] = []
    db = await connect_db()
    try:
        for i in range(0, len(paths), _CHUNK):
            chunk = paths[i:i + _CHUNK]
            async with db.execute(
                "SELECT file_path, file_size, duration, video_conv_savings_bytes, health_checked_at "
                f"FROM scan_results WHERE file_path IN ({','.join('?' * len(chunk))})", chunk) as cur:
                rows += [dict(r) for r in await cur.fetchall()]
        async with db.execute("SELECT DISTINCT file_path FROM jobs WHERE status IN ('pending', 'running', 'review')") as cur:
            busy = {r[0] for r in await cur.fetchall()}
    finally:
        await db.close()
    return [r for r in rows if r["file_path"] not in busy]


def within_budget(rows: list[dict], max_files: int | None = None, max_bytes: float | None = None) -> list[dict]:
    """`rows`, in order, as far as the budget goes: at most `max_files`
    files and `max_bytes` of source (a file too big for what's left is
    passed over for smaller ones). No budget: all of them."""
    out, total = [], 0
    for row in rows:
        if max_files is not None and len(out) >= max_files:
            break
        size = row.get("file_size") or 0
        if max_bytes is not None and total + size > max_bytes:
            continue
        out.append(row)
        total += size
    return out


async def run_policy(policy_id: int) -> dict:
    """Run a policy now: its view's files without a waiting job, best first
    (queue: highest estimated savings; health check: never or longest ago
    checked), as far as its budget goes. Records the result on the policy."""
    from backend.routes import jobs as jobs_route
    from backend.routes.scan import _paths_matching
    from backend.routes.views import get_view
    policy = await get_policy(policy_id)
    if policy is None:
        return {"error": "notFound"}
    view = await get_view(policy["view_id"])
    if view is None:
        result: dict = {"error": "viewMissing"}
    else:
        rows = await _candidates(await _paths_matching(view["filter"]))
        if policy["action"] == "health_check":
            rows.sort(key=lambda r: r["health_checked_at"] or "")
        else:
            rows.sort(key=lambda r: -(r["video_conv_savings_bytes"] or 0))
        chosen = within_budget(rows, policy["max_files"],
                               policy["max_gb"] * GIB if policy["max_gb"] else None)
        paths = [r["file_path"] for r in chosen]
        added = 0
        if paths and policy["action"] == "health_check":
            added = (await jobs_route.queue_health_checks(jobs_route.HealthCheckRequest(file_paths=paths)))["added"]
        elif paths:
            added = (await jobs_route.add_jobs_from_scan(jobs_route.BulkQueueFromScanRequest(file_paths=paths)))["added"]
        if added and policy["start_queue"] and jobs_route._worker is not None:
            jobs_route._worker.start_if_idle()
        result = {"matched": len(rows), "chosen": len(paths), "queued": added,
                  "bytes": sum(r["file_size"] or 0 for r in chosen)}
    db = await connect_db()
    try:
        await db.execute("UPDATE policies SET last_run_at = ?, last_result = ? WHERE id = ?",
                         (datetime.now(timezone.utc).isoformat(), json.dumps(result), policy_id))
        await db.commit()
    finally:
        await db.close()
    print(f"[POLICY] {policy['name']}: {result}", flush=True)
    return result


# ── Schedule ─────────────────────────────────────────────────────────────

def _job_id(policy_id: int) -> str:
    return f"policy-{policy_id}"


def reschedule(policy: dict) -> None:
    """(Re)register a policy with the scheduler: daily or weekly at its
    local time; nothing when it's off."""
    from apscheduler.triggers.cron import CronTrigger
    from backend.scheduler import scheduler
    unschedule(policy["id"])
    if not policy["enabled"]:
        return
    hour, minute = (int(x) for x in policy["at"].split(":"))
    trigger = CronTrigger(hour=hour, minute=minute,
                          day_of_week=policy["weekday"] if policy["every"] == "weekly" else None)
    scheduler.add_job(run_policy, trigger, args=[policy["id"]], id=_job_id(policy["id"]),
                      replace_existing=True, coalesce=True, misfire_grace_time=3600)


def unschedule(policy_id: int) -> None:
    from backend.scheduler import scheduler
    try:
        scheduler.remove_job(_job_id(policy_id))
    except Exception:
        pass


def next_run(policy_id: int) -> str | None:
    from backend.scheduler import scheduler
    job = scheduler.get_job(_job_id(policy_id))
    when = getattr(job, "next_run_time", None) if job else None
    return when.isoformat() if when else None


async def schedule_policies() -> None:
    """At startup: every policy that's on."""
    for policy in await list_policies():
        try:
            reschedule(policy)
        except Exception as exc:
            print(f"[POLICY] Couldn't schedule {policy['name']}: {exc}", flush=True)


# ── Planner ──────────────────────────────────────────────────────────────

async def _pending_rows(speeds: dict) -> list[dict]:
    """The pending jobs that free space (a health check changes nothing),
    each with what it saves — as the queue's "What the queue will do" has
    it — and its seconds of encoding; a cleanup is a short remux."""
    from backend.encoding_estimates import load_quality_settings
    from backend.routes.jobs import _JOB_PLAN_COLS, _job_savings, _scan_rows_for
    db = await connect_db()
    try:
        async with db.execute(f"SELECT {_JOB_PLAN_COLS} FROM jobs WHERE status = 'pending' "
                              "AND job_type != 'health_check' ORDER BY priority DESC, queue_order ASC") as cur:
            jobs = [dict(r) for r in await cur.fetchall()]
        rows = await _scan_rows_for(db, sorted({j["file_path"] for j in jobs}))
        quality = await load_quality_settings(db)
    finally:
        await db.close()
    out = []
    for job in jobs:
        row = rows.get(job["file_path"], {})
        try:
            audio = json.loads(row.get("audio_tracks_json") or "[]")
        except (ValueError, TypeError):
            audio = []
        speed = speeds["libx265" if job.get("encoder") == "libx265" else "nvenc"]
        out.append({"job_id": job["id"], "file_path": job["file_path"], "savings": _job_savings(job, row, audio, quality),
                    "seconds": 30.0 if job["job_type"] == "audio" else max(30.0, (row.get("duration") or 0) * speed)})
    return out


async def plan(target_bytes: int, deadline: datetime | None = None, view_id: int | None = None,
               pending: bool = False) -> dict:
    """The conversions that free `target_bytes` fastest: files that need
    converting (not ignored, queued, or skipped as hardlinked / leaving
    soon), by estimated savings per hour of encoding, until the target is
    reached — and when they'd be done at this server's parallel jobs.
    `view_id` limits it to a saved view's files; `pending` (v0.10.0) plans
    from the jobs already waiting instead (their `job_ids`, to move them to
    the front)."""
    from backend.routes.jobs import encode_speed_factors
    from backend.routes.scan import _paths_matching
    from backend.routes.views import get_view
    db = await connect_db()
    try:
        async with db.execute("SELECT key, value FROM settings WHERE key IN "
                              "('skip_hardlinked', 'maintainerr_skip', 'parallel_jobs', 'default_encoder')") as cur:
            settings = {k: v for k, v in await cur.fetchall()}
        speeds = await encode_speed_factors(db)
    finally:
        await db.close()
    rows = []
    if pending:
        rows = [r for r in await _pending_rows(speeds) if r["savings"] > 0]
    else:
        pills = ["needs_conversion", "!ignored", "!queued"]
        if (settings.get("skip_hardlinked") or "true").lower() == "true":
            pills.append("!hardlinked")
        if (settings.get("maintainerr_skip") or "true").lower() == "true":
            pills.append("!leaving_soon")
        paths = await _paths_matching(",".join(pills))
        if view_id is not None:
            view = await get_view(view_id)
            paths = await _paths_matching(view["filter"], files=paths) if view else []
        speed = speeds["libx265" if (settings.get("default_encoder") or "nvenc") == "libx265" else "nvenc"]
        for row in await _candidates(paths):
            savings = row["video_conv_savings_bytes"] or 0
            if savings > 0:
                rows.append({**row, "savings": savings, "seconds": max(30.0, (row["duration"] or 0) * speed)})
    rows.sort(key=lambda r: r["savings"] / r["seconds"], reverse=True)
    chosen, saved, seconds = [], 0, 0.0
    for row in rows:
        if saved >= target_bytes:
            break
        chosen.append(row)
        saved += row["savings"]
        seconds += row["seconds"]
    try:
        parallel = max(1, int(settings.get("parallel_jobs") or 1))
    except ValueError:
        parallel = 1
    finish = datetime.now(timezone.utc) + timedelta(seconds=seconds / parallel)
    reached = saved >= target_bytes
    return {
        "files": len(chosen), "savings": saved, "seconds": round(seconds / parallel),
        "finish_at": finish.isoformat(), "reached": reached,
        "available": sum(r["savings"] for r in rows),
        "on_time": deadline is None or finish <= deadline,
        "paths": [r["file_path"] for r in chosen],
        "job_ids": [r["job_id"] for r in chosen] if pending else [],
    }
