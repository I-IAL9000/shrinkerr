import asyncio
import hashlib
import hmac
import logging
import os
import time
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

    Only checks directories of pending CONVERT/COMBINED jobs (health_check jobs
    don't produce temp files). Hard cap on directories scanned so a flooded
    queue can't stall startup.
    """
    import aiosqlite
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT DISTINCT file_path FROM jobs "
            "WHERE status = 'pending' AND job_type IN ('convert', 'combined') "
            "LIMIT 500"
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
    """Populate ignored_files from completed CONVERT/COMBINED jobs with no savings."""
    import aiosqlite
    db = await aiosqlite.connect(DB_PATH)
    try:
        async with db.execute(
            "SELECT file_path FROM jobs WHERE status = 'completed' "
            "AND job_type IN ('convert', 'combined') AND space_saved <= 0"
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
    # Backfill file_events from existing jobs — run in BACKGROUND so a huge
    # jobs table can't block startup and cause Docker to kill the container.
    import asyncio as _asyncio
    async def _bg_backfill_events():
        try:
            from backend.file_events import backfill_from_jobs
            await backfill_from_jobs()
        except Exception as exc:
            print(f"[STARTUP] file_events backfill skipped: {exc}", flush=True)
    _asyncio.create_task(_bg_backfill_events())
    # Initialize VMAF check and clean test encode temp files
    from backend.test_encode import check_vmaf_available, cleanup_temp_dir
    await check_vmaf_available()
    cleanup_temp_dir()
    # Load IMDb ratings dataset
    from backend.imdb_ratings import ensure_ratings
    await ensure_ratings()
    # Initialize node manager and register local worker
    from backend.nodes import NodeManager, stale_release_loop
    node_manager = NodeManager()
    await node_manager.register_local_node()

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
    app.state.node_manager = node_manager
    init_job_routes(worker, queue)
    init_scheduler(worker.start)
    watcher.start()
    # Weekly database backup task (keeps last 4)
    import asyncio
    from backend.routes.settings import scheduled_backup_loop
    backup_task = asyncio.create_task(scheduled_backup_loop())
    # Background: stale node detection + job release
    stale_task = asyncio.create_task(stale_release_loop(node_manager))

    # Background: keep the built-in "local" node's last_heartbeat fresh so
    # the Nodes page doesn't show the server itself going stale (it clearly
    # hasn't — we're the one rendering the page). 30s cadence matches the
    # remote-worker heartbeat interval.
    async def _local_heartbeat_loop():
        try:
            while True:
                try:
                    await node_manager.touch_local_heartbeat()
                except Exception as exc:
                    print(f"[NODES] Local heartbeat refresh failed: {exc}", flush=True)
                await asyncio.sleep(30)
        except asyncio.CancelledError:
            pass
    local_heartbeat_task = asyncio.create_task(_local_heartbeat_loop())

    yield
    worker.stop()
    watcher.stop()
    backup_task.cancel()
    stale_task.cancel()
    local_heartbeat_task.cancel()


app = FastAPI(title="Squeezarr", lifespan=lifespan)

# Auth settings cache (replaces old _api_key_cache)
_auth_cache: dict = {"settings": None, "checked_at": 0}


def _get_auth_settings_sync() -> dict:
    """Synchronous auth settings check -- cached for 60s."""
    now = time.monotonic()
    if now - _auth_cache.get("checked_at", 0) < 60 and _auth_cache.get("settings"):
        return _auth_cache["settings"]
    try:
        import sqlite3
        db = sqlite3.connect(DB_PATH)
        try:
            cur = db.execute(
                "SELECT key, value FROM settings WHERE key IN "
                "('auth_enabled','auth_username','auth_password_hash','api_key','session_secret')"
            )
            settings = {r[0]: r[1] for r in cur.fetchall()}
        finally:
            db.close()
        _auth_cache["settings"] = {
            "auth_enabled": settings.get("auth_enabled", "false") == "true",
            "api_key": settings.get("api_key", ""),
            "auth_username": settings.get("auth_username", ""),
            "auth_password_hash": settings.get("auth_password_hash", ""),
            "session_secret": settings.get("session_secret", ""),
        }
        _auth_cache["checked_at"] = now
    except Exception:
        if not _auth_cache.get("settings"):
            _auth_cache["settings"] = {"auth_enabled": False, "api_key": "", "session_secret": ""}
    return _auth_cache["settings"]


def _validate_session(token: str, auth_settings: dict) -> bool:
    """Validate a session cookie token."""
    try:
        parts = token.split(":")
        if len(parts) != 3:
            return False
        username, timestamp, signature = parts
        secret = auth_settings.get("session_secret", "default-secret")
        expected = hmac.new(
            secret.encode(), f"{username}:{timestamp}".encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        # Check token age (30 days max)
        age = time.time() - int(timestamp)
        return age < 86400 * 30
    except Exception:
        return False


@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    path = request.url.path

    # Always allow: health, static assets, poster images, SPA routes, websocket
    if (
        path in ("/api/health",)
        or path == "/api/posters/image"
        or path.startswith("/assets/")
        or not path.startswith("/api/")
    ):
        return await call_next(request)

    # Always allow auth endpoints
    if path in ("/api/auth/check", "/api/auth/login", "/api/auth/logout"):
        return await call_next(request)

    # Check if auth is enabled
    auth_settings = _get_auth_settings_sync()
    if not auth_settings.get("auth_enabled"):
        return await call_next(request)

    # Method 1: API key in header or query param
    api_key = request.headers.get("X-Api-Key") or request.query_params.get("api_key")
    if api_key and api_key == auth_settings.get("api_key"):
        return await call_next(request)

    # Method 2: Session cookie
    session_cookie = request.cookies.get("squeezarr_session")
    if session_cookie and _validate_session(session_cookie, auth_settings):
        return await call_next(request)

    return JSONResponse(status_code=401, content={"detail": "Authentication required"})


# Auth check endpoint
@app.get("/api/auth/check")
async def auth_check(request: Request):
    """Check if authentication is required and if the current request is authenticated."""
    settings = _get_auth_settings_sync()

    if not settings.get("auth_enabled"):
        return {"auth_required": False, "authenticated": True, "method": None}

    # Check API key
    api_key = request.headers.get("X-Api-Key") or request.query_params.get("api_key")
    if api_key and api_key == settings.get("api_key"):
        return {"auth_required": True, "authenticated": True, "method": "api_key"}

    # Check session cookie
    session = request.cookies.get("squeezarr_session")
    if session and _validate_session(session, settings):
        return {"auth_required": True, "authenticated": True, "method": "session"}

    return {"auth_required": True, "authenticated": False, "method": None}


@app.post("/api/auth/login")
async def auth_login(request: Request):
    """Authenticate with username/password and set a session cookie."""
    body = await request.json()
    username = body.get("username", "")
    password = body.get("password", "")

    # Read settings
    import aiosqlite
    db = await aiosqlite.connect(DB_PATH)
    try:
        settings = {}
        async with db.execute(
            "SELECT key, value FROM settings WHERE key IN "
            "('auth_enabled','auth_username','auth_password_hash','session_secret')"
        ) as cur:
            for row in await cur.fetchall():
                settings[row[0]] = row[1]
    finally:
        await db.close()

    if settings.get("auth_enabled", "false") != "true":
        return JSONResponse({"error": "Auth not enabled"}, status_code=400)

    stored_username = settings.get("auth_username", "")
    stored_hash = settings.get("auth_password_hash", "")

    if username != stored_username:
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)

    # Verify password
    password_hash = hashlib.sha256((password + stored_username).encode()).hexdigest()
    if password_hash != stored_hash:
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)

    # Create session token
    secret = settings.get("session_secret", "default-secret")
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), f"{username}:{timestamp}".encode(), hashlib.sha256
    ).hexdigest()
    token = f"{username}:{timestamp}:{signature}"

    response = JSONResponse({"success": True, "username": username})
    response.set_cookie(
        "squeezarr_session", token, httponly=True, samesite="lax", max_age=86400 * 30
    )
    return response


@app.post("/api/auth/logout")
async def auth_logout():
    """Clear the session cookie."""
    response = JSONResponse({"success": True})
    response.delete_cookie("squeezarr_session")
    return response


# Include routers
app.include_router(jobs_router)
app.include_router(scan_router)
app.include_router(schedule_router)
app.include_router(settings_router)
app.include_router(stats_router)
app.include_router(rules_router)

from backend.routes.posters import router as poster_router
app.include_router(poster_router)

from backend.routes.webhooks import router as webhook_router
app.include_router(webhook_router)

from backend.routes.activity import router as activity_router
app.include_router(activity_router)

from backend.routes.nodes import router as nodes_router
app.include_router(nodes_router)

from backend.routes.search import router as search_router
app.include_router(search_router)

from backend.routes.rename import router as rename_router
app.include_router(rename_router)

from backend.routes.arr import router as arr_router
app.include_router(arr_router)

from backend.routes.plex import router as plex_router
app.include_router(plex_router)


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
    """Check authentication for WebSocket connections via query param or cookie."""
    auth_settings = _get_auth_settings_sync()
    if not auth_settings.get("auth_enabled"):
        return True
    # Check API key in query param
    provided = websocket.query_params.get("api_key", "")
    if provided and provided == auth_settings.get("api_key"):
        return True
    # Check session cookie
    session = websocket.cookies.get("squeezarr_session")
    if session and _validate_session(session, auth_settings):
        return True
    return False


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
    # Worker mode: run as a remote worker instead of the server
    mode = os.environ.get("SHRINKERR_MODE", "server").lower()
    if mode == "worker":
        from backend.worker_mode import run_worker
        asyncio.run(run_worker())
    else:
        import uvicorn
        uvicorn.run("backend.main:app", host="0.0.0.0", port=6680)
