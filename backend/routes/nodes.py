"""API endpoints for distributed worker nodes.

Remote workers use these endpoints to register, request jobs, report progress,
and report completion. The frontend uses GET /api/nodes for the Nodes page.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.database import connect_db
from backend.websocket import ws_manager


router = APIRouter(prefix="/api/nodes")


def _get_nm(request: Request):
    """Get NodeManager from app state."""
    nm = getattr(request.app.state, "node_manager", None)
    if nm is None:
        raise HTTPException(503, "Node manager not initialized")
    return nm


# ------------------------------------------------------------------
# Models
# ------------------------------------------------------------------

class HeartbeatRequest(BaseModel):
    node_id: str
    name: str
    hostname: str = ""
    capabilities: list[str] = []
    path_mappings: list[dict] = []
    ffmpeg_version: str | None = None
    gpu_name: str | None = None
    os_info: str | None = None
    max_jobs: int = 1
    driver_version: str | None = None
    # Human-readable reason the worker isn't advertising nvenc (e.g. driver
    # too old, no NVIDIA device, ffmpeg build lacks the encoder). Surfaced
    # on the Monitor page so users know what to fix.
    nvenc_unavailable_reason: str | None = None


class RequestJobBody(BaseModel):
    node_id: str


class ProgressReport(BaseModel):
    node_id: str
    job_id: int
    progress: float = 0
    fps: float | None = None
    eta_seconds: int | None = None
    step: str = "converting"


class CompletionReport(BaseModel):
    node_id: str
    job_id: int
    success: bool
    output_path: str | None = None
    space_saved: int = 0
    error: str | None = None
    vmaf_score: float | None = None
    backup_path: str | None = None
    ffmpeg_command: str | None = None
    encoding_stats: dict | None = None


class MetricsReport(BaseModel):
    node_id: str
    # Accept the whole metrics dict straight from get_all_metrics() on the
    # worker. We don't enforce a schema here — if a worker sends extra fields
    # (future GPU features, etc) we just pass them through to the frontend.
    metrics: dict


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@router.post("/heartbeat")
async def heartbeat(req: HeartbeatRequest, request: Request):
    """Worker registration + periodic keepalive."""
    nm = _get_nm(request)
    node = await nm.register_or_update(
        node_id=req.node_id,
        name=req.name,
        hostname=req.hostname,
        capabilities=req.capabilities,
        path_mappings=req.path_mappings,
        ffmpeg_version=req.ffmpeg_version,
        gpu_name=req.gpu_name,
        os_info=req.os_info,
        max_jobs=req.max_jobs,
        driver_version=req.driver_version,
        nvenc_unavailable_reason=req.nvenc_unavailable_reason,
    )
    # Broadcast node update to frontend
    await ws_manager.broadcast({
        "type": "node_update",
        "node_id": req.node_id,
        "name": req.name,
        "status": node.get("status", "online"),
    })
    return {"status": "ok", "heartbeat_interval": 30}


@router.post("/request-job")
async def request_job(req: RequestJobBody, request: Request):
    """Worker polls for the next available job matching its capabilities."""
    nm = _get_nm(request)
    node = await nm.get_node(req.node_id)
    if not node:
        raise HTTPException(404, "Node not registered — send a heartbeat first")

    # Respect queue pause state for the LOCAL worker only.
    # Remote workers should always be able to poll for jobs — the queue
    # pause controls the server's built-in worker, not the whole cluster.
    from backend.routes.jobs import _worker
    queue_paused = _worker is not None and (not _worker._running or _worker._paused)
    if queue_paused and req.node_id == "local":
        return {"job": None, "queue_paused": True}

    # Per-node pause — don't hand out jobs to a paused node
    if node.get("paused"):
        return {"job": None, "node_paused": True}

    # Circuit breaker: refuse jobs if the node has too many consecutive failures
    consecutive_failures = node.get("consecutive_failures", 0)
    if consecutive_failures >= nm.MAX_CONSECUTIVE_FAILURES:
        return {
            "job": None,
            "suspended": True,
            "message": f"Node suspended after {consecutive_failures} consecutive failures. "
                       "Reset from the Nodes page to resume.",
        }

    capabilities = node.get("capabilities", [])

    # Per-node schedule — don't hand out jobs outside configured hours
    if node.get("schedule_enabled"):
        hours = node.get("schedule_hours") or []
        from datetime import datetime
        if isinstance(hours, str):
            try:
                hours = json.loads(hours)
            except Exception:
                hours = []
        if datetime.now().hour not in hours:
            return {"job": None, "out_of_schedule": True}

    # Per-node job affinity — restrict which encoders this node accepts
    affinity = node.get("job_affinity") or "any"
    affinity_filter = ""
    affinity_params: list = []
    if affinity == "cpu_only":
        affinity_filter = "AND (encoder IS NULL OR encoder = '' OR LOWER(encoder) IN ('libx265','x265','cpu'))"
    elif affinity == "nvenc_only":
        affinity_filter = "AND LOWER(encoder) IN ('nvenc','hevc_nvenc')"

    # If encoder translation is disabled, only assign jobs this node can run natively
    # (affinity still applies on top of this). A libx265-only node with translation
    # disabled will never get nvenc-tagged jobs.
    translate = node.get("translate_encoder")
    if translate is None:
        translate = True  # default on
    if not translate:
        native_encoders = []
        if "nvenc" in capabilities:
            native_encoders.extend(["nvenc", "hevc_nvenc"])
        if "libx265" in capabilities:
            native_encoders.extend(["libx265", "x265", "cpu"])
        if native_encoders:
            placeholders = ",".join("?" * len(native_encoders))
            affinity_filter += f" AND (encoder IS NULL OR encoder = '' OR LOWER(encoder) IN ({placeholders}))"
            affinity_params = native_encoders

    # Find next pending job.
    db = await connect_db()
    try:
        async with db.execute(
            f"SELECT * FROM jobs WHERE status = 'pending' {affinity_filter} "
            f"ORDER BY priority DESC, queue_order ASC LIMIT 1",
            affinity_params,
        ) as cur:
            job = await cur.fetchone()
    finally:
        await db.close()

    if not job:
        return {"job": None}

    job = dict(job)

    # Assign this job to the node
    assigned = await nm.assign_job_to_node(req.node_id, job)
    # Include the node's translate_encoder flag so the worker knows whether to
    # fall back to libx265 for nvenc jobs (vs. reject them)
    assigned["translate_encoder"] = bool(translate)

    # Pass VMAF + libx265 default settings through to the worker so remote
    # nodes honour the server's configured policy. Without libx265 defaults,
    # a CPU-only worker handed an NVENC job had to fall back on the preset
    # translation table, which mapped "nvenc p6/CQ20" to "libx265 slower/CRF16"
    # — effectively unusable on CPU (~1-2 fps on M1).
    db = await connect_db()
    try:
        async with db.execute(
            "SELECT key, value FROM settings "
            "WHERE key IN ('vmaf_analysis_enabled', 'vmaf_min_score', "
            "              'libx265_preset', 'libx265_crf', "
            "              'nvenc_preset', 'nvenc_cq')"
        ) as cur:
            srv_settings = {r["key"]: r["value"] for r in await cur.fetchall()}
    finally:
        await db.close()
    assigned["vmaf_analysis_enabled"] = (
        srv_settings.get("vmaf_analysis_enabled", "true").lower() == "true"
    )
    try:
        assigned["vmaf_min_score"] = float(srv_settings.get("vmaf_min_score", "0") or 0)
    except (TypeError, ValueError):
        assigned["vmaf_min_score"] = 0.0
    # Server's configured libx265 defaults — the worker uses these when a job
    # doesn't carry explicit libx265_preset/crf (e.g. an NVENC job dispatched
    # to a CPU-only worker). Optional; absent on older servers.
    assigned["default_libx265_preset"] = srv_settings.get("libx265_preset")
    try:
        _crf = srv_settings.get("libx265_crf")
        assigned["default_libx265_crf"] = int(_crf) if _crf is not None else None
    except (TypeError, ValueError):
        assigned["default_libx265_crf"] = None
    # Server's NVENC defaults so a remote worker translating an NVENC job
    # with NULL per-job settings uses the user's global choice instead of
    # the old hardcoded "p6/CQ20" fallback.
    assigned["default_nvenc_preset"] = srv_settings.get("nvenc_preset")
    try:
        _cq = srv_settings.get("nvenc_cq")
        assigned["default_nvenc_cq"] = int(_cq) if _cq is not None else None
    except (TypeError, ValueError):
        assigned["default_nvenc_cq"] = None
    print(f"[NODES] Assigned job {job['id']} ({job.get('encoder') or 'default'}) to node '{req.node_id}' ({node['name']})", flush=True)

    # Broadcast that this node is now working
    await ws_manager.broadcast({
        "type": "node_update",
        "node_id": req.node_id,
        "name": node["name"],
        "status": "working",
        "current_job_id": job["id"],
    })

    return {"job": assigned}


@router.post("/report-progress")
async def report_progress(req: ProgressReport, request: Request):
    """Worker reports job progress. Returns cancel flag."""
    nm = _get_nm(request)

    # Update job progress in DB
    db = await connect_db()
    try:
        await db.execute(
            "UPDATE jobs SET progress = ?, fps = ?, eta_seconds = ? WHERE id = ?",
            (req.progress, req.fps, req.eta_seconds, req.job_id),
        )
        await db.commit()
    finally:
        await db.close()

    # Get node name for the broadcast
    node = await nm.get_node(req.node_id)
    node_name = node["name"] if node else req.node_id

    # Broadcast to frontend via existing WebSocket pipeline
    # Get stats for the broadcast
    stats_db = await connect_db()
    try:
        async with stats_db.execute(
            "SELECT COUNT(*) as completed, "
            "COALESCE(SUM(CASE WHEN status='completed' AND space_saved > 0 THEN space_saved ELSE 0 END), 0) as saved "
            "FROM jobs"
        ) as cur:
            srow = await cur.fetchone()
        # Get file name
        async with stats_db.execute("SELECT file_path FROM jobs WHERE id = ?", (req.job_id,)) as cur:
            jrow = await cur.fetchone()
    finally:
        await stats_db.close()

    file_name = jrow["file_path"].rsplit("/", 1)[-1] if jrow else ""

    await ws_manager.broadcast({
        "type": "job_progress",
        "job_id": req.job_id,
        "file_name": file_name,
        "progress": req.progress,
        "fps": req.fps,
        "eta": req.eta_seconds,
        "step": req.step,
        "jobs_completed": srow["completed"] if srow else 0,
        "jobs_total": 0,
        "total_saved": srow["saved"] if srow else 0,
        "node_name": node_name,
        "node_id": req.node_id,
    })

    # Check cancel flag
    cancelled = nm.is_cancel_requested(req.job_id)
    if cancelled:
        # Also check DB flag
        pass
    else:
        db2 = await connect_db()
        try:
            async with db2.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?", (req.job_id,)
            ) as cur:
                cr = await cur.fetchone()
                if cr and cr["cancel_requested"]:
                    cancelled = True
        finally:
            await db2.close()

    return {"ok": True, "cancelled": cancelled}


@router.post("/report-complete")
async def report_complete(req: CompletionReport, request: Request):
    """Worker reports job completion or failure."""
    nm = _get_nm(request)
    nm.clear_cancel(req.job_id)

    # Translate output_path back to server paths
    output_path = req.output_path
    if output_path:
        output_path = await nm.translate_path(output_path, req.node_id, "to_server")

    db = await connect_db()
    try:
        if req.success:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                "UPDATE jobs SET status = 'completed', completed_at = ?, "
                "space_saved = ?, error_log = NULL WHERE id = ?",
                (now, req.space_saved, req.job_id),
            )
            # Store encoding stats if provided
            if req.encoding_stats:
                try:
                    stats_json = json.dumps(req.encoding_stats)
                    await db.execute(
                        "UPDATE jobs SET conversion_log_stats = ? WHERE id = ?",
                        (stats_json, req.job_id),
                    )
                except Exception:
                    pass
            # Store VMAF score
            if req.vmaf_score is not None:
                await db.execute(
                    "UPDATE jobs SET vmaf_score = ? WHERE id = ?",
                    (req.vmaf_score, req.job_id),
                )
            # Store backup path
            if req.backup_path:
                bp = await nm.translate_path(req.backup_path, req.node_id, "to_server")
                await db.execute(
                    "UPDATE jobs SET backup_path = ? WHERE id = ?",
                    (bp, req.job_id),
                )
        else:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            # If the job was cancelled via a pause (node pause), return it to
            # pending instead of marking as failed, so it can be picked up again.
            if nm.should_requeue(req.job_id):
                await db.execute(
                    "UPDATE jobs SET status = 'pending', progress = 0, fps = NULL, "
                    "eta_seconds = NULL, started_at = NULL, error_log = NULL, "
                    "assigned_node_id = NULL, assigned_at = NULL, cancel_requested = 0 "
                    "WHERE id = ?",
                    (req.job_id,),
                )
                print(f"[NODES] Job {req.job_id} returned to pending (node paused)", flush=True)
            else:
                await db.execute(
                    "UPDATE jobs SET status = 'failed', completed_at = ?, error_log = ?, "
                    "assigned_node_id = NULL, assigned_at = NULL WHERE id = ?",
                    (now, req.error, req.job_id),
                )
        await db.commit()

        # Get job info for broadcasts
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (req.job_id,)) as cur:
            job = await cur.fetchone()
    finally:
        await db.close()

    # Update node stats (only counts successes, tracks consecutive failures)
    await nm.complete_job_on_node(
        req.node_id, req.job_id,
        success=req.success,
        space_saved=req.space_saved if req.success else 0,
    )

    # Broadcast completion
    file_name = job["file_path"].rsplit("/", 1)[-1] if job else ""
    await ws_manager.broadcast({
        "type": "job_complete",
        "job_id": req.job_id,
        "status": "completed" if req.success else "failed",
        "space_saved": req.space_saved if req.success else 0,
        "error": req.error,
    })

    # Log to file_events
    try:
        from backend.file_events import log_event, EVENT_COMPLETED, EVENT_FAILED
        if req.success and req.space_saved > 0:
            gb = req.space_saved / (1024 ** 3)
            original_size = job["original_size"] if job else 0
            pct = (req.space_saved / original_size * 100) if original_size else 0
            await log_event(
                job["file_path"] if job else "",
                EVENT_COMPLETED,
                f"Converted on {(await nm.get_node(req.node_id) or {}).get('name', req.node_id)}: saved {gb:.2f} GB ({pct:.0f}%)",
                {"job_id": req.job_id, "node_id": req.node_id, "space_saved": req.space_saved},
            )
        elif not req.success:
            await log_event(
                job["file_path"] if job else "",
                EVENT_FAILED,
                f"Failed on {(await nm.get_node(req.node_id) or {}).get('name', req.node_id)}: {(req.error or '')[:120]}",
                {"job_id": req.job_id, "node_id": req.node_id},
            )
    except Exception:
        pass

    # Broadcast node status update (may be "error" if circuit breaker tripped)
    node = await nm.get_node(req.node_id)
    if node:
        await ws_manager.broadcast({
            "type": "node_update",
            "node_id": req.node_id,
            "name": node["name"],
            "status": node["status"],
            "current_job_id": None,
            "consecutive_failures": node.get("consecutive_failures", 0),
        })

    return {"ok": True}


@router.post("/report-metrics")
async def report_metrics(req: MetricsReport, request: Request):
    """Worker pushes CPU/RAM/GPU/disk/net metrics.

    Called every ~5 seconds so the Monitor page can show live per-node
    utilisation. We store these in memory only — persisting every sample
    to disk would be wasteful churn.
    """
    nm = _get_nm(request)
    # Sanity check: the worker must have registered first.
    node = await nm.get_node(req.node_id)
    if not node:
        raise HTTPException(404, "Node not registered — send a heartbeat first")
    nm.update_metrics(req.node_id, req.metrics)
    return {"ok": True}


@router.get("/metrics")
async def get_all_node_metrics(request: Request):
    """Return the latest metrics for every active worker node.

    Used by the Monitor page. Returns a list so the frontend can render
    stable per-node cards; includes the node record (name, hostname,
    gpu_name, status, current_job_id) so the UI doesn't need a separate
    fetch to get node labels.
    """
    nm = _get_nm(request)
    nodes = await nm.get_all_nodes()
    metrics_map = nm.get_all_metrics()
    result = []
    for node in nodes:
        sample = metrics_map.get(node["id"])
        result.append({
            "node_id": node["id"],
            "name": node.get("name") or node.get("hostname") or node["id"],
            "hostname": node.get("hostname", ""),
            "status": node.get("status", "offline"),
            "gpu_name": node.get("gpu_name"),
            "driver_version": node.get("driver_version"),
            "nvenc_unavailable_reason": node.get("nvenc_unavailable_reason"),
            "os_info": node.get("os_info"),
            "current_job_id": node.get("current_job_id"),
            "capabilities": node.get("capabilities", []),
            "metrics": sample["metrics"] if sample else None,
            "age_seconds": sample["age_seconds"] if sample else None,
        })
    return {"nodes": result}


@router.get("")
async def list_nodes(request: Request):
    """List all registered worker nodes."""
    nm = _get_nm(request)
    nodes = await nm.get_all_nodes()
    # Include queue state so frontend can show paused indicator
    from backend.routes.jobs import _worker
    queue_running = _worker._running if _worker else False
    queue_paused = _worker._paused if _worker else False
    return {"nodes": nodes, "queue_running": queue_running, "queue_paused": queue_paused}


@router.delete("/{node_id}")
async def remove_node(node_id: str, request: Request):
    """Remove a remote node."""
    nm = _get_nm(request)
    ok = await nm.remove_node(node_id)
    if not ok:
        raise HTTPException(400, "Cannot remove node (local node cannot be removed)")
    return {"status": "removed"}


@router.post("/{node_id}/cancel")
async def cancel_node_job(node_id: str, request: Request):
    """Cancel the job currently running on a node."""
    nm = _get_nm(request)
    node = await nm.get_node(node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    job_id = node.get("current_job_id")
    if not job_id:
        raise HTTPException(400, "No job running on this node")

    # Set cancel flag (checked by report-progress)
    nm.request_cancel(job_id)

    # Also set DB flag for persistence
    db = await connect_db()
    try:
        await db.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
        await db.commit()
    finally:
        await db.close()

    return {"status": "cancel_requested", "job_id": job_id}


class NodeSettingsBody(BaseModel):
    paused: bool | None = None
    max_jobs: int | None = None
    job_affinity: str | None = None      # 'any' | 'cpu_only' | 'nvenc_only'
    translate_encoder: bool | None = None
    schedule_enabled: bool | None = None
    schedule_hours: list[int] | None = None


@router.patch("/{node_id}/settings")
async def update_node_settings(node_id: str, body: NodeSettingsBody, request: Request):
    """Update per-node settings (pause, affinity, translation, schedule, parallel jobs)."""
    nm = _get_nm(request)
    node = await nm.get_node(node_id)
    if not node:
        raise HTTPException(404, "Node not found")

    updates = []
    params = []
    if body.paused is not None:
        updates.append("paused = ?")
        params.append(1 if body.paused else 0)
    if body.max_jobs is not None:
        updates.append("max_jobs = ?")
        params.append(max(1, min(32, body.max_jobs)))
    if body.job_affinity is not None:
        if body.job_affinity not in ("any", "cpu_only", "nvenc_only"):
            raise HTTPException(400, "job_affinity must be 'any', 'cpu_only', or 'nvenc_only'")
        updates.append("job_affinity = ?")
        params.append(body.job_affinity)
    if body.translate_encoder is not None:
        updates.append("translate_encoder = ?")
        params.append(1 if body.translate_encoder else 0)
    if body.schedule_enabled is not None:
        updates.append("schedule_enabled = ?")
        params.append(1 if body.schedule_enabled else 0)
    if body.schedule_hours is not None:
        hours = [h for h in body.schedule_hours if isinstance(h, int) and 0 <= h <= 23]
        updates.append("schedule_hours = ?")
        params.append(json.dumps(sorted(set(hours))))

    if not updates:
        return {"status": "noop"}

    params.append(node_id)
    db = await connect_db()
    try:
        await db.execute(f"UPDATE worker_nodes SET {', '.join(updates)} WHERE id = ?", params)
        await db.commit()
    finally:
        await db.close()

    # If we just paused a node that's currently running a job, cancel that job
    # and return it to pending (not failed) so another node can pick it up.
    if body.paused is True:
        current_job_id = node.get("current_job_id")
        if current_job_id:
            nm.request_cancel(current_job_id, requeue=True)
            # Also mark the DB flag so the local worker picks it up
            db = await connect_db()
            try:
                await db.execute(
                    "UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (current_job_id,),
                )
                await db.commit()
            finally:
                await db.close()
            # For the local node, trigger in-process cancellation of the running task
            if node_id == "local":
                try:
                    from backend.routes.jobs import _worker
                    if _worker is not None:
                        await _worker.cancel_current(current_job_id)
                except Exception as exc:
                    print(f"[NODES] Could not cancel local job: {exc}", flush=True)
            print(f"[NODES] Pausing node '{node_id}' — cancelling + requeuing job {current_job_id}", flush=True)

    # Broadcast updated node
    node = await nm.get_node(node_id)
    if node:
        await ws_manager.broadcast({
            "type": "node_update",
            "node_id": node_id,
            "name": node["name"],
            "status": node["status"],
        })
    return {"status": "updated"}


@router.post("/{node_id}/reset")
async def reset_node(node_id: str, request: Request):
    """Reset a node's error state after consecutive failures."""
    nm = _get_nm(request)
    ok = await nm.reset_node(node_id)
    if not ok:
        raise HTTPException(400, "Node is not in error state")

    node = await nm.get_node(node_id)
    if node:
        await ws_manager.broadcast({
            "type": "node_update",
            "node_id": node_id,
            "name": node["name"],
            "status": node["status"],
            "consecutive_failures": 0,
        })
    return {"status": "reset"}
