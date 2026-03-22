from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.queue import JobQueue, QueueWorker

router = APIRouter(prefix="/api/jobs")

_worker: Optional[QueueWorker] = None
_queue: Optional[JobQueue] = None


def init_job_routes(worker: QueueWorker, queue: JobQueue) -> None:
    global _worker, _queue
    _worker = worker
    _queue = queue


class BulkJobCreate(BaseModel):
    jobs: list[dict]


@router.post("/add")
async def add_job(
    file_path: str,
    job_type: str,
    encoder: Optional[str] = None,
    audio_tracks_to_remove: Optional[list[int]] = None,
):
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    job_id = await _queue.add_job(
        file_path=file_path,
        job_type=job_type,
        encoder=encoder,
        audio_tracks_to_remove=audio_tracks_to_remove or [],
    )
    return {"job_id": job_id}


@router.post("/add-bulk")
async def add_bulk_jobs(payload: BulkJobCreate):
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    job_ids = []
    for job_data in payload.jobs:
        job_id = await _queue.add_job(
            file_path=job_data["file_path"],
            job_type=job_data["job_type"],
            encoder=job_data.get("encoder"),
            audio_tracks_to_remove=job_data.get("audio_tracks_to_remove", []),
        )
        job_ids.append(job_id)
    return {"job_ids": job_ids}


@router.get("/")
async def list_jobs():
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    return await _queue.get_all_jobs()


@router.get("/stats")
async def get_stats():
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    return await _queue.get_stats()


@router.post("/start")
async def start_worker():
    if _worker is None:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    _worker.start()
    return {"status": "started"}


@router.post("/pause")
async def pause_worker():
    if _worker is None:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    _worker.pause()
    return {"status": "paused"}


@router.post("/resume")
async def resume_worker():
    if _worker is None:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    _worker.resume()
    return {"status": "resumed"}


@router.delete("/{job_id}")
async def remove_job(job_id: int):
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    await _queue.remove_job(job_id)
    return {"status": "removed", "job_id": job_id}


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: int):
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    await _queue.update_status(job_id, "cancelled")
    return {"status": "cancelled", "job_id": job_id}


@router.post("/{job_id}/retry")
async def retry_job(job_id: int):
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    await _queue.update_status(job_id, "pending")
    return {"status": "pending", "job_id": job_id}


@router.post("/clear-completed")
async def clear_completed():
    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")
    await _queue.clear_completed()
    return {"status": "cleared"}
