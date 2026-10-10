"""Doctor (v0.10.0): checks what Shrinkerr depends on — the media folders
(there, readable, writable, room to write), its data folder and database,
ffmpeg and the encoder, the connections, the remote workers — and a
diagnostics bundle for bug reports, with the secrets taken out.

Each check is {id, status: ok / warn / error / info, target, data}; the
page words it (frontend locales/*/doctor.json)."""
import asyncio
import dataclasses
import io
import json
import os
import platform
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone

import aiosqlite

from backend.database import DB_PATH

GB = 1024 ** 3
_FOLDER_TIMEOUT = 15  # seconds: a stalled network share


def _check(check_id: str, status: str, target: str | None = None, **data) -> dict:
    return {"id": check_id, "status": status, "target": target, "data": data}


async def _settings(db) -> dict[str, str]:
    async with db.execute("SELECT key, value FROM settings") as cur:
        return {k: v for k, v in await cur.fetchall()}


# ── Folders ──────────────────────────────────────────────────────────────

def _folder_state(path: str) -> tuple[str, dict]:
    """Blocking. Writability from the permissions and the mount — not by
    writing a file: deleting it on a NAS share can land in its recycle bin."""
    if not os.path.isdir(path):
        return "missing", {}
    try:
        with os.scandir(path) as entries:
            next(entries, None)
    except OSError:
        return "unreadable", {}
    usage = shutil.disk_usage(path)
    data = {"free": usage.free, "total": usage.total}
    try:
        readonly = bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except (OSError, AttributeError):
        readonly = False
    if readonly or not os.access(path, os.W_OK):
        return "readonly", data
    return "ok", data


async def check_media_folders(db, settings: dict) -> list[dict]:
    async with db.execute("SELECT path FROM media_dirs WHERE enabled = 1 ORDER BY path") as cur:
        paths = [r[0] for r in await cur.fetchall()]
    if not paths:
        return [_check("media_folder", "warn", None, problem="none")]
    try:
        threshold = float(settings.get("disk_space_threshold_gb") or 50) * GB
    except ValueError:
        threshold = 50 * GB

    async def one(path: str) -> dict:
        try:
            state, data = await asyncio.wait_for(asyncio.to_thread(_folder_state, path), _FOLDER_TIMEOUT)
        except TimeoutError:
            return _check("media_folder", "error", path, problem="timeout")
        if state != "ok":
            return _check("media_folder", "error", path, problem=state, **data)
        if data["free"] < threshold:
            return _check("media_folder", "warn", path, problem="low_space", threshold=threshold, **data)
        return _check("media_folder", "ok", path, **data)
    return list(await asyncio.gather(*(one(p) for p in paths)))


def _data_folder_state(path: str) -> tuple[str, dict]:
    """Blocking. The data folder is Shrinkerr's own: a test file is fine."""
    probe = os.path.join(path, f".doctor-{os.getpid()}")
    try:
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError:
        return "readonly", {}
    usage = shutil.disk_usage(path)
    return "ok", {"free": usage.free, "total": usage.total}


async def check_data_folder() -> list[dict]:
    path = os.path.dirname(os.path.abspath(DB_PATH))
    state, data = await asyncio.to_thread(_data_folder_state, path)
    if state != "ok":
        return [_check("data_folder", "error", path, problem=state)]
    if data["free"] < 2 * GB:
        return [_check("data_folder", "warn", path, problem="low_space", **data)]
    return [_check("data_folder", "ok", path, **data)]


def _quick_check(path: str) -> str:
    import sqlite3
    con = sqlite3.connect(path, timeout=30)
    try:
        return str(con.execute("PRAGMA quick_check").fetchone()[0])
    finally:
        con.close()


async def check_database() -> list[dict]:
    result = await asyncio.to_thread(_quick_check, DB_PATH)
    size = sum(os.path.getsize(p) for p in (DB_PATH, DB_PATH + "-wal") if os.path.exists(p))
    if result != "ok":
        return [_check("database", "error", DB_PATH, size=size, detail=result.splitlines()[0][:300])]
    return [_check("database", "ok", DB_PATH, size=size)]


# ── ffmpeg and the encoder ───────────────────────────────────────────────

async def _tool_version(tool: str) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            tool, "-version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), 10)
    except (OSError, TimeoutError):
        return None
    first = out.decode(errors="replace").splitlines()[0] if out else ""
    return first.split(" version ", 1)[1].split(" ", 1)[0] if " version " in first else first or None


async def check_ffmpeg(settings: dict) -> list[dict]:
    out = []
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        version = await _tool_version(tool) if path else None
        out.append(_check(tool, "ok", path, version=version) if version
                   else _check(tool, "error", path, problem="missing"))
    from backend.test_encode import check_vmaf_available
    out.append(_check("vmaf", "ok" if await check_vmaf_available() else "info"))

    from backend.encoder_caps import detect_encoders
    caps = await asyncio.to_thread(detect_encoders)
    available = ["libx265"] + [name for name, ok in (
        ("nvenc", caps.nvenc), ("qsv", caps.qsv), ("vaapi", caps.vaapi), ("videotoolbox", caps.videotoolbox)) if ok]
    chosen = (settings.get("default_encoder") or "nvenc").lower()
    chosen = {"hevc_nvenc": "nvenc", "x265": "libx265", "cpu": "libx265"}.get(chosen, chosen)
    out.append(_check("encoder", "ok" if chosen in available else "warn", chosen, available=available))
    return out


# ── Connections and workers ──────────────────────────────────────────────

_SERVICES = (("plex", "plex_url"), ("jellyfin", "jellyfin_url"), ("emby", "emby_url"),
             ("sonarr", "sonarr_url"), ("radarr", "radarr_url"))


async def check_connections(settings: dict) -> list[dict]:
    from backend.routes.settings import TestApiRequest, test_api_key

    async def one(service: str) -> dict:
        try:
            r = await asyncio.wait_for(test_api_key(TestApiRequest(service=service)), 30)
        except TimeoutError:
            r = {"success": False, "error": "timed out"}
        if r.get("success"):
            return _check("connection", "ok", service, version=r.get("version") or r.get("server_name"))
        return _check("connection", "error", service, detail=str(r.get("error") or r.get("message") or "")[:300])

    services = [s for s, key in _SERVICES if (settings.get(key) or "").strip()]
    checks = list(await asyncio.gather(*(one(s) for s in services)))
    from backend.metadata import resolve_tmdb_key_sync
    if (settings.get("tmdb_api_key") or "").strip():
        checks.append(await one("tmdb"))
    elif resolve_tmdb_key_sync(""):
        checks.append(_check("connection", "ok", "tmdb", bundled=True))
    else:
        checks.append(_check("connection", "warn", "tmdb", problem="no_key"))
    return checks


async def check_nodes(db) -> list[dict]:
    async with db.execute("SELECT name, last_heartbeat FROM worker_nodes WHERE id != 'local' ORDER BY name") as cur:
        rows = await cur.fetchall()
    now = datetime.now(timezone.utc)
    out = []
    for name, beat in rows:
        try:
            seen = datetime.fromisoformat(beat)
            seen = seen if seen.tzinfo else seen.replace(tzinfo=timezone.utc)
            age = (now - seen).total_seconds()
        except (TypeError, ValueError):
            age = None
        status = "ok" if age is not None and age < 120 else "warn"
        out.append(_check("node", status, name, last_seen=beat, age=round(age) if age is not None else None))
    return out


def check_auth(settings: dict) -> list[dict]:
    on = (settings.get("auth_enabled") or "").lower() == "true" and bool(settings.get("auth_password_hash"))
    return [_check("auth", "ok" if on else "info")]


async def run_checks() -> dict:
    db = await aiosqlite.connect(DB_PATH)
    try:
        await db.execute("PRAGMA busy_timeout=10000")
        settings = await _settings(db)
        groups = await asyncio.gather(
            check_media_folders(db, settings), check_data_folder(), check_database(),
            check_ffmpeg(settings), check_connections(settings), check_nodes(db))
    finally:
        await db.close()
    checks = [c for group in groups for c in group] + check_auth(settings)
    from backend.routes.stats import _get_current_version
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "version": _get_current_version(),
            "checks": checks}


# ── Diagnostics bundle ───────────────────────────────────────────────────

# Secrets, and the personal details a public bug report shouldn't carry.
_SECRET_KEY = re.compile(r"key|token|password|secret|webhook|session|pass$|smtp_user|smtp_from|chat_id", re.IGNORECASE)
_SECRET_IN_TEXT = re.compile(
    r"(?i)(x-plex-token|x-api-key|api[_-]?key|apikey|token|password|secret)([=:]\s*\"?)([^\s&\"',;]{4,})")


def _is_secret(key: str) -> bool:
    return bool(_SECRET_KEY.search(key))


class Scrubber:
    """Takes every secret Shrinkerr knows (and anything that looks like one)
    out of text that goes into the bundle."""

    def __init__(self, settings: dict):
        values = [v for k, v in settings.items() if _is_secret(k) and v and len(v) >= 6]
        values += [v for k, v in os.environ.items() if _is_secret(k) and v and len(v) >= 6]
        self.values = sorted(set(values), key=len, reverse=True)

    def __call__(self, text) -> str:
        text = "" if text is None else str(text)
        for value in self.values:
            text = text.replace(value, "***")
        return _SECRET_IN_TEXT.sub(lambda m: m.group(1) + m.group(2) + "***", text)


async def diagnostics_bundle() -> tuple[str, bytes]:
    """(file name, zip bytes): the checks, settings and environment without
    secrets, the library's shape, the recent log and failed jobs."""
    doctor = await run_checks()
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        settings = await _settings(db)
        async with db.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status") as cur:
            jobs = {r["status"]: r["n"] for r in await cur.fetchall()}
        async with db.execute("SELECT COUNT(*) FROM scan_results WHERE removed_from_list = 0") as cur:
            scanned = (await cur.fetchone())[0]
        async with db.execute("SELECT path, label, enabled FROM media_dirs ORDER BY path") as cur:
            media_dirs = [dict(r) for r in await cur.fetchall()]
        async with db.execute("SELECT COUNT(*) FROM encoding_rules WHERE enabled = 1") as cur:
            rules = (await cur.fetchone())[0]
        async with db.execute(
            "SELECT name, status, capabilities, ffmpeg_version, gpu_name, os_info, last_heartbeat "
            "FROM worker_nodes ORDER BY name") as cur:
            nodes = [dict(r) for r in await cur.fetchall()]
        async with db.execute(
            "SELECT id, file_path, job_type, encoder, completed_at, error_key, error_log, ffmpeg_command "
            "FROM jobs WHERE status = 'failed' ORDER BY completed_at DESC LIMIT 25") as cur:
            failed = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()

    scrub = Scrubber(settings)
    from backend.encoder_caps import detect_encoders
    from backend.logstream import log_buffer
    caps = await asyncio.to_thread(detect_encoders)
    system = {
        "version": doctor["version"],
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
        "encoders": dataclasses.asdict(caps),
        "environment": {k: ("***" if _is_secret(k) else v) for k, v in sorted(os.environ.items())
                        if k.startswith("SHRINKERR_") or k in ("TZ", "PUID", "PGID", "UMASK")},
    }
    files = {
        "doctor.json": doctor,
        "settings.json": {k: ("***" if _is_secret(k) and v else v) for k, v in sorted(settings.items())},
        "system.json": system,
        "library.json": {"jobs": jobs, "scanned_files": scanned, "media_dirs": media_dirs,
                         "enabled_rules": rules, "nodes": nodes},
        "failed_jobs.json": [{**j, "error_log": scrub(j["error_log"])[-4000:],
                              "ffmpeg_command": scrub(j["ffmpeg_command"])} for j in failed],
    }
    log = "\n".join(scrub(f'{e["timestamp"]} {e["level"]:<5} {e["source"]:<10} {e["message"]}')
                    for e in log_buffer.get_recent(2000))
    readme = ("Shrinkerr diagnostics — attach this to a bug report.\n\n"
              "Passwords, API keys and tokens are replaced with ***. File and folder\n"
              "paths are included (they help find the problem); look through the files\n"
              "before sharing if that matters to you.\n")

    def build() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("README.txt", readme)
            for name, content in files.items():
                z.writestr(name, scrub(json.dumps(content, indent=2, default=str)))
            z.writestr("log.txt", log)
        return buf.getvalue()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return f"shrinkerr-diagnostics-{doctor['version']}-{stamp}.zip", await asyncio.to_thread(build)
