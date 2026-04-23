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

# Configure logging so shrinkerr.* loggers output to console
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


async def _bootstrap_auth_defaults() -> None:
    """Generate an api_key on a true fresh install, warn on insecure upgrades.

    Two scenarios:

    * Fresh install (settings table completely empty): mint a strong random
      api_key + a session_secret, enable password-auth by default, and print
      the generated key prominently so the user can configure their client.
    * Existing install with api_key='' and auth_enabled=false: leave state
      alone (don't lock out an existing deployment mid-upgrade) but print a
      loud warning so the admin knows they're running unauthenticated.
    """
    import secrets
    import sqlite3
    try:
        db = sqlite3.connect(DB_PATH)
        try:
            row_count = db.execute("SELECT COUNT(*) FROM settings").fetchone()[0]
            existing = {
                r[0]: r[1]
                for r in db.execute(
                    "SELECT key, value FROM settings "
                    "WHERE key IN ('api_key','auth_enabled','session_secret')"
                ).fetchall()
            }
            if row_count == 0:
                # True fresh install — secure-by-default.
                generated_key = secrets.token_hex(24)
                generated_secret = secrets.token_hex(32)
                db.executemany(
                    "INSERT INTO settings (key, value) VALUES (?, ?)",
                    [
                        ("api_key", generated_key),
                        ("auth_enabled", "true"),
                        ("session_secret", generated_secret),
                    ],
                )
                db.commit()
                banner = "!" * 78
                print(banner, flush=True)
                print("[SECURITY] Fresh install detected — generated an API key.", flush=True)
                print(f"[SECURITY] API KEY: {generated_key}", flush=True)
                print("[SECURITY] Use it as the X-Api-Key header, or set a password in", flush=True)
                print("[SECURITY] Settings → System → Authentication.", flush=True)
                print(banner, flush=True)
                return

            api_key = (existing.get("api_key") or "").strip()
            auth_enabled = (existing.get("auth_enabled") or "false").strip().lower() == "true"
            session_secret = (existing.get("session_secret") or "").strip()

            # Always ensure `session_secret` exists. The HMAC signing path
            # now refuses to issue/validate sessions when it's empty (old
            # code fell back to the literal string "default-secret",
            # which meant every install that hadn't touched settings used
            # an identical forgeable key). Auto-generate so the fail-
            # closed path never fires for a legitimate user.
            if not session_secret:
                db.execute(
                    "INSERT INTO settings (key, value) VALUES ('session_secret', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (secrets.token_hex(32),),
                )
                db.commit()
                print("[SECURITY] Generated session_secret (was empty).", flush=True)

            if not api_key and not auth_enabled:
                banner = "!" * 78
                print(banner, flush=True)
                print("[SECURITY] WARNING: this instance is running WITHOUT authentication.", flush=True)
                print("[SECURITY] Anyone with network access can read your library, queue", flush=True)
                print("[SECURITY] transcodes against arbitrary paths, and change dangerous", flush=True)
                print("[SECURITY] settings like `post_conversion_script` (RCE vector).", flush=True)
                print("[SECURITY] Set an API key in Settings → System → Authentication, or", flush=True)
                print("[SECURITY] bind the port to 127.0.0.1 and front with a reverse proxy.", flush=True)
                print(banner, flush=True)
        finally:
            db.close()
    except Exception as exc:
        print(f"[SECURITY] Bootstrap skipped ({exc}); continuing.", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Init log capture BEFORE anything else prints
    from backend.logstream import init_logstream
    init_logstream()

    await init_db()
    await _bootstrap_auth_defaults()
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

    # Background: poll GitHub's latest-release endpoint so the sidebar's
    # "Update available" pill surfaces new versions to the running
    # container without the user having to docker compose pull first.
    # Pulls once at startup and then every 30 min (see stats.py). Matches
    # the UX of Sonarr/Radarr/Plex where updates are advertised to the
    # running instance, not only after a manual image refresh.
    from backend.routes.stats import update_check_loop
    version_check_task = asyncio.create_task(update_check_loop())

    yield
    worker.stop()
    watcher.stop()
    backup_task.cancel()
    stale_task.cancel()
    local_heartbeat_task.cancel()
    version_check_task.cancel()


app = FastAPI(title="Shrinkerr", lifespan=lifespan)

# Auth settings cache (replaces old _api_key_cache)
_auth_cache: dict = {"settings": None, "checked_at": 0}


def _get_auth_settings_sync() -> dict | None:
    """Synchronous auth settings check — cached for 60s.

    Returns None when the auth settings can't be read (DB locked, missing, etc.).
    The middleware uses that to **fail closed** with a 503 rather than admit
    traffic under a permissive default — the old behaviour turned a transient
    DB error into a full auth bypass.
    """
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
        return _auth_cache["settings"]
    except Exception as exc:
        # Fail closed if we've never successfully loaded settings. If we have
        # a cached copy from a previous successful read, serve that — stale
        # but authoritative — rather than flipping to unauthenticated.
        if _auth_cache.get("settings"):
            print(f"[AUTH] settings read failed, serving cached: {exc}", flush=True)
            return _auth_cache["settings"]
        print(f"[AUTH] settings read failed and no cached copy: {exc}", flush=True)
        return None


def _validate_session(token: str, auth_settings: dict) -> bool:
    """Validate a session cookie token.

    Refuses the token if `session_secret` is empty — the old code fell
    back to the literal string `"default-secret"` which made every
    install's sessions trivially forgeable whenever the secret hadn't
    been generated yet.
    """
    try:
        parts = token.split(":")
        if len(parts) != 3:
            return False
        username, timestamp, signature = parts
        secret = (auth_settings.get("session_secret") or "").strip()
        if not secret:
            return False  # fail closed — no silent fallback to a known constant
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


# -----------------------------------------------------------------------
# Password hashing
# -----------------------------------------------------------------------
# bcrypt replaces the legacy unsalted SHA-256 scheme. The verify path
# handles both formats so existing installs keep working: a successful
# login with a legacy SHA-256 hash triggers a one-shot re-hash to bcrypt
# at that moment, transparent to the user.

_BCRYPT_PREFIXES = (b"$2a$", b"$2b$", b"$2y$")


def _hash_password(password: str) -> str:
    """Hash a password with bcrypt (cost 12)."""
    import bcrypt
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def _verify_password(password: str, username: str, stored_hash: str) -> bool:
    """Verify a password against either a bcrypt hash or the legacy
    SHA-256(password + username) hash. Callers should call
    `_maybe_upgrade_password_hash` after a successful legacy match to
    move the stored value over to bcrypt."""
    import bcrypt
    if not stored_hash:
        return False
    stored_bytes = stored_hash.encode("utf-8")
    if any(stored_bytes.startswith(p) for p in _BCRYPT_PREFIXES):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), stored_bytes)
        except (ValueError, TypeError):
            return False
    # Legacy path: SHA-256(password + username) hex
    legacy = hashlib.sha256((password + username).encode()).hexdigest()
    return hmac.compare_digest(legacy, stored_hash)


async def _maybe_upgrade_password_hash(username: str, password: str, stored_hash: str) -> None:
    """If the stored hash is in the legacy format, rewrite it with bcrypt
    transparently during a successful login."""
    if not stored_hash:
        return
    if any(stored_hash.encode().startswith(p) for p in _BCRYPT_PREFIXES):
        return  # already on bcrypt
    try:
        import aiosqlite
        new_hash = _hash_password(password)
        db = await aiosqlite.connect(DB_PATH)
        try:
            await db.execute(
                "UPDATE settings SET value = ? WHERE key = 'auth_password_hash'",
                (new_hash,),
            )
            await db.commit()
            print(f"[AUTH] Upgraded password hash for '{username}' to bcrypt", flush=True)
        finally:
            await db.close()
        # Kick the auth cache so subsequent middleware lookups see the new hash
        _auth_cache["checked_at"] = 0
    except Exception as exc:
        print(f"[AUTH] Password hash upgrade failed (will retry next login): {exc}", flush=True)


# -----------------------------------------------------------------------
# Login rate limiter — in-memory per-IP leaky bucket.
# -----------------------------------------------------------------------
# Small enough to hand-roll rather than pull slowapi. Guards against
# online password guessing; not an answer to a real distributed attack,
# but with bcrypt behind it a single source can't burn through the
# keyspace. Cleared when the process restarts; that's fine — attacks
# survive restarts much less often than the legitimate admin does.

_LOGIN_BUCKET: dict[str, list[float]] = {}
_LOGIN_BUCKET_WINDOW = 60.0       # seconds
_LOGIN_BUCKET_MAX = 8              # attempts per window per IP


def _login_allowed(remote_ip: str) -> bool:
    """Return True if another login attempt from this IP is allowed.
    Records the attempt on return-True."""
    now = time.time()
    bucket = _LOGIN_BUCKET.setdefault(remote_ip, [])
    # Drop old entries
    cutoff = now - _LOGIN_BUCKET_WINDOW
    bucket[:] = [t for t in bucket if t >= cutoff]
    if len(bucket) >= _LOGIN_BUCKET_MAX:
        return False
    bucket.append(now)
    # Opportunistic cleanup — prevent the dict growing unbounded
    if len(_LOGIN_BUCKET) > 2048:
        for ip in list(_LOGIN_BUCKET.keys()):
            entries = [t for t in _LOGIN_BUCKET[ip] if t >= cutoff]
            if entries:
                _LOGIN_BUCKET[ip] = entries
            else:
                _LOGIN_BUCKET.pop(ip, None)
    return True


# Endpoints that require an API key even when the UI password-auth
# (`auth_enabled`) is off. These are machine-to-machine integration surfaces
# where anonymous access is dangerous regardless of the user's top-level
# auth toggle — we always want to demand a shared secret on them.
_AUTH_ALWAYS_REQUIRED_PREFIXES = (
    "/api/webhooks/",                   # NZBGet / SABnzbd / arr post-processing
    "/api/nodes/",                       # remote worker registration + job pull
    "/api/settings/backup/download",     # DB snapshot incl. every stored secret
    "/api/settings/backup/restore",      # replaces the DB — trivial takeover if open
    "/api/settings/nzbget-config",       # returns unmasked integration api_keys
    "/api/settings/nzbget-script",       # script with the api_key baked in
    "/api/settings/sabnzbd-script",      # same
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Attach baseline security headers to every response.

    None of these meaningfully restrict the self-hosted SPA's own
    behaviour; they just make sure that if the app is ever loaded in a
    hostile iframe / MIME-sniffed / reflected-content scenario, the
    browser treats it sensibly. No CSP script-src is set because the
    React bundle relies on inline-style attributes.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    # frame-ancestors 'none' is the CSP-native equivalent of X-Frame-Options
    # DENY and also works on newer browsers that ignore the older header.
    response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


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

    # Fail closed on DB errors. The old code flipped to unauthenticated here
    # whenever the settings read raised — a transient SQLite lock was enough
    # to drop auth for the whole process. Now a read failure is a 503 and
    # the request is rejected, not admitted.
    auth_settings = _get_auth_settings_sync()
    if auth_settings is None:
        return JSONResponse(status_code=503, content={"detail": "Auth subsystem unavailable"})

    configured_api_key = auth_settings.get("api_key") or ""
    password_auth_on = bool(auth_settings.get("auth_enabled"))

    # Integration endpoints always require the api_key even when password
    # auth is off — otherwise a LAN-exposed install hands out RCE-adjacent
    # primitives (webhook path injection, worker-node spoofing, backup
    # download) to anyone who can reach port 6680.
    needs_auth = False
    if any(path.startswith(p) for p in _AUTH_ALWAYS_REQUIRED_PREFIXES):
        needs_auth = True
    elif configured_api_key or password_auth_on:
        # General case: gate the whole /api/ surface whenever ANY auth is
        # configured. The old fail-open behaviour (only gating when
        # `auth_enabled=true`) meant setting an api_key without flipping
        # the password toggle left the app wide open.
        needs_auth = True

    if not needs_auth:
        return await call_next(request)

    # Method 1: API key (constant-time compare — plain `==` leaks timing).
    supplied_key = request.headers.get("X-Api-Key") or request.query_params.get("api_key") or ""
    if supplied_key and configured_api_key and hmac.compare_digest(supplied_key, configured_api_key):
        return await call_next(request)

    # Method 2: UI session cookie — only meaningful when the user has
    # enabled password auth.
    if password_auth_on:
        session_cookie = request.cookies.get("shrinkerr_session")
        if session_cookie and _validate_session(session_cookie, auth_settings):
            return await call_next(request)

    # Endpoint needed auth but got none (or needed a key specifically but
    # caller sent only a session). No configured credential path = 503
    # rather than locking the admin out.
    if not configured_api_key and not password_auth_on:
        return JSONResponse(status_code=503, content={
            "detail": "This endpoint requires an API key. Set one in Settings → System → Authentication.",
        })
    return JSONResponse(status_code=401, content={"detail": "Authentication required"})


# Auth check endpoint
@app.get("/api/auth/check")
async def auth_check(request: Request):
    """Check if authentication is required and if the current request is authenticated."""
    settings = _get_auth_settings_sync()
    if settings is None:
        return JSONResponse(
            status_code=503,
            content={"auth_required": True, "authenticated": False, "method": None,
                     "error": "Auth subsystem unavailable"},
        )

    configured_api_key = (settings.get("api_key") or "").strip()
    password_auth_on = bool(settings.get("auth_enabled"))

    # No auth configured at all — treat as "nothing required".
    if not configured_api_key and not password_auth_on:
        return {"auth_required": False, "authenticated": True, "method": None}

    # Check API key (constant-time; old `==` comparison was an online
    # timing oracle against this unauthenticated endpoint).
    supplied_key = request.headers.get("X-Api-Key") or request.query_params.get("api_key") or ""
    if supplied_key and configured_api_key and hmac.compare_digest(supplied_key, configured_api_key):
        return {"auth_required": True, "authenticated": True, "method": "api_key"}

    # Check session cookie — only when password auth is enabled.
    if password_auth_on:
        session = request.cookies.get("shrinkerr_session")
        if session and _validate_session(session, settings):
            return {"auth_required": True, "authenticated": True, "method": "session"}

    return {"auth_required": True, "authenticated": False, "method": None}


@app.post("/api/auth/login")
async def auth_login(request: Request):
    """Authenticate with username/password and set a session cookie."""
    # Rate limit — 8 attempts per minute per IP. Blunts online
    # brute-force against weak passwords; combined with bcrypt (cost 12)
    # a single source can't make meaningful progress.
    remote_ip = (request.client.host if request.client else "unknown") or "unknown"
    if not _login_allowed(remote_ip):
        return JSONResponse(
            {"error": "Too many login attempts. Try again in a minute."},
            status_code=429,
        )

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
    secret = (settings.get("session_secret") or "").strip()
    if not secret:
        # Fail closed — without a session secret we can't safely issue a
        # session cookie (old code fell back to the literal string
        # "default-secret" → every install's sessions were forgeable).
        return JSONResponse(
            {"error": "Server misconfiguration: session secret not set"},
            status_code=503,
        )

    # Always hash the candidate password (even when username is wrong) so
    # timing can't be used to enumerate usernames. The _verify_password
    # call does the bcrypt/SHA-256 dispatch; we only accept the result
    # when the username also matches.
    password_ok = _verify_password(password, stored_username, stored_hash)
    username_ok = bool(stored_username) and hmac.compare_digest(username, stored_username)
    if not (username_ok and password_ok):
        return JSONResponse({"error": "Invalid credentials"}, status_code=401)

    # One-shot upgrade of legacy SHA-256 hashes to bcrypt on first
    # successful login. Runs in the background; the response doesn't
    # wait for the DB update.
    import asyncio as _asyncio
    _asyncio.create_task(_maybe_upgrade_password_hash(stored_username, password, stored_hash))

    # Create session token
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), f"{username}:{timestamp}".encode(), hashlib.sha256
    ).hexdigest()
    token = f"{username}:{timestamp}:{signature}"

    response = JSONResponse({"success": True, "username": username})
    # Set Secure when the request came in over HTTPS — reverse-proxy users
    # on HTTPS get the stricter cookie, local HTTP dev stays functional.
    is_https = (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "").lower() == "https"
    )
    response.set_cookie(
        "shrinkerr_session", token,
        httponly=True, samesite="lax", max_age=86400 * 30,
        secure=is_https,
    )
    return response


@app.post("/api/auth/logout")
async def auth_logout():
    """Clear the session cookie."""
    response = JSONResponse({"success": True})
    response.delete_cookie("shrinkerr_session")
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
    session = websocket.cookies.get("shrinkerr_session")
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
