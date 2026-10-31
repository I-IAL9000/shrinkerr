"""Policies and the planner (v0.10.0) — see backend/policies.py."""
import json
import re
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from backend import policies
from backend.api_errors import ApiError
from backend.database import connect_db

router = APIRouter(prefix="/api/policies")


class PolicyBody(BaseModel):
    name: str
    view_id: int
    action: Literal["queue", "health_check"] = "queue"
    every: Literal["daily", "weekly"] = "daily"
    weekday: Optional[int] = None  # 0 = Monday
    at: str = "01:00"
    max_files: Optional[int] = None
    max_gb: Optional[float] = None
    start_queue: bool = True
    enabled: bool = True


async def _check(body: PolicyBody) -> None:
    from backend.routes.views import get_view
    if not body.name.strip():
        raise ApiError(400, "Give the policy a name.", code="policies.nameRequired")
    if await get_view(body.view_id) is None:
        raise ApiError(400, "That saved view doesn't exist.", code="policies.viewMissing")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", body.at.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ApiError(400, "The time must be HH:MM.", code="policies.invalidTime")
    if body.every == "weekly" and (body.weekday is None or not 0 <= body.weekday <= 6):
        raise ApiError(400, "Choose the day of the week.", code="policies.weekdayRequired")
    if (body.max_files is not None and body.max_files < 1) or (body.max_gb is not None and body.max_gb <= 0):
        raise ApiError(400, "A budget must be above zero.", code="policies.invalidBudget")


def _values(body: PolicyBody) -> tuple:
    h, m = body.at.strip().split(":")
    return (body.name.strip(), body.view_id, body.action, body.every,
            body.weekday if body.every == "weekly" else None, f"{int(h):02d}:{m}",
            body.max_files, body.max_gb, int(body.start_queue), int(body.enabled))


def _out(policy: dict) -> dict:
    return {**policy, "start_queue": bool(policy["start_queue"]), "enabled": bool(policy["enabled"]),
            "last_result": json.loads(policy["last_result"]) if policy.get("last_result") else None,
            "next_run_at": policies.next_run(policy["id"])}


@router.get("")
async def list_all():
    return [_out(p) for p in await policies.list_policies()]


@router.post("")
async def create(body: PolicyBody):
    await _check(body)
    db = await connect_db()
    try:
        async with db.execute(
            "INSERT INTO policies (name, view_id, action, every, weekday, at, max_files, max_gb, start_queue, "
            "enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*_values(body), datetime.now(timezone.utc).isoformat())) as cur:
            policy_id = cur.lastrowid
        await db.commit()
    finally:
        await db.close()
    policy = await policies.get_policy(policy_id)
    policies.reschedule(policy)
    return _out(policy)


@router.put("/{policy_id}")
async def update(policy_id: int, body: PolicyBody):
    await _check(body)
    if await policies.get_policy(policy_id) is None:
        raise ApiError(404, "No such policy.", code="policies.notFound")
    db = await connect_db()
    try:
        await db.execute(
            "UPDATE policies SET name = ?, view_id = ?, action = ?, every = ?, weekday = ?, at = ?, max_files = ?, "
            "max_gb = ?, start_queue = ?, enabled = ? WHERE id = ?", (*_values(body), policy_id))
        await db.commit()
    finally:
        await db.close()
    policy = await policies.get_policy(policy_id)
    policies.reschedule(policy)
    return _out(policy)


@router.delete("/{policy_id}")
async def delete(policy_id: int):
    policies.unschedule(policy_id)
    db = await connect_db()
    try:
        await db.execute("DELETE FROM policies WHERE id = ?", (policy_id,))
        await db.commit()
    finally:
        await db.close()
    return {"deleted": policy_id}


@router.post("/{policy_id}/run")
async def run_now(policy_id: int):
    if await policies.get_policy(policy_id) is None:
        raise ApiError(404, "No such policy.", code="policies.notFound")
    return await policies.run_policy(policy_id)


class PlanBody(BaseModel):
    target_gb: float
    deadline: Optional[datetime] = None
    view_id: Optional[int] = None
    # From the pending queue (v0.10.0): its jobs move to the front, nothing's added
    pending: bool = False


async def _plan(body: PlanBody) -> dict:
    if body.target_gb <= 0:
        raise ApiError(400, "Choose how much space to free.", code="policies.invalidTarget")
    deadline = body.deadline
    if deadline is not None and deadline.tzinfo is None:
        deadline = deadline.astimezone()  # the browser's local time, sent without a zone
    return await policies.plan(int(body.target_gb * policies.GIB), deadline, body.view_id, body.pending)


@router.post("/plan")
async def plan(body: PlanBody):
    """The planner: which conversions free `target_gb` fastest, and when
    they'd be done. Nothing is queued."""
    result = await _plan(body)
    return {k: v for k, v in result.items() if k not in ("paths", "job_ids")}


@router.post("/plan/queue")
async def queue_plan(body: PlanBody):
    """Queue the plan's files (ahead of the rest) and start the queue — or,
    from the pending queue, move its jobs to the front in the plan's order,
    the rest after them as they were (priorities stay)."""
    from backend.routes import jobs as jobs_route
    result = await _plan(body)
    if body.pending:
        chosen = result["job_ids"]
        if chosen and jobs_route._queue is not None:
            db = await connect_db()
            try:
                async with db.execute("SELECT id FROM jobs WHERE status = 'pending' "
                                      "ORDER BY priority DESC, queue_order ASC") as cur:
                    rest = [r[0] for r in await cur.fetchall() if r[0] not in set(chosen)]
            finally:
                await db.close()
            await jobs_route._queue.reorder_jobs(chosen + rest)
            if jobs_route._worker is not None:
                jobs_route._worker.start_if_idle()
        return {"moved": len(chosen)}
    paths = result["paths"]
    added = 0
    if paths:
        added = (await jobs_route.add_jobs_from_scan(
            jobs_route.BulkQueueFromScanRequest(file_paths=paths, priority=1, automatic=True)))["added"]
        if added and jobs_route._worker is not None:
            jobs_route._worker.start_if_idle()
    return {"queued": added}
