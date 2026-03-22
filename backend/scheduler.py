from datetime import datetime
from typing import Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

scheduler = AsyncIOScheduler()

_SCHEDULED_JOB_ID = "queue_start"
_worker_start_callback: Optional[Callable] = None


def init_scheduler(worker_start_callback: Callable) -> None:
    """Save callback and start the scheduler."""
    global _worker_start_callback
    _worker_start_callback = worker_start_callback
    if not scheduler.running:
        scheduler.start()


def schedule_queue_start(start_time: datetime) -> str:
    """Schedule the worker to start at start_time. Replaces any existing schedule."""
    # Remove existing job if any
    cancel_scheduled_start()

    scheduler.add_job(
        _worker_start_callback,
        trigger="date",
        run_date=start_time,
        id=_SCHEDULED_JOB_ID,
        replace_existing=True,
    )
    return _SCHEDULED_JOB_ID


def cancel_scheduled_start() -> None:
    """Cancel any pending scheduled start."""
    try:
        scheduler.remove_job(_SCHEDULED_JOB_ID)
    except Exception:
        pass


def get_scheduled_start() -> Optional[datetime]:
    """Return the scheduled start datetime, or None if none scheduled."""
    try:
        job = scheduler.get_job(_SCHEDULED_JOB_ID)
        if job is None:
            return None
        return job.next_run_time
    except Exception:
        return None
