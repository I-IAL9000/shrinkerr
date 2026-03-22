from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend import scheduler as sched_module

router = APIRouter(prefix="/api/schedule")


class ScheduleSetRequest(BaseModel):
    start_time: str  # ISO 8601 datetime string


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
    if dt is None:
        return {"scheduled_start": None}
    return {"scheduled_start": dt.isoformat()}
