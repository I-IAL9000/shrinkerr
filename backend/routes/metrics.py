"""Prometheus metrics and a dashboard-widget summary (v0.10.0).

/api/metrics: the Prometheus text format — the queue, lifetime savings, the
library, remote workers. Prometheus sends the API key as `Authorization:
Bearer <key>` (its `authorization:` scrape setting); main.py accepts that
here. /api/stats/widget: the same numbers as one small JSON object, for
Homepage / Homarr "custom API" widgets (they send `X-Api-Key`)."""
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from backend.database import connect_db

router = APIRouter()


async def collect() -> dict:
    """Everything both endpoints report. Lifetime totals include jobs
    cleared from the Queue."""
    db = await connect_db()
    try:
        async with db.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status") as cur:
            jobs = {status: n for status, n in await cur.fetchall()}
        async with db.execute(
            "SELECT COALESCE(SUM(CASE WHEN space_saved > 0 THEN space_saved ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN original_size > 0 THEN original_size ELSE 0 END), 0), "
            "AVG(CASE WHEN vmaf_score > 0 THEN vmaf_score END) FROM jobs WHERE status = 'completed'") as cur:
            saved, original, vmaf = await cur.fetchone()
        async with db.execute("SELECT AVG(fps) FROM jobs WHERE status = 'running' AND fps > 0") as cur:
            fps = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(file_size), 0), "
            "COALESCE(SUM(CASE WHEN needs_conversion != 0 THEN 1 ELSE 0 END), 0), "
            "COALESCE(SUM(CASE WHEN needs_conversion != 0 THEN COALESCE(video_conv_savings_bytes, 0) ELSE 0 END), 0) "
            "FROM scan_results WHERE removed_from_list = 0") as cur:
            files, library, to_convert, estimated = await cur.fetchone()
        async with db.execute("SELECT name, status FROM worker_nodes WHERE id != 'local' ORDER BY name") as cur:
            nodes = [(name, status) for name, status in await cur.fetchall()]
    finally:
        await db.close()
    from backend.routes import jobs as jobs_route
    from backend.routes.stats import _get_current_version
    worker = jobs_route._worker
    return {
        "version": _get_current_version(),
        "jobs": {s: jobs.get(s, 0) for s in ("pending", "running", "completed", "failed", "cancelled")},
        "paused": bool(worker and worker._paused),
        "saved_bytes": saved, "original_bytes": original,
        "vmaf_average": round(vmaf, 2) if vmaf else None,
        "fps": round(fps, 1) if fps else None,
        "library_files": files, "library_bytes": library,
        "to_convert": to_convert, "estimated_savings_bytes": estimated,
        "nodes": nodes,
    }


def _label(value: str) -> str:
    """A label value: backslash, quote and newline escaped."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render(s: dict) -> str:
    """The Prometheus text exposition format (0.0.4)."""
    out: list[str] = []

    def metric(name: str, kind: str, help_text: str, samples: list[tuple[str, float]]):
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {kind}")
        out.extend(f"{name}{labels} {value}" for labels, value in samples)

    metric("shrinkerr_info", "gauge", "Shrinkerr version.", [(f'{{version="{_label(s["version"])}"}}', 1)])
    metric("shrinkerr_jobs", "gauge", "Jobs by status (completed: lifetime).",
           [(f'{{status="{k}"}}', v) for k, v in s["jobs"].items()])
    metric("shrinkerr_queue_paused", "gauge", "1 while the queue is paused.", [("", int(s["paused"]))])
    metric("shrinkerr_saved_bytes_total", "counter", "Space saved by completed jobs.", [("", s["saved_bytes"])])
    metric("shrinkerr_original_bytes_total", "counter", "Original size of the files completed jobs converted.",
           [("", s["original_bytes"])])
    if s["vmaf_average"] is not None:
        metric("shrinkerr_vmaf_average", "gauge", "Average VMAF score of completed conversions.", [("", s["vmaf_average"])])
    metric("shrinkerr_encode_fps", "gauge", "Average speed of the running jobs (0 when idle).", [("", s["fps"] or 0)])
    metric("shrinkerr_library_files", "gauge", "Files in the Scanner.", [("", s["library_files"])])
    metric("shrinkerr_library_bytes", "gauge", "Size of the files in the Scanner.", [("", s["library_bytes"])])
    metric("shrinkerr_files_to_convert", "gauge", "Files that need converting.", [("", s["to_convert"])])
    metric("shrinkerr_estimated_savings_bytes", "gauge", "What converting them would save, estimated.",
           [("", s["estimated_savings_bytes"])])
    metric("shrinkerr_node_up", "gauge", "Remote workers: 1 online or working, 0 offline.",
           [(f'{{node="{_label(name)}"}}', int(status in ("online", "working"))) for name, status in s["nodes"]])
    return "\n".join(out) + "\n"


@router.get("/api/metrics", response_class=PlainTextResponse)
async def metrics():
    return PlainTextResponse(render(await collect()), media_type="text/plain; version=0.0.4")


@router.get("/api/stats/widget")
async def widget():
    """One flat object for dashboard widgets (documented in docs/monitoring.md)."""
    s = await collect()
    saved, original = s["saved_bytes"], s["original_bytes"]
    return {
        "version": s["version"],
        "pending": s["jobs"]["pending"], "running": s["jobs"]["running"],
        "completed": s["jobs"]["completed"], "failed": s["jobs"]["failed"], "paused": s["paused"],
        "saved_bytes": saved, "saved_percent": round(saved / original * 100, 1) if original else 0,
        "library_files": s["library_files"], "to_convert": s["to_convert"],
        "estimated_savings_bytes": s["estimated_savings_bytes"],
        "fps": s["fps"], "vmaf_average": s["vmaf_average"],
        "nodes_online": sum(1 for _, st in s["nodes"] if st in ("online", "working")), "nodes": len(s["nodes"]),
    }
