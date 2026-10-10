"""Webhook endpoints for external tool integration."""

import asyncio
from typing import Optional

from fastapi import APIRouter
from backend.api_errors import ApiError
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
        raise ApiError(status_code=409, detail="Scan already in progress", code="scan.alreadyRunning")

    paths = request.paths
    # Load configured media directories — used either as the full scan set
    # (when caller passed nothing) or as the allowlist (when they did).
    db = await connect_db()
    try:
        async with db.execute("SELECT path FROM media_dirs") as cur:
            rows = await cur.fetchall()
            configured = [r["path"] for r in rows]
    finally:
        await db.close()

    if not paths:
        paths = configured
    else:
        # Reject caller-supplied paths that aren't under a configured media
        # dir. Stops the webhook from being used to scan `/etc` or bind-
        # mounted secrets.
        from backend.media_paths import is_in_any, _resolve
        if not configured:
            raise ApiError(
                status_code=400,
                detail="No media directories configured",
                code="media.noMediaDirs",
            )
        resolved = [_resolve(p) for p in paths]
        bad = [p for p, r in zip(paths, resolved) if not is_in_any(r, configured)]
        if bad:
            raise ApiError(
                status_code=403,
                detail=f"Paths outside configured media directories: {bad}",
                code="media.pathsOutsideMediaDirs",
                params={"paths": ", ".join(bad)},
            )
        paths = resolved

    if not paths:
        raise ApiError(status_code=400, detail="No paths to scan", code="scan.noPaths")

    scan_mod._scan_task = asyncio.create_task(_run_scan(paths))
    return {"status": "started", "paths": paths}


@router.post("/queue")
async def webhook_queue(request: WebhookQueueRequest):
    """Add files to the conversion queue by path: stored in the Scanner, then
    queued as Add to Queue would queue them — rules included (v0.10.0)."""
    from backend.routes.jobs import BulkQueueFromScanRequest, _queue, queue_files_by_path

    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")
    added, errors = await queue_files_by_path(
        request.paths,
        BulkQueueFromScanRequest(priority=request.priority, force_reencode=request.force_reencode),
        insert_next=request.insert_next, source="WEBHOOK",
    )
    return {"added": added, "errors": errors}


def arr_import_paths(payload: dict) -> tuple[Optional[str], list[str]]:
    """The service and the files a Sonarr / Radarr "Download" (import or
    upgrade) event carries — as Sonarr / Radarr see them."""
    import posixpath
    if isinstance(payload.get("series"), dict):
        service, root = "sonarr", payload["series"].get("path") or ""
        files = [payload.get("episodeFile"), *(payload.get("episodeFiles") or [])]
    elif isinstance(payload.get("movie"), dict):
        service, root = "radarr", payload["movie"].get("folderPath") or ""
        files = [payload.get("movieFile")]
    else:
        return None, []
    paths: list[str] = []
    for f in files:
        if not isinstance(f, dict):
            continue
        path = f.get("path") or (posixpath.join(root, f["relativePath"]) if root and f.get("relativePath") else None)
        if path and path not in paths:
            paths.append(path)
    return service, paths


@router.post("/arr")
async def webhook_arr(payload: dict):
    """Sonarr / Radarr Connect → Webhook (v0.10.0), for setups without the
    NZBGet / SABnzbd scripts — torrent users: an imported file is stored in
    the Scanner and queued as Add to Queue would queue it (rules, hardlink
    skipping and the media-folder check included). Other events are
    acknowledged and ignored."""
    from backend.arr import _from_arr_path
    from backend.routes.jobs import BulkQueueFromScanRequest, _queue, queue_files_by_path

    event = str(payload.get("eventType") or "")
    if event == "Test":
        return {"status": "ok"}
    service, arr_paths = arr_import_paths(payload) if event == "Download" else (None, [])
    if not service or not arr_paths:
        return {"status": "ignored", "event": event}
    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")
    db = await connect_db()
    try:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (f"{service}_path_mapping",)) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()
    paths = [_from_arr_path(p, row[0] if row else "") for p in arr_paths]
    added, errors = await queue_files_by_path(paths, BulkQueueFromScanRequest(), source=service.upper())
    return {"status": "queued", "service": service, "added": added, "errors": errors}


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
