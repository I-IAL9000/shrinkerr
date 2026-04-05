import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import scheduler as sched_module
from backend.database import get_db

router = APIRouter(prefix="/api/schedule")


class ScheduleSetRequest(BaseModel):
    start_time: str  # ISO 8601 datetime string


class RunHoursRequest(BaseModel):
    enabled: bool = False
    hours: Optional[list[int]] = None  # list of active hours (0-23)
    start: Optional[int] = None  # legacy: start hour
    end: Optional[int] = None    # legacy: end hour


@router.post("/set")
async def set_schedule(request: ScheduleSetRequest):
    try:
        start_time = datetime.fromisoformat(request.start_time)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid datetime: {exc}")
    job_id = sched_module.schedule_queue_start(start_time)
    return {"status": "scheduled", "job_id": job_id, "start_time": start_time.isoformat()}


@router.delete("/cancel")
async def cancel_schedule():
    sched_module.cancel_scheduled_start()
    return {"status": "cancelled"}


@router.get("/")
async def get_schedule():
    dt = sched_module.get_scheduled_start()
    run_hours = await _get_run_hours()
    return {
        "scheduled_start": dt.isoformat() if dt else None,
        "run_hours": run_hours,
    }


@router.post("/run-hours")
async def set_run_hours(request: RunHoursRequest):
    if request.hours is not None:
        data = {"enabled": request.enabled, "hours": sorted(set(h for h in request.hours if 0 <= h < 24))}
    else:
        # Legacy start/end format — convert to hours array
        s = request.start or 22
        e = request.end or 8
        if s > e:
            hours = [h for h in range(24) if h >= s or h < e]
        else:
            hours = list(range(s, e))
        data = {"enabled": request.enabled, "hours": hours}
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            ("run_hours", json.dumps(data)),
        )
        await db.commit()
    finally:
        await db.close()
    return {"status": "saved", "run_hours": data}


async def _get_run_hours() -> dict:
    db = await get_db()
    try:
        async with db.execute("SELECT value FROM settings WHERE key = ?", ("run_hours",)) as cur:
            row = await cur.fetchone()
            if row is None:
                return {"enabled": False, "start": 22, "end": 8}
            return json.loads(row[0])
    finally:
        await db.close()


async def is_within_run_hours() -> bool:
    """Check if the current time is within the configured run hours. Returns True if no restriction or within window."""
    run_hours = await _get_run_hours()
    if not run_hours.get("enabled", False):
        return True

    from datetime import datetime
    now = datetime.now().hour

    if "hours" in run_hours:
        active_hours = run_hours["hours"]
        if not active_hours:
            return False  # No hours selected
        return now in active_hours

    # Legacy start/end format
    start = run_hours.get("start", 22)
    end = run_hours.get("end", 8)
    if start > end:
        return now >= start or now < end
    elif start < end:
        return start <= now < end
    else:
        return True
