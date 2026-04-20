"""Webhook endpoints for external tool integration."""

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.database import connect_db

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


class WebhookScanRequest(BaseModel):
    paths: Optional[list[str]] = None


class WebhookQueueRequest(BaseModel):
    paths: list[str]
    priority: int = 0
    insert_next: bool = False
    force_reencode: bool = False


@router.post("/scan")
async def webhook_scan(request: WebhookScanRequest = WebhookScanRequest()):
    """Trigger a library scan. If no paths provided, scans all configured directories."""
    from backend.routes.scan import _scan_task, _run_scan, ScanRequest
    import backend.routes.scan as scan_mod

    if scan_mod._scan_task and not scan_mod._scan_task.done():
        raise HTTPException(status_code=409, detail="Scan already in progress")

    paths = request.paths
    if not paths:
        # Load all configured media directories
        db = await connect_db()
        try:
            async with db.execute("SELECT path FROM media_dirs") as cur:
                rows = await cur.fetchall()
                paths = [r["path"] for r in rows]
        finally:
            await db.close()

    if not paths:
        raise HTTPException(status_code=400, detail="No paths to scan")

    scan_mod._scan_task = asyncio.create_task(_run_scan(paths))
    return {"status": "started", "paths": paths}


@router.post("/queue")
async def webhook_queue(request: WebhookQueueRequest):
    """Add files to the conversion queue by path."""
    from backend.routes.jobs import _queue, _os
    from backend.scanner import probe_file, classify_audio_tracks, classify_subtitle_tracks, detect_native_language, codec_matches_source
    from backend.config import settings

    if _queue is None:
        raise HTTPException(status_code=503, detail="Queue not initialized")

    # Load source codecs from settings
    source_codecs = ["h264"]
    try:
        async with connect_db() as db:
            async with db.execute("SELECT value FROM settings WHERE key = 'source_codecs'") as cur:
                row = await cur.fetchone()
                if row and row[0]:
                    source_codecs = json.loads(row[0])
    except Exception:
        pass

    added = 0
    errors = []

    for fp in request.paths:
        import os
        if not os.path.exists(fp):
            errors.append(f"File not found: {fp}")
            continue

        probe = await probe_file(fp)
        if not probe:
            errors.append(f"Probe failed: {fp}")
            continue

        video_codec = (probe.get("video_codec") or "").lower()
        needs_conversion = codec_matches_source(video_codec, source_codecs)
        if request.force_reencode:
            needs_conversion = True

        native_lang = detect_native_language(probe.get("audio_tracks", []))
        audio_tracks = classify_audio_tracks(probe.get("audio_tracks", []), native_lang, probe.get("duration", 0))
        sub_tracks = classify_subtitle_tracks(probe.get("subtitle_tracks", []), native_lang)

        audio_remove = [t.stream_index for t in audio_tracks if not t.keep and not t.locked]
        sub_remove = [t.stream_index for t in sub_tracks if not t.keep and not t.locked]
        has_audio_work = len(audio_remove) > 0 or len(sub_remove) > 0

        if needs_conversion and has_audio_work:
            job_type = "combined"
        elif needs_conversion:
            job_type = "convert"
        elif has_audio_work:
            job_type = "audio"
        else:
            continue

        await _queue.add_job(
            file_path=fp,
            job_type=job_type,
            encoder="nvenc",
            audio_tracks_to_remove=audio_remove,
            subtitle_tracks_to_remove=sub_remove,
            original_size=probe.get("file_size", 0),
            priority=request.priority,
            insert_next=request.insert_next,
        )
        added += 1

    # Auto-start queue if items were added and worker is idle
    if added > 0:
        from backend.routes.jobs import _worker
        if _worker is not None and (not _worker._running or _worker._paused):
            print(f"[WEBHOOK] Auto-starting queue for {added} new job(s)", flush=True)
            _worker.start()

    return {"added": added, "errors": errors}


@router.post("/pause")
async def webhook_pause():
    """Pause the conversion queue."""
    from backend.routes.jobs import _worker
    if _worker:
        _worker.pause()
    return {"status": "paused"}


@router.post("/resume")
async def webhook_resume():
    """Resume the conversion queue."""
    from backend.routes.jobs import _worker
    if _worker:
        _worker.resume()
    return {"status": "resumed"}


@router.get("/status")
async def webhook_status():
    """Get current Shrinkerr status."""
    db = await connect_db()
    try:
        async with db.execute("SELECT COUNT(*) as c FROM jobs WHERE status = 'running'") as cur:
            running = (await cur.fetchone())["c"]
        async with db.execute("SELECT COUNT(*) as c FROM jobs WHERE status = 'pending'") as cur:
            pending = (await cur.fetchone())["c"]
        async with db.execute(
            "SELECT COUNT(*) as c, COALESCE(SUM(space_saved), 0) as saved FROM jobs WHERE status = 'completed'"
        ) as cur:
            row = await cur.fetchone()
            completed = row["c"]
            total_saved = row["saved"]
        async with db.execute("SELECT AVG(fps) as avg_fps FROM jobs WHERE status = 'running' AND fps > 0") as cur:
            row = await cur.fetchone()
            avg_fps = round(row["avg_fps"], 1) if row and row["avg_fps"] else 0
    finally:
        await db.close()

    from backend.routes.jobs import _worker
    paused = _worker.paused if _worker else False

    return {
        "running": running,
        "pending": pending,
        "completed": completed,
        "total_saved": total_saved,
        "avg_fps": avg_fps,
        "paused": paused,
    }
