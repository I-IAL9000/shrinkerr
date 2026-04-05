import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

# Configure logging so squeezarr.* loggers output to console
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
from fastapi.staticfiles import StaticFiles

from backend.database import init_db, DB_PATH
from backend.queue import JobQueue, QueueWorker
from backend.websocket import ws_manager
from backend.scheduler import init_scheduler
from backend.watcher import FileWatcher
from backend.routes.jobs import router as jobs_router, init_job_routes
from backend.routes.scan import router as scan_router
from backend.routes.schedule import router as schedule_router
from backend.routes.settings import router as settings_router
from backend.routes.stats import router as stats_router
from backend.routes.rules import router as rules_router


async def cleanup_temp_files(queue: JobQueue) -> None:
    """Delete leftover .converting.mkv temp files from interrupted conversions.

    Only checks directories of jobs that were recently running, not all media dirs.
    """
    import aiosqlite
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        # Only look at directories of pending jobs (just reset from running)
        async with db.execute(
            "SELECT DISTINCT file_path FROM jobs WHERE status = 'pending'"
        ) as cur:
            rows = await cur.fetchall()
            job_dirs = {str(Path(row["file_path"]).parent) for row in rows}
    finally:
        await db.close()

    if not job_dirs:
        return

    removed = 0
    for dir_path in job_dirs:
        try:
            p = Path(dir_path)
            if not p.is_dir():
                continue
            # Only check this one directory, no recursive walk needed
            for f in p.iterdir():
                if f.is_file() and f.name.endswith(".converting.mkv"):
                    try:
                        f.unlink()
                        removed += 1
                        print(f"[CLEANUP] Deleted temp file: {f}", flush=True)
                    except OSError as exc:
                        print(f"[CLEANUP] Failed to delete {f}: {exc}", flush=True)
        except OSError:
            pass
    if removed:
        print(f"[CLEANUP] Removed {removed} leftover temp file(s)", flush=True)


async def backfill_ignored_files() -> None:
    """Populate ignored_files from completed jobs with space_saved <= 0 (one-time migration)."""
    import aiosqlite
    db = await aiosqlite.connect(DB_PATH)
    try:
        async with db.execute(
            "SELECT file_path FROM jobs WHERE status = 'completed' AND space_saved <= 0"
        ) as cur:
            rows = await cur.fetchall()
        count = 0
        for row in rows:
            try:
                await db.execute(
                    "INSERT OR IGNORE INTO ignored_files (file_path, reason, ignored_at) "
                    "VALUES (?, 'no_savings', datetime('now'))",
                    (row[0],),
                )
                count += 1
            except Exception:
                pass
        if count:
            await db.commit()
            print(f"[STARTUP] Backfilled {count} ignored file(s) from completed jobs", flush=True)
    finally:
        await db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Init log capture BEFORE anything else prints
    from backend.logstream import init_logstream
    init_logstream()

    await init_db()
    from backend.database import backfill_daily_stats
    await backfill_daily_stats()
    # Initialize VMAF check and clean test encode temp files
    from backend.test_encode import check_vmaf_available, cleanup_temp_dir
    await check_vmaf_available()
    cleanup_temp_dir()
    # Load IMDb ratings dataset
    from backend.imdb_ratings import ensure_ratings
    await ensure_ratings()
    queue = JobQueue(DB_PATH)
    # Reset any jobs stuck in "running" from a previous session back to pending
    await queue.reset_stale_running()
    # Clean up leftover .converting.mkv temp files from interrupted conversions
    await cleanup_temp_files(queue)
    # Backfill ignored_files from existing completed jobs with no savings
    await backfill_ignored_files()
    worker = QueueWorker(DB_PATH)
    watcher = FileWatcher(DB_PATH, interval_minutes=5)
    app.state.watcher = watcher  # Expose for the API endpoint
    init_job_routes(worker, queue)
    init_scheduler(worker.start)
    watcher.start()
    yield
    worker.stop()
    watcher.stop()


app = FastAPI(title="Squeezarr", lifespan=lifespan)

# API key authentication middleware
_api_key_cache: dict = {"key": None, "checked_at": 0}

@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    path = request.url.path

    # Always allow: health, static assets, poster images, SPA routes, websocket
    if path in ("/api/health",) or path == "/api/posters/image" or path.startswith("/assets/") or not path.startswith("/api/"):
        return await call_next(request)

    # Also allow the login/auth check endpoint
    if path == "/api/auth/check":
        return await call_next(request)

    # Check if API key is configured (cached for 60s)
    import time
    now = time.monotonic()
    if now - _api_key_cache.get("checked_at", 0) > 60:
        try:
            import aiosqlite
            db = await aiosqlite.connect(DB_PATH)
            try:
                async with db.execute("SELECT value FROM settings WHERE key = 'api_key'") as cur:
                    row = await cur.fetchone()
                    _api_key_cache["key"] = row[0] if row and row[0] else None
            finally:
                await db.close()
        except Exception:
            _api_key_cache["key"] = None
        _api_key_cache["checked_at"] = now

    configured_key = _api_key_cache.get("key")
    if not configured_key:
        # No API key configured — allow all requests
        return await call_next(request)

    # Check for API key in header or query param
    provided_key = request.headers.get("X-Api-Key") or request.query_params.get("api_key")
    if provided_key == configured_key:
        return await call_next(request)

    return JSONResponse(status_code=401, content={"detail": "Invalid or missing API key"})


# Auth check endpoint
@app.get("/api/auth/check")
async def auth_check(request: Request):
    """Check if authentication is required and if the provided key is valid."""
    import aiosqlite
    try:
        db = await aiosqlite.connect(DB_PATH)
        try:
            async with db.execute("SELECT value FROM settings WHERE key = 'api_key'") as cur:
                row = await cur.fetchone()
                configured_key = row[0] if row and row[0] else None
        finally:
            await db.close()
    except Exception:
        configured_key = None

    if not configured_key:
        return {"auth_required": False, "authenticated": True}

    provided_key = request.headers.get("X-Api-Key") or request.query_params.get("api_key")
    return {"auth_required": True, "authenticated": provided_key == configured_key}


# Include routers
app.include_router(jobs_router)
app.include_router(scan_router)
app.include_router(schedule_router)
app.include_router(settings_router)
app.include_router(stats_router)
app.include_router(rules_router)

from backend.routes.posters import router as poster_router
app.include_router(poster_router)


@app.get("/api/health")
async def health_check():
    """Health endpoint for Docker HEALTHCHECK and monitoring tools."""
    return {"status": "ok"}


@app.get("/api/logs")
async def get_logs(
    limit: int = Query(200, ge=1, le=2000),
    source: str = Query(""),
    search: str = Query(""),
):
    from backend.logstream import log_buffer
    return log_buffer.get_recent(limit, source, search)


async def _check_ws_auth(websocket: WebSocket) -> bool:
    """Check API key for WebSocket connections via query param."""
    configured_key = _api_key_cache.get("key")
    if not configured_key:
        return True
    provided = websocket.query_params.get("api_key", "")
    return provided == configured_key


@app.websocket("/ws/logs")
async def logs_websocket(websocket: WebSocket):
    if not await _check_ws_auth(websocket):
        await websocket.close(code=4001)
        return
    from backend.logstream import log_buffer
    await websocket.accept()
    queue: asyncio.Queue = asyncio.Queue()
    log_buffer.add_subscriber(queue)
    try:
        while True:
            entry = await queue.get()
            await websocket.send_json(entry)
    except WebSocketDisconnect:
        pass
    finally:
        log_buffer.remove_subscriber(queue)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if not await _check_ws_auth(websocket):
        await websocket.close(code=4001)
        return
    await ws_manager.connect(websocket)
    try:
        while True:
            # Keep connection open; receive and ignore client messages
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


# Mount frontend static files LAST
frontend_dist = Path(__file__).parent.parent / "frontend" / "dist"
if frontend_dist.exists():
    from fastapi.responses import FileResponse

    # Serve static assets (js, css, images) directly
    app.mount("/assets", StaticFiles(directory=str(frontend_dist / "assets")), name="static-assets")

    # Serve other static files from dist root (favicon, logos, etc.)
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        # Try to serve the exact file first
        file_path = frontend_dist / full_path
        if full_path and file_path.is_file():
            return FileResponse(file_path)
        # Otherwise serve index.html for SPA routing
        return FileResponse(frontend_dist / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=6680, reload=True)
