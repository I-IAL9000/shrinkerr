import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger("shrinkerr.queue")

# Minimum wall-clock interval between persisted progress writes for a
# single job. ffmpeg emits ~2 progress lines/sec/job, but the queue page
# only consults `jobs.progress` on a manual reload — live UI gets values
# via the WebSocket. With this throttle the DB sees one UPDATE every
# 3 seconds per job instead of every progress line, which kills a class of
# stalls where the WAL write lock was held by some other transaction and
# every progress callback queued behind it. Terminal updates
# (progress >= 99.99) always flush so the persisted final value lands.
# Tied to the WS throttle (500ms in websocket.py) by an order of magnitude
# — DB persistence is best-effort, the WebSocket is authoritative for live
# rendering. v0.3.36+.
_PROGRESS_DB_WRITE_INTERVAL = 3.0


async def _run_post_conversion_script(job_id: int, file_path: str, original_path: str, result: dict, job_data: dict):
    """Run user-configured post-conversion script with job details as env vars."""
    try:
        import sqlite3
        from backend.config import settings as app_settings
        db = sqlite3.connect(app_settings.db_path)
        try:
            cur = db.execute("SELECT value FROM settings WHERE key = 'post_conversion_script'")
            row = cur.fetchone()
            script = row[0] if row else ""
            cur = db.execute("SELECT value FROM settings WHERE key = 'post_conversion_script_timeout'")
            row = cur.fetchone()
            timeout = int(row[0]) if row else 300
        finally:
            db.close()
    except Exception:
        return

    if not script or not script.strip():
        return

    import os
    env = {**os.environ}
    # Post-conversion hook env vars. Primary names are SHRINKERR_*; legacy
    # SQUEEZARR_* are still emitted so existing user-owned hook scripts
    # from the old app name keep working. New scripts should read SHRINKERR_*.
    hook_vars = {
        "EVENT": "job_completed",
        "JOB_ID": str(job_id),
        "FILE_PATH": str(file_path),
        "ORIGINAL_PATH": str(original_path or file_path),
        "JOB_TYPE": str(job_data.get("job_type", "")),
        "SPACE_SAVED": str(result.get("space_saved", 0)),
        "ORIGINAL_SIZE": str(job_data.get("original_size", 0)),
        "ENCODER": str(job_data.get("encoder", "")),
        "PRESET": str(job_data.get("nvenc_preset", "")),
        "CQ": str(job_data.get("nvenc_cq", "")),
        "FPS": str(round(job_data.get("fps", 0) or 0, 1)),
        "VMAF_SCORE": str(result.get("vmaf_score", "")),
        "STATUS": "completed" if result.get("success") else "failed",
        "ERROR": str(result.get("error", "")),
    }
    for _k, _v in hook_vars.items():
        env[f"SHRINKERR_{_k}"] = _v
        env[f"SQUEEZARR_{_k}"] = _v  # legacy alias — remove in a future major version

    try:
        proc = await asyncio.create_subprocess_exec(
            script, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        rc = proc.returncode
        if rc != 0:
            print(f"[WORKER] Post-conversion script exited with code {rc}: {stderr.decode()[:200]}", flush=True)
        else:
            print(f"[WORKER] Post-conversion script completed successfully", flush=True)
    except asyncio.TimeoutError:
        print(f"[WORKER] Post-conversion script timed out after {timeout}s", flush=True)
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
    except FileNotFoundError:
        print(f"[WORKER] Post-conversion script not found: {script}", flush=True)
    except Exception as exc:
        print(f"[WORKER] Post-conversion script error: {exc}", flush=True)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobQueue:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def _connect(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=30000")
        return db

    async def add_job(
        self,
        file_path: str,
        job_type: str,
        encoder: Optional[str] = None,
        audio_tracks_to_remove: Optional[list] = None,
        subtitle_tracks_to_remove: Optional[list] = None,
        original_size: Optional[int] = None,
        nvenc_preset: Optional[str] = None,
        nvenc_cq: Optional[int] = None,
        audio_codec: Optional[str] = None,
        audio_bitrate: Optional[int] = None,
        libx265_crf: Optional[int] = None,
        libx265_preset: Optional[str] = None,
        target_resolution: Optional[str] = None,
        priority: int = 0,
        insert_next: bool = False,
    ) -> int:
        db = await self._connect()
        try:
            # Skip if file already has a pending or running job
            async with db.execute(
                "SELECT id FROM jobs WHERE file_path = ? AND status IN ('pending', 'running') LIMIT 1",
                (file_path,),
            ) as cur:
                existing = await cur.fetchone()
            if existing:
                return existing[0]

            if insert_next:
                # Insert at the front of the pending queue (right after running jobs)
                async with db.execute(
                    "SELECT COALESCE(MIN(queue_order), 1) FROM jobs WHERE status = 'pending'"
                ) as cur:
                    row = await cur.fetchone()
                    min_pending = (row[0] or 1)
                next_order = min_pending - 1
            else:
                # Get current max queue_order
                async with db.execute("SELECT COALESCE(MAX(queue_order), 0) FROM jobs") as cur:
                    row = await cur.fetchone()
                    next_order = (row[0] or 0) + 1

            audio_json = json.dumps(audio_tracks_to_remove or [])
            sub_json = json.dumps(subtitle_tracks_to_remove or [])
            now = _utcnow()
            async with db.execute(
                """INSERT INTO jobs
                   (file_path, job_type, status, encoder, audio_tracks_to_remove, subtitle_tracks_to_remove,
                    created_at, queue_order, original_size,
                    nvenc_preset, nvenc_cq, audio_codec, audio_bitrate, libx265_crf, libx265_preset, target_resolution, priority)
                   VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (file_path, job_type, encoder, audio_json, sub_json, now, next_order,
                 original_size or 0, nvenc_preset, nvenc_cq, audio_codec, audio_bitrate,
                 libx265_crf, libx265_preset, target_resolution, priority),
            ) as cur:
                job_id = cur.lastrowid
            await db.commit()
            return job_id
        finally:
            await db.close()

    async def _log_event(self, file_path: str, event_type: str, summary: str, details: dict | None = None) -> None:
        """Convenience wrapper around file_events.log_event (never raises)."""
        try:
            from backend.file_events import log_event
            await log_event(file_path, event_type, summary, details)
        except Exception:
            pass

    async def add_jobs_bulk(self, jobs: list[dict]) -> list[int]:
        """Insert many jobs in a single transaction — much faster than N add_job() calls.

        Each job dict supports the same fields as add_job() kwargs plus an optional
        "insert_next" boolean. Returns the list of inserted job IDs (0 for skipped
        duplicates). Files already having a pending/running job are skipped.
        """
        if not jobs:
            return []
        db = await self._connect()
        try:
            # 1. Find which files already have pending/running jobs (batched)
            file_paths = [j["file_path"] for j in jobs]
            existing: set[str] = set()
            CHUNK = 900
            for i in range(0, len(file_paths), CHUNK):
                chunk = file_paths[i:i + CHUNK]
                placeholders = ",".join("?" * len(chunk))
                async with db.execute(
                    f"SELECT file_path FROM jobs WHERE status IN ('pending','running') "
                    f"AND file_path IN ({placeholders})",
                    chunk,
                ) as cur:
                    for r in await cur.fetchall():
                        existing.add(r["file_path"])

            # 2. Get current min pending order (for insert_next) and max order once
            async with db.execute(
                "SELECT COALESCE(MIN(queue_order), 1) FROM jobs WHERE status = 'pending'"
            ) as cur:
                row = await cur.fetchone()
                min_pending = (row[0] or 1)
            async with db.execute("SELECT COALESCE(MAX(queue_order), 0) FROM jobs") as cur:
                row = await cur.fetchone()
                max_order = (row[0] or 0)

            insert_next_counter = min_pending - 1
            append_counter = max_order

            # 3. Insert every new job in one transaction
            now = _utcnow()
            job_ids: list[int] = []
            sql = (
                "INSERT INTO jobs (file_path, job_type, status, encoder, "
                "audio_tracks_to_remove, subtitle_tracks_to_remove, created_at, "
                "queue_order, original_size, nvenc_preset, nvenc_cq, audio_codec, "
                "audio_bitrate, libx265_crf, libx265_preset, target_resolution, priority) "
                "VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            for j in jobs:
                fp = j["file_path"]
                if fp in existing:
                    job_ids.append(0)
                    continue
                if j.get("insert_next"):
                    order = insert_next_counter
                    insert_next_counter -= 1
                else:
                    append_counter += 1
                    order = append_counter

                async with db.execute(
                    sql,
                    (
                        fp,
                        j["job_type"],
                        j.get("encoder"),
                        json.dumps(j.get("audio_tracks_to_remove") or []),
                        json.dumps(j.get("subtitle_tracks_to_remove") or []),
                        now,
                        order,
                        j.get("original_size") or 0,
                        j.get("nvenc_preset"),
                        j.get("nvenc_cq"),
                        j.get("audio_codec"),
                        j.get("audio_bitrate"),
                        j.get("libx265_crf"),
                        j.get("libx265_preset"),
                        j.get("target_resolution"),
                        j.get("priority") or 0,
                    ),
                ) as cur:
                    job_ids.append(cur.lastrowid or 0)
                    # Mark this file as existing so duplicates within the same batch are skipped
                    existing.add(fp)

            await db.commit()
        finally:
            await db.close()

        # Log "queued" events for new jobs (skipped duplicates have id == 0)
        try:
            from backend.file_events import log_event, EVENT_QUEUED
            for j, jid in zip(jobs, job_ids):
                if not jid:
                    continue
                jt = j.get("job_type", "convert")
                if jt == "health_check":
                    summary = f"Queued for {j.get('encoder', 'quick')} health check"
                else:
                    summary = f"Queued for {jt}"
                await log_event(j["file_path"], EVENT_QUEUED, summary, {"job_id": jid, "job_type": jt})
        except Exception:
            pass
        return job_ids

    async def reset_stale_running(self) -> int:
        """Reset jobs stuck in 'running' status back to 'pending' (e.g. after a restart).

        Only resets jobs assigned to the local node — remote workers may still be
        actively processing their assigned jobs. Stale remote assignments are
        handled by the NodeManager's release_stale_assignments loop based on
        heartbeat timeout.
        """
        db = await self._connect()
        try:
            async with db.execute(
                "UPDATE jobs SET status = 'pending', progress = 0, fps = NULL, "
                "eta_seconds = NULL, started_at = NULL, error_log = NULL, "
                "assigned_node_id = NULL, assigned_at = NULL "
                "WHERE status = 'running' "
                "AND (assigned_node_id IS NULL OR assigned_node_id = '' OR assigned_node_id = 'local')"
            ) as cur:
                count = cur.rowcount
            await db.commit()
            if count:
                print(f"[QUEUE] Reset {count} stale running job(s) back to pending", flush=True)
            return count
        finally:
            await db.close()

    async def get_next_job(
        self,
        exclude_ids: list[int] | None = None,
        affinity: str = "any",
    ) -> Optional[dict]:
        db = await self._connect()
        try:
            # Priority DESC ensures Highest (2) before High (1) before Normal (0)
            # Within same priority, FIFO by queue_order
            # Optional affinity filter: 'cpu_only', 'nvenc_only', or 'any'
            affinity_sql = ""
            if affinity == "cpu_only":
                affinity_sql = " AND (encoder IS NULL OR encoder = '' OR LOWER(encoder) IN ('libx265','x265','cpu'))"
            elif affinity == "nvenc_only":
                affinity_sql = " AND LOWER(encoder) IN ('nvenc','hevc_nvenc')"

            if exclude_ids:
                placeholders = ",".join("?" * len(exclude_ids))
                query = (
                    f"SELECT * FROM jobs WHERE status = 'pending'{affinity_sql} "
                    f"AND id NOT IN ({placeholders}) "
                    f"ORDER BY priority DESC, queue_order ASC LIMIT 1"
                )
                async with db.execute(query, exclude_ids) as cur:
                    row = await cur.fetchone()
            else:
                async with db.execute(
                    f"SELECT * FROM jobs WHERE status = 'pending'{affinity_sql} "
                    f"ORDER BY priority DESC, queue_order ASC LIMIT 1"
                ) as cur:
                    row = await cur.fetchone()
            if row is None:
                return None
            return dict(row)
        finally:
            await db.close()

    async def get_jobs_by_status(self, status: str, limit: int = 0, offset: int = 0) -> list[dict]:
        db = await self._connect()
        try:
            order = "completed_at DESC" if status == "completed" else "priority DESC, queue_order ASC"
            sql = f"SELECT * FROM jobs WHERE status = ? ORDER BY {order}"
            params: list = [status]
            if limit > 0:
                sql += " LIMIT ? OFFSET ?"
                params += [limit, offset]
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]
        finally:
            await db.close()

    async def get_all_jobs(self, limit: int = 0, offset: int = 0) -> list[dict]:
        db = await self._connect()
        try:
            sql = "SELECT * FROM jobs ORDER BY queue_order ASC"
            params: list = []
            if limit > 0:
                sql += " LIMIT ? OFFSET ?"
                params = [limit, offset]
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]
        finally:
            await db.close()

    async def update_status(
        self,
        job_id: int,
        status: str,
        error_log: Optional[str] = None,
    ) -> None:
        for attempt in range(5):
            db = await self._connect()
            try:
                now = _utcnow()
                if status == "running":
                    await db.execute(
                        "UPDATE jobs SET status = ?, started_at = ?, error_log = ? WHERE id = ?",
                        (status, now, error_log, job_id),
                    )
                elif status in ("completed", "failed"):
                    await db.execute(
                        "UPDATE jobs SET status = ?, completed_at = ?, error_log = ? WHERE id = ?",
                        (status, now, error_log, job_id),
                    )
                else:
                    await db.execute(
                        "UPDATE jobs SET status = ?, error_log = ? WHERE id = ?",
                        (status, error_log, job_id),
                    )
                await db.commit()
                return
            except Exception as exc:
                if "locked" in str(exc).lower() and attempt < 4:
                    print(f"[QUEUE] DB locked on update_status (attempt {attempt+1}/5), retrying...", flush=True)
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise
            finally:
                await db.close()

    async def update_progress(
        self,
        job_id: int,
        progress: float,
        fps: Optional[float] = None,
        eta: Optional[int] = None,
    ) -> None:
        db = await self._connect()
        try:
            await db.execute(
                "UPDATE jobs SET progress = ?, fps = ?, eta_seconds = ? WHERE id = ?",
                (progress, fps, eta, job_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def update_original_size(self, job_id: int, original_size: int) -> None:
        db = await self._connect()
        try:
            await db.execute(
                "UPDATE jobs SET original_size = ? WHERE id = ?",
                (original_size, job_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def update_space_saved(self, job_id: int, space_saved: int) -> None:
        db = await self._connect()
        try:
            await db.execute(
                "UPDATE jobs SET space_saved = ? WHERE id = ?",
                (space_saved, job_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def update_backup_path(self, job_id: int, backup_path: str, original_file_path: str | None = None) -> None:
        db = await self._connect()
        try:
            if original_file_path:
                await db.execute(
                    "UPDATE jobs SET backup_path = ?, original_file_path = ? WHERE id = ?",
                    (backup_path, original_file_path, job_id),
                )
            else:
                await db.execute(
                    "UPDATE jobs SET backup_path = ? WHERE id = ?",
                    (backup_path, job_id),
                )
            await db.commit()
        finally:
            await db.close()

    async def update_conversion_log(self, job_id: int, command: str | None, log: str | None, stats: str | None) -> None:
        db = await self._connect()
        try:
            await db.execute(
                "UPDATE jobs SET ffmpeg_command = ?, ffmpeg_log = ?, encoding_stats = ? WHERE id = ?",
                (command, log, stats, job_id),
            )
            await db.commit()
        finally:
            await db.close()

    async def reorder_jobs(self, job_ids: list[int]) -> None:
        """Update queue_order for jobs based on the provided ordering."""
        db = await self._connect()
        try:
            for order, job_id in enumerate(job_ids, start=1):
                await db.execute(
                    "UPDATE jobs SET queue_order = ? WHERE id = ? AND status = 'pending'",
                    (order, job_id),
                )
            await db.commit()
        finally:
            await db.close()

    async def remove_job(self, job_id: int) -> None:
        db = await self._connect()
        try:
            await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            await db.commit()
        finally:
            await db.close()

    async def clear_completed(self) -> None:
        db = await self._connect()
        try:
            await db.execute(
                "DELETE FROM jobs WHERE status IN ('completed', 'failed', 'cancelled')"
            )
            await db.commit()
        finally:
            await db.close()

    async def clear_pending(self) -> None:
        db = await self._connect()
        try:
            await db.execute("DELETE FROM jobs WHERE status = 'pending'")
            await db.commit()
        finally:
            await db.close()

    async def get_stats(self) -> dict:
        db = await self._connect()
        try:
            async with db.execute(
                """SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) as running,
                    SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed,
                    COALESCE(SUM(CASE WHEN status='completed' AND space_saved > 0 THEN space_saved ELSE 0 END), 0) as total_space_saved,
                    COALESCE(SUM(CASE WHEN status='completed' AND original_size > 0 THEN original_size ELSE 0 END), 0) as total_original_size
                FROM jobs"""
            ) as cur:
                row = await cur.fetchone()
            return {
                "total_jobs": row["total"],
                "pending": row["pending"],
                "running": row["running"],
                "completed": row["completed"],
                "failed": row["failed"],
                "total_space_saved": row["total_space_saved"],
                "total_original_size": row["total_original_size"],
            }
        finally:
            await db.close()


_last_backup_cleanup = 0.0  # Throttle: at most once per hour


async def _cleanup_expired_backups():
    """Delete backup files older than backup_original_days. Runs at most once per hour."""
    import os
    import time

    global _last_backup_cleanup
    now = time.time()
    if now - _last_backup_cleanup < 3600:
        return  # Already ran recently
    _last_backup_cleanup = now

    from backend.database import DB_PATH
    db = await aiosqlite.connect(DB_PATH)
    try:
        db.row_factory = aiosqlite.Row
        settings = {}
        async with db.execute(
            "SELECT key, value FROM settings WHERE key IN ('backup_original_days', 'backup_folder')"
        ) as cur:
            for row in await cur.fetchall():
                settings[row["key"]] = row["value"]

        days = int(settings.get("backup_original_days", "0"))
        if days <= 0:
            return  # Backup disabled or keep forever

        cutoff = now - (days * 86400)
        custom_folder = settings.get("backup_folder", "")

        # Get media dirs
        async with db.execute("SELECT path FROM media_dirs") as cur:
            media_dirs = [r["path"] for r in await cur.fetchall()]
    finally:
        await db.close()

    deleted = 0

    def cleanup_dir(backup_dir: str):
        nonlocal deleted
        if not os.path.isdir(backup_dir):
            return
        for entry in os.scandir(backup_dir):
            if entry.is_file():
                try:
                    if entry.stat().st_mtime < cutoff:
                        os.unlink(entry.path)
                        deleted += 1
                except OSError:
                    pass
        # Remove dir if empty
        try:
            if not any(os.scandir(backup_dir)):
                os.rmdir(backup_dir)
        except OSError:
            pass

    # Clean custom backup folder
    if custom_folder and os.path.isdir(custom_folder):
        for entry in os.scandir(custom_folder):
            if entry.is_dir():
                cleanup_dir(entry.path)

    # Clean both .shrinkerr_backup (new) and .squeezarr_backup (legacy) in
    # media dirs so expired backups get cleaned up regardless of which name
    # created them.
    _BACKUP_DIRNAMES = {".shrinkerr_backup", ".squeezarr_backup"}
    for media_dir in media_dirs:
        for root, dirs, _files in os.walk(media_dir):
            for backup_name in _BACKUP_DIRNAMES & set(dirs):
                cleanup_dir(os.path.join(root, backup_name))
            dirs[:] = [d for d in dirs if d not in _BACKUP_DIRNAMES]

    if deleted > 0:
        print(f"[BACKUP] Cleaned up {deleted} expired backup(s) (older than {days} days)", flush=True)


class QueueWorker:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.queue = JobQueue(db_path)
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._paused = False
        # Parallel job tracking: keyed by job_id
        self._active_procs: dict[int, asyncio.subprocess.Process] = {}
        self._active_tasks: dict[int, asyncio.Task] = {}
        self._cancel_flags: set[int] = set()

    async def _db(self) -> aiosqlite.Connection:
        """Open a DB connection with WAL mode and busy timeout for parallel safety."""
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=30000")
        return db

    @property
    def _current_job_id(self) -> Optional[int]:
        """Compat: return first active job id (for API that expects single job)."""
        return next(iter(self._active_procs), None)

    def start(self) -> None:
        print(f"[WORKER] start() called: _running={self._running}, _task={self._task}, _task.done={self._task.done() if self._task else 'N/A'}", flush=True)
        self._paused = False
        if self._running and self._task and not self._task.done():
            print("[WORKER] Already running, unpaused", flush=True)
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        self._task.add_done_callback(self._task_done)
        print(f"[WORKER] Created new task: {self._task}", flush=True)

    @staticmethod
    def _task_done(task: asyncio.Task) -> None:
        if task.cancelled():
            print("[WORKER] Task was cancelled", flush=True)
        elif task.exception():
            print(f"[WORKER] Task crashed: {task.exception()}", flush=True)
        else:
            print("[WORKER] Task finished normally", flush=True)

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    async def cancel_current(self, job_id: Optional[int] = None) -> Optional[int]:
        """Cancel a running job by ID. If no ID given, cancel the first active job."""
        if job_id is None:
            job_id = next(iter(self._active_procs), None)
        if job_id is None:
            return None
        self._cancel_flags.add(job_id)
        proc = self._active_procs.get(job_id)
        if proc and proc.returncode is None:
            try:
                proc.kill()
                print(f"[WORKER] Killed ffmpeg for job {job_id}", flush=True)
            except ProcessLookupError:
                pass
        return job_id

    async def _get_parallel_limit(self) -> int:
        """Read parallel_jobs setting from DB."""
        try:
            db = await self._db()
            try:
                async with db.execute("SELECT value FROM settings WHERE key = 'parallel_jobs'") as cur:
                    row = await cur.fetchone()
                    return int(row[0]) if row else 8
            finally:
                await db.close()
        except Exception:
            return 1

    async def _is_quiet_hours(self) -> bool:
        """Check if we're currently in quiet hours."""
        try:
            db = await self._db()
            try:
                settings = {}
                async with db.execute(
                    "SELECT key, value FROM settings WHERE key IN ('quiet_hours_enabled', 'quiet_hours_start', 'quiet_hours_end')"
                ) as cur:
                    for row in await cur.fetchall():
                        settings[row[0]] = row[1]
            finally:
                await db.close()

            if settings.get("quiet_hours_enabled", "false").lower() != "true":
                return False

            from datetime import datetime
            hour = datetime.now().hour
            start = int(settings.get("quiet_hours_start", "22"))
            end = int(settings.get("quiet_hours_end", "8"))

            if start > end:  # Overnight: e.g., 22-8
                return hour >= start or hour < end
            else:  # Same day: e.g., 1-6
                return start <= hour < end
        except Exception:
            return False

    async def _get_quiet_hours_parallel(self) -> int:
        try:
            db = await self._db()
            try:
                async with db.execute("SELECT value FROM settings WHERE key = 'quiet_hours_parallel'") as cur:
                    row = await cur.fetchone()
                    return int(row[0]) if row else 1
            finally:
                await db.close()
        except Exception:
            return 1

    async def _should_pause_for_plex(self) -> bool:
        """Check if encoding should pause due to active Plex streams."""
        try:
            db = await self._db()
            try:
                settings = {}
                async with db.execute(
                    "SELECT key, value FROM settings WHERE key IN ('plex_pause_on_stream', 'plex_pause_stream_threshold', 'plex_pause_transcode_only')"
                ) as cur:
                    for row in await cur.fetchall():
                        settings[row[0]] = row[1]
            finally:
                await db.close()

            if settings.get("plex_pause_on_stream", "false").lower() != "true":
                return False

            threshold = int(settings.get("plex_pause_stream_threshold", "1"))
            transcode_only = settings.get("plex_pause_transcode_only", "true").lower() == "true"

            from backend.plex import get_active_streams
            streams = await get_active_streams()

            if transcode_only:
                return streams["transcoding"] >= threshold
            else:
                return streams["total"] >= threshold
        except Exception:
            return False

    async def _should_pause_for_jellyfin(self) -> bool:
        """Check if encoding should pause due to active Jellyfin streams."""
        try:
            db = await self._db()
            try:
                settings = {}
                async with db.execute(
                    "SELECT key, value FROM settings WHERE key IN ('jellyfin_pause_on_stream', 'jellyfin_pause_stream_threshold', 'jellyfin_pause_transcode_only')"
                ) as cur:
                    for row in await cur.fetchall():
                        settings[row[0]] = row[1]
            finally:
                await db.close()

            if settings.get("jellyfin_pause_on_stream", "false").lower() != "true":
                return False

            threshold = int(settings.get("jellyfin_pause_stream_threshold", "1"))
            transcode_only = settings.get("jellyfin_pause_transcode_only", "true").lower() == "true"

            from backend.jellyfin import get_active_streams
            streams = await get_active_streams()

            if transcode_only:
                return streams["transcoding"] >= threshold
            else:
                return streams["total"] >= threshold
        except Exception:
            return False

    async def _should_use_nice(self) -> bool:
        """Check if quiet hours nice (low priority) is enabled and active."""
        if not await self._is_quiet_hours():
            return False
        try:
            db = await self._db()
            try:
                async with db.execute("SELECT value FROM settings WHERE key = 'quiet_hours_nice'") as cur:
                    row = await cur.fetchone()
                    return row and row[0].lower() == "true"
            finally:
                await db.close()
        except Exception:
            return False

    async def _get_local_node_settings(self) -> dict:
        """Read per-node settings for the 'local' node from worker_nodes."""
        try:
            db = await self._db()
            try:
                async with db.execute(
                    "SELECT paused, max_jobs, job_affinity, translate_encoder, "
                    "schedule_enabled, schedule_hours FROM worker_nodes WHERE id = 'local'"
                ) as cur:
                    row = await cur.fetchone()
                if not row:
                    return {}
                try:
                    schedule_hours = json.loads(row["schedule_hours"] or "[]")
                except Exception:
                    schedule_hours = []
                return {
                    "paused": bool(row["paused"]),
                    "max_jobs": row["max_jobs"] or 1,
                    "job_affinity": row["job_affinity"] or "any",
                    "translate_encoder": bool(row["translate_encoder"]),
                    "schedule_enabled": bool(row["schedule_enabled"]),
                    "schedule_hours": schedule_hours,
                }
            finally:
                await db.close()
        except Exception:
            return {}

    @staticmethod
    def _is_local_within_schedule(settings: dict) -> bool:
        """Return True if the local node's per-node schedule allows running now."""
        if not settings.get("schedule_enabled"):
            return True  # Schedule disabled = always allowed
        hours = settings.get("schedule_hours") or []
        if not hours:
            return False  # Schedule enabled but no hours selected = never
        from datetime import datetime
        return datetime.now().hour in hours

    async def _run_loop(self) -> None:
        print("[WORKER] Loop started, running=%s paused=%s" % (self._running, self._paused), flush=True)
        self._outside_hours_logged = False
        _first_iter = True
        while self._running:
            if _first_iter:
                print("[WORKER] Entering first loop iteration", flush=True)
                _first_iter = False
            if self._paused:
                await asyncio.sleep(1)
                continue

            # Per-node settings for 'local' — check pause, schedule, parallel_jobs, affinity
            local_settings = await self._get_local_node_settings()
            if local_settings.get("paused"):
                await asyncio.sleep(2)
                continue
            if not self._is_local_within_schedule(local_settings):
                await asyncio.sleep(30)
                continue

            # Check run hours restriction
            try:
                from backend.routes.schedule import is_within_run_hours
                if not await is_within_run_hours():
                    if not self._outside_hours_logged:
                        print("[WORKER] Outside configured run hours, waiting...", flush=True)
                        self._outside_hours_logged = True
                    await asyncio.sleep(30)
                    continue
                if self._outside_hours_logged:
                    print("[WORKER] Back within run hours, resuming", flush=True)
                    self._outside_hours_logged = False
            except Exception:
                pass

            # Check if Plex streams require pausing (only when no jobs are running to avoid interrupting)
            if len(self._active_tasks) == 0:
                try:
                    if await self._should_pause_for_plex():
                        if not getattr(self, '_plex_pause_logged', False):
                            print("[WORKER] Pausing — active Plex stream(s) detected", flush=True)
                            self._plex_pause_logged = True
                        await asyncio.sleep(15)
                        continue
                    if await self._should_pause_for_jellyfin():
                        if not getattr(self, '_jellyfin_pause_logged', False):
                            print("[WORKER] Pausing — active Jellyfin stream(s) detected", flush=True)
                            self._jellyfin_pause_logged = True
                        await asyncio.sleep(15)
                        continue
                    elif getattr(self, '_plex_pause_logged', False):
                        print("[WORKER] Resuming — Plex streams ended", flush=True)
                        self._plex_pause_logged = False
                except Exception:
                    pass

            # Clean up completed tasks
            done_ids = [jid for jid, t in self._active_tasks.items() if t.done()]
            for jid in done_ids:
                self._active_tasks.pop(jid, None)
                self._active_procs.pop(jid, None)
                self._cancel_flags.discard(jid)

            # Check how many slots are available (reduced during quiet hours)
            try:
                # Per-node max_jobs takes precedence over the global parallel_jobs setting
                node_max = local_settings.get("max_jobs")
                max_parallel = node_max if node_max else await self._get_parallel_limit()
                if await self._is_quiet_hours():
                    quiet_parallel = await self._get_quiet_hours_parallel()
                    max_parallel = min(max_parallel, quiet_parallel)
            except Exception as exc:
                print(f"[WORKER] Failed to read parallel_jobs setting: {exc}", flush=True)
                max_parallel = 1
            active_count = len(self._active_tasks)

            if active_count >= max_parallel:
                await asyncio.sleep(0.5)
                continue

            # Try to pick the next job (excluding already-running ones)
            try:
                running_ids = list(self._active_tasks.keys())
                job = await self.queue.get_next_job(
                    exclude_ids=running_ids,
                    affinity=local_settings.get("job_affinity", "any"),
                )
            except Exception as exc:
                print(f"[WORKER] Failed to get next job (DB may be busy): {exc}", flush=True)
                await asyncio.sleep(2)
                continue

            if job is None:
                await asyncio.sleep(1)
                continue

            # Spawn a worker task for this job
            job_id = job["id"]
            print(f"[WORKER] Processing job {job_id}: {job['file_path']} (slot {active_count + 1}/{max_parallel})", flush=True)
            task = asyncio.create_task(self._worker_task(job))
            self._active_tasks[job_id] = task

            # Small stagger between spawns so workers don't all probe/start at the same instant
            await asyncio.sleep(0.5)

    async def _worker_task(self, job: dict) -> None:
        """Process a single job. Runs as an independent async task."""
        job_id = job["id"]
        try:
            await self._process_job(job)
            if job_id in self._cancel_flags:
                print(f"[WORKER] Job {job_id} was cancelled", flush=True)
            else:
                print(f"[WORKER] Job {job_id} completed", flush=True)
        except Exception as exc:
            print(f"[WORKER] Job {job_id} FAILED: {exc}", flush=True)
            import traceback; traceback.print_exc()
            try:
                await self.queue.update_status(job_id, "failed", error_log=str(exc))
            except Exception:
                pass
            try:
                from backend.file_events import log_event, EVENT_FAILED
                await log_event(job.get("file_path", ""), EVENT_FAILED, f"Failed: {str(exc)[:120]}", {"job_id": job_id})
            except Exception:
                pass
        finally:
            # Record this job against the built-in "local" node so the
            # Nodes page shows accurate Completed / Saved stats for the
            # server itself. Remote workers do this via the /report-complete
            # endpoint; the local worker doesn't go through that path, so
            # without this hook the Local card was stuck at 0 completed.
            try:
                from backend.main import app as _app
                nm = getattr(_app.state, "node_manager", None)
                if nm is not None:
                    db = await self.queue._connect()
                    try:
                        async with db.execute(
                            "SELECT status, space_saved FROM jobs WHERE id = ?",
                            (job_id,),
                        ) as cur:
                            r = await cur.fetchone()
                    finally:
                        await db.close()
                    if r:
                        # r is a Row object (dict-like)
                        final_status = r["status"]
                        saved = int(r["space_saved"] or 0)
                        success = (final_status == "completed")
                        # Skip health_check jobs — they don't represent work
                        # in the useful sense (they don't transcode anything)
                        # and inflating jobs_completed on the local card with
                        # them would be misleading.
                        if job.get("job_type") != "health_check":
                            await nm.complete_job_on_node(
                                "local", job_id,
                                success=success, space_saved=saved,
                            )
            except Exception as exc:
                print(f"[WORKER] Failed to update local node stats: {exc}", flush=True)

            self._active_procs.pop(job_id, None)
            self._active_tasks.pop(job_id, None)
            self._cancel_flags.discard(job_id)

            # Release the WebSocket progress-throttle entry on every exit
            # path (completion, cancel, requeue, exception). Without this,
            # jobs that exit without calling `send_job_complete` (e.g. the
            # node-pause requeue branch and unhandled-exception branch
            # above) leak entries in ws_manager._last_job_progress_emit
            # and retries of the same job_id get their first progress
            # message swallowed for up to 500ms.
            try:
                from backend.websocket import ws_manager
                ws_manager.release_job_throttle(job_id)
            except Exception:
                pass

    async def _run_health_check_job(self, job_id: int, file_path: str, file_name: str, job: dict, stats: dict) -> None:
        """Run a health check (quick or thorough) and persist results to scan_results."""
        import os
        from backend.health_check import run_check
        from backend.websocket import ws_manager
        from backend.database import DB_PATH

        # Mode is stashed in the 'encoder' column (reuses existing schema)
        mode = (job.get("encoder") or "quick").lower()
        if mode not in ("quick", "thorough"):
            mode = "quick"

        if not os.path.exists(file_path):
            await self.queue.update_status(job_id, "failed", error_log="File not found")
            return

        await ws_manager.send_job_progress(
            job_id=job_id,
            file_name=file_name,
            progress=0.0,
            fps=None,
            eta=None,
            step=f"health-check ({mode})",
            jobs_completed=stats["completed"],
            jobs_total=stats["total_jobs"],
            total_saved=stats["total_space_saved"],
        )

        try:
            result = await run_check(file_path, mode=mode)
        except Exception as exc:
            await self.queue.update_status(job_id, "failed", error_log=f"Health check failed: {exc}")
            return

        status = result.get("status", "healthy")
        errors = result.get("errors", [])
        errs_json = json.dumps(errors) if errors else None
        duration_s = result.get("duration_seconds")

        # Persist results to scan_results AND mirror onto the job row
        import aiosqlite
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        db = await aiosqlite.connect(DB_PATH)
        try:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=30000")
            await db.execute(
                "UPDATE scan_results SET health_status = ?, health_errors_json = ?, "
                "health_checked_at = ?, health_check_type = ? WHERE file_path = ?",
                (status, errs_json, now, mode, file_path),
            )
            await db.execute(
                "UPDATE jobs SET health_status = ?, health_errors_json = ?, "
                "health_check_type = ?, health_check_seconds = ? WHERE id = ?",
                (status, errs_json, mode, duration_s, job_id),
            )
            await db.commit()
        finally:
            await db.close()

        print(
            f"[HEALTH] {mode} check complete for {file_name}: {status} "
            f"({result.get('duration_seconds', 0)}s)",
            flush=True,
        )

        # Mark the job completed with the status recorded as error_log for visibility
        error_log = None
        if status == "corrupt":
            error_log = "Corrupt: " + ("; ".join(errors[:3]) if errors else "unknown error")
        await self.queue.update_status(job_id, "completed", error_log=error_log)

        # File-events log
        try:
            from backend.file_events import log_event, EVENT_HEALTH_CHECK
            await log_event(
                file_path,
                EVENT_HEALTH_CHECK,
                f"Health check: {status} ({mode})",
                {
                    "status": status,
                    "check_type": mode,
                    "duration_seconds": duration_s,
                    "errors": errors[:5] if errors else None,
                    "job_id": job_id,
                },
            )
        except Exception:
            pass

        # Broadcast final progress
        stats_final = await self.queue.get_stats()
        await ws_manager.send_job_progress(
            job_id=job_id,
            file_name=file_name,
            progress=100.0,
            fps=None,
            eta=0,
            step=f"health-check ({status})",
            jobs_completed=stats_final["completed"],
            jobs_total=stats_final["total_jobs"],
            total_saved=stats_final["total_space_saved"],
        )

    async def _process_job(self, job: dict) -> None:
        from backend.scanner import probe_file
        from backend.converter import convert_file
        from backend.audio import remux_audio
        from backend.websocket import ws_manager

        job_id = job["id"]
        file_path = job["file_path"]
        job_type = job["job_type"]
        encoder = job.get("encoder") or "nvenc"

        audio_tracks_to_remove_raw = job.get("audio_tracks_to_remove") or "[]"
        if isinstance(audio_tracks_to_remove_raw, str):
            try:
                audio_tracks_to_remove = json.loads(audio_tracks_to_remove_raw)
            except (json.JSONDecodeError, ValueError):
                audio_tracks_to_remove = []
        else:
            audio_tracks_to_remove = audio_tracks_to_remove_raw or []

        subtitle_tracks_to_remove_raw = job.get("subtitle_tracks_to_remove") or "[]"
        if isinstance(subtitle_tracks_to_remove_raw, str):
            try:
                subtitle_tracks_to_remove = json.loads(subtitle_tracks_to_remove_raw)
            except (json.JSONDecodeError, ValueError):
                subtitle_tracks_to_remove = []
        else:
            subtitle_tracks_to_remove = subtitle_tracks_to_remove_raw or []

        await self.queue.update_status(job_id, "running")
        # Tag job as assigned to the local node
        db = await self.queue._connect()
        try:
            await db.execute(
                "UPDATE jobs SET assigned_node_id = 'local' WHERE id = ?", (job_id,),
            )
            await db.commit()
        finally:
            await db.close()
        print(f"[WORKER] Job {job_id} status set to running (local)", flush=True)

        import os
        file_name = os.path.basename(file_path)

        # Send immediate "starting" progress so the frontend shows the card right away
        stats = await self.queue.get_stats()
        await ws_manager.send_job_progress(
            job_id=job_id,
            file_name=file_name,
            progress=0.0,
            fps=None,
            eta=None,
            step="starting",
            jobs_completed=stats["completed"],
            jobs_total=stats["total_jobs"],
            total_saved=stats["total_space_saved"],
        )

        # Health check jobs bypass the convert/audio pipeline entirely
        if job_type == "health_check":
            await self._run_health_check_job(job_id, file_path, file_name, job, stats)
            return

        # Probe for duration
        probe = await probe_file(file_path)
        if probe is None:
            print(f"[WORKER] Job {job_id}: FAILED to probe {file_path}", flush=True)
            await self.queue.update_status(job_id, "failed", error_log="Failed to probe file")
            return
        print(f"[WORKER] Job {job_id}: probed OK, duration={probe.get('duration', 0):.1f}s, codec={probe.get('video_codec', '?')}", flush=True)

        duration = probe.get("duration", 0.0)
        file_size = probe.get("file_size", 0)
        if file_size > 0:
            await self.queue.update_original_size(job_id, file_size)

        jobs_total = stats["total_jobs"]
        jobs_completed = stats["completed"]
        total_saved = stats["total_space_saved"]

        space_saved = 0
        current_file_path = file_path

        if job_type in ("convert", "combined"):
            # Decouple WS-broadcast frequency from DB-write frequency. ffmpeg
            # emits ~2 progress lines/sec/job and we want the live UI to feel
            # smooth, but every DB write needs the WAL write lock — under
            # contention from a long-running periodic transaction those
            # writes can stall ~60s at a time, blocking the entire progress
            # callback (and by extension ffmpeg's stderr buffer). Symptom:
            # progress bar pinned at one number for a minute, then jumping
            # to a much higher value. v0.3.36+.
            #
            # New cadence:
            #   - WS broadcast: every progress line (server-side ws_manager
            #     already throttles to 500ms per job).
            #   - DB write: at most once every _PROGRESS_DB_WRITE_INTERVAL
            #     seconds per job, plus a guaranteed write when progress
            #     reaches terminal (>= 99.99) so the persisted final value
            #     isn't off by a hair when the job lands in history.
            _last_db_write = [0.0]  # cell, since closures can't rebind a number
            async def progress_cb(progress: float, fps=None, eta_seconds=None, step=None):
                now = time.monotonic()
                is_terminal = progress >= 99.99
                if is_terminal or (now - _last_db_write[0]) >= _PROGRESS_DB_WRITE_INTERVAL:
                    await self.queue.update_progress(job_id, progress, fps=fps, eta=eta_seconds)
                    _last_db_write[0] = now
                await ws_manager.send_job_progress(
                    job_id=job_id,
                    file_name=file_name,
                    progress=progress,
                    fps=fps,
                    eta=eta_seconds,
                    step=step or "converting",
                    jobs_completed=jobs_completed,
                    jobs_total=jobs_total,
                    total_saved=total_saved,
                )

            def on_proc(proc):
                self._active_procs[job_id] = proc

            # Pass per-job encoding overrides (None means use global settings).
            # For combined jobs, pass audio/subtitle removal lists so they're applied
            # in the same ffmpeg pass — no second remux with mismatched stream indices.
            use_nice = await self._should_use_nice()
            result = await convert_file(
                input_path=current_file_path,
                encoder=encoder,
                duration=duration,
                progress_callback=progress_cb,
                proc_callback=on_proc,
                override_preset=job.get("nvenc_preset"),
                override_cq=job.get("nvenc_cq"),
                override_audio_codec=job.get("audio_codec"),
                override_audio_bitrate=job.get("audio_bitrate"),
                override_crf=job.get("libx265_crf"),
                override_libx265_preset=job.get("libx265_preset"),
                override_target_resolution=job.get("target_resolution"),
                nice=use_nice,
                audio_tracks_to_remove=audio_tracks_to_remove if job_type == "combined" else None,
                subtitle_tracks_to_remove=subtitle_tracks_to_remove if job_type == "combined" else None,
            )
            if not result["success"]:
                if job_id in self._cancel_flags:
                    # Check if this cancel was triggered by a node pause — if so,
                    # return the job to pending for another worker to pick up.
                    requeue = False
                    try:
                        from backend.routes.jobs import app_state_ref  # pragma: no cover
                    except Exception:
                        app_state_ref = None
                    try:
                        # NodeManager is the source of truth for requeue flags
                        from backend.main import app as _app
                        nm = getattr(_app.state, "node_manager", None)
                        if nm is not None and nm.should_requeue(job_id):
                            requeue = True
                            nm.clear_cancel(job_id)
                    except Exception:
                        pass

                    if requeue:
                        db = await self.queue._connect()
                        try:
                            await db.execute(
                                "UPDATE jobs SET status = 'pending', progress = 0, fps = NULL, "
                                "eta_seconds = NULL, started_at = NULL, error_log = NULL, "
                                "assigned_node_id = NULL, assigned_at = NULL, cancel_requested = 0 "
                                "WHERE id = ?",
                                (job_id,),
                            )
                            await db.commit()
                        finally:
                            await db.close()
                        print(f"[WORKER] Job {job_id} returned to pending (node paused)", flush=True)
                    else:
                        await self.queue.update_status(job_id, "cancelled", error_log="Cancelled by user")
                        await ws_manager.send_job_complete(job_id, "cancelled", 0, "Cancelled by user")
                else:
                    await self.queue.update_status(job_id, "failed", error_log=result["error"])
                    await ws_manager.send_job_complete(job_id, "failed", 0, result["error"])
                    try:
                        from backend.notifications import send_notification
                        await send_notification("job_failed", "Job Failed",
                            f"{file_name} failed during conversion",
                            {"Error": result["error"][:200]})
                    except Exception:
                        pass
                return
            space_saved += result.get("space_saved", 0)
            current_file_path = result["output_path"]

            # IMPORTANT: as soon as the file has been renamed on disk, update the
            # job + scan_results fully: new path, new size, new codec, new track
            # info. Otherwise the UI will show the old codec badge / size / tracks
            # until a manual rescan, and any failed post-conversion step would
            # leave the row in an inconsistent half-updated state.
            if current_file_path != file_path:
                import os as _os
                import json as _json
                try:
                    new_size = _os.path.getsize(current_file_path)
                except OSError:
                    new_size = None

                # Re-probe the converted file to get fresh audio / subtitle track info.
                # Stream indices, track counts, and codecs have changed. We also
                # re-classify tracks (keep/locked) so the UI shows correct checkboxes
                # and future "add to queue" knows which tracks to remove.
                new_audio_json = None
                new_sub_json = None
                new_has_removable_audio = 0
                new_has_removable_subs = 0
                new_lossless = 0
                try:
                    from backend.scanner import (
                        classify_audio_tracks, classify_subtitle_tracks,
                        detect_native_language, _is_cleanup_enabled,
                        languages_match,
                    )
                    from backend.converter import is_lossless_audio
                    fresh = await probe_file(current_file_path)
                    if fresh:
                        raw_audio = fresh.get("audio_tracks") or []
                        raw_subs = fresh.get("subtitle_tracks") or []
                        native_lang = detect_native_language(raw_audio)
                        # Look up API-sourced native language from scan_results
                        try:
                            db_nl = await self._db()
                            try:
                                async with db_nl.execute(
                                    "SELECT native_language FROM scan_results WHERE file_path = ?",
                                    (file_path,),
                                ) as cur:
                                    nl_row = await cur.fetchone()
                                if nl_row and nl_row["native_language"]:
                                    native_lang = nl_row["native_language"]
                            finally:
                                await db_nl.close()
                        except Exception:
                            pass
                        classified_audio = classify_audio_tracks(
                            raw_audio, native_lang, fresh.get("duration", 0),
                        )
                        classified_subs = classify_subtitle_tracks(raw_subs, native_lang)
                        new_audio_json = _json.dumps([t.model_dump() for t in classified_audio])
                        new_sub_json = _json.dumps([t.model_dump() for t in classified_subs])
                        # Flag includes both removable tracks AND reorder-needed (if enabled)
                        needs_reorder = False
                        if _is_cleanup_enabled("reorder_native_audio") and len(classified_audio) > 1 and native_lang and native_lang.lower() != "und":
                            first_lang = (classified_audio[0].language or "").lower()
                            needs_reorder = not languages_match(first_lang, native_lang.lower())
                        new_has_removable_audio = 1 if (any(not t.keep for t in classified_audio) or needs_reorder) else 0
                        new_has_removable_subs = 1 if any(not t.keep for t in classified_subs) else 0
                        new_lossless = 1 if any(
                            is_lossless_audio(t.codec, getattr(t, "profile", ""))
                            for t in classified_audio
                        ) else 0
                except Exception as exc:
                    print(f"[WORKER] Re-probe after conversion failed (non-fatal): {exc}", flush=True)

                try:
                    db_path = await self.queue._connect()
                    try:
                        await db_path.execute(
                            "UPDATE jobs SET file_path = ? WHERE id = ?",
                            (current_file_path, job_id),
                        )
                        # Build the scan_results update based on what we have
                        update_cols = [
                            "file_path = ?",
                            "video_codec = 'hevc'",
                            "needs_conversion = 0",
                            "converted = 1",
                        ]
                        update_params: list = [current_file_path]
                        if new_size is not None:
                            update_cols.append("file_size = ?")
                            update_params.append(new_size)
                        if new_audio_json is not None:
                            update_cols.append("audio_tracks_json = ?")
                            update_params.append(new_audio_json)
                            update_cols.append("has_removable_tracks_flag = ?")
                            update_params.append(new_has_removable_audio)
                            update_cols.append("has_lossless_audio_flag = ?")
                            update_params.append(new_lossless)
                        if new_sub_json is not None:
                            update_cols.append("subtitle_tracks_json = ?")
                            update_params.append(new_sub_json)
                            update_cols.append("has_removable_subs_flag = ?")
                            update_params.append(new_has_removable_subs)
                        update_params.append(file_path)  # WHERE
                        await db_path.execute(
                            f"UPDATE scan_results SET {', '.join(update_cols)} WHERE file_path = ?",
                            update_params,
                        )
                        await db_path.commit()
                    finally:
                        await db_path.close()
                except Exception as exc:
                    print(f"[WORKER] Early scan_results update failed (non-fatal): {exc}", flush=True)

            # Store backup path + conversion log for undo/history
            if result.get("backup_path"):
                try:
                    await self.queue.update_backup_path(job_id, result["backup_path"], file_path)
                except Exception as exc:
                    print(f"[WORKER] Failed to store backup path: {exc}", flush=True)
            if result.get("ffmpeg_command"):
                try:
                    import json as _json
                    stats_json = _json.dumps(result["encoding_stats"]) if result.get("encoding_stats") else None
                    await self.queue.update_conversion_log(job_id, result["ffmpeg_command"], result.get("ffmpeg_log"), stats_json)
                except Exception as exc:
                    print(f"[WORKER] Failed to store conversion log: {exc}", flush=True)

            # Store VMAF score from converter. By this point the scan_results
            # row has already been renamed (above) from file_path to
            # current_file_path when conversion changed the filename (e.g.
            # x264 → x265 rename). So the UPDATE and the file-event MUST
            # both use current_file_path, not file_path — otherwise they
            # land on a row/path that doesn't exist anymore, the vmaf_score
            # gets silently dropped, and the History tab shows no VMAF entry.
            vmaf_score = result.get("vmaf_score")
            vmaf_uncertain = bool(result.get("vmaf_uncertain"))
            if vmaf_score is not None:
                vmaf_path = current_file_path  # post-rename path is authoritative
                try:
                    db = await self._db()
                    try:
                        await db.execute(
                            "UPDATE jobs SET vmaf_score = ?, vmaf_uncertain = ? WHERE id = ?",
                            (vmaf_score, 1 if vmaf_uncertain else 0, job_id),
                        )
                        cur = await db.execute(
                            "UPDATE scan_results SET vmaf_score = ?, vmaf_uncertain = ? WHERE file_path = ?",
                            (vmaf_score, 1 if vmaf_uncertain else 0, vmaf_path),
                        )
                        rows_updated = cur.rowcount
                        await db.commit()
                        suspect = " (measurement-suspect)" if vmaf_uncertain else ""
                        if rows_updated == 0:
                            print(f"[WORKER] VMAF score {vmaf_score}{suspect} NOT saved to scan_results — path not found: {vmaf_path}", flush=True)
                        else:
                            print(f"[WORKER] VMAF score {vmaf_score}{suspect} saved to scan_results for {file_name}", flush=True)
                    finally:
                        await db.close()
                except Exception as exc:
                    print(f"[WORKER] Failed to store VMAF score: {exc}", flush=True)
                # File-events log — use the same post-rename path so the
                # History tab query (by current file_path) finds this event.
                try:
                    # Canonical 3-tier table (v0.3.32+) — mirrored in
                    # backend/test_encode.py, backend/routes/stats.py, and
                    # frontend/src/utils/vmaf.ts. Edit all four together.
                    if vmaf_score >= 93: tier = "Excellent"
                    elif vmaf_score >= 87: tier = "Good"
                    else: tier = "Poor"
                    from backend.file_events import log_event, EVENT_VMAF
                    summary = f"VMAF: {vmaf_score} ({tier})"
                    if vmaf_uncertain:
                        summary += " ⚠ measurement-suspect"
                    await log_event(
                        vmaf_path,
                        EVENT_VMAF,
                        summary,
                        {
                            "vmaf_score": vmaf_score,
                            "tier": tier,
                            "job_id": job_id,
                            "vmaf_uncertain": vmaf_uncertain,
                        },
                    )
                except Exception:
                    pass

            # Surface VMAF *failures* (not just scores) in Activity. Previously a
            # silent VMAF failure left no trace anywhere the user could see —
            # docker logs if they checked, nothing in the UI.
            vmaf_err = result.get("vmaf_error")
            if vmaf_score is None and vmaf_err:
                try:
                    from backend.file_events import log_event, EVENT_VMAF
                    await log_event(
                        current_file_path,
                        EVENT_VMAF,
                        f"VMAF failed — {vmaf_err[:200]}",
                        {"vmaf_error": vmaf_err, "job_id": job_id},
                    )
                except Exception:
                    pass

            if result.get("skipped_larger"):
                # Mark file as ignored so future scans tag it
                try:
                    db = await self._db()
                    try:
                        await db.execute(
                            "INSERT OR IGNORE INTO ignored_files (file_path, reason, ignored_at) VALUES (?, ?, ?)",
                            (file_path, "conversion_larger", _utcnow()),
                        )
                        await db.commit()
                        print(f"[WORKER] Marked as ignored (larger after conversion): {file_path}", flush=True)
                    finally:
                        await db.close()
                except Exception as exc:
                    print(f"[WORKER] Failed to mark ignored: {exc}", flush=True)

            if result.get("vmaf_rejected"):
                # Encode completed cleanly but didn't meet the VMAF threshold.
                # Store the rejection reason on the job so the UI can surface
                # it, and emit a file-event so the Activity page has a record
                # of why this file wasn't converted. We deliberately do NOT
                # auto-ignore the file the way skipped_larger does — a VMAF
                # miss usually means the user's CQ is too aggressive for this
                # content and they may want to retry with different settings.
                reason = result.get("vmaf_reject_reason") or "VMAF below threshold"
                try:
                    db = await self._db()
                    try:
                        await db.execute(
                            "UPDATE jobs SET error_log = ? WHERE id = ?",
                            (reason, job_id),
                        )
                        await db.commit()
                    finally:
                        await db.close()
                except Exception as exc:
                    print(f"[WORKER] Failed to store VMAF rejection reason: {exc}", flush=True)
                try:
                    from backend.file_events import log_event, EVENT_VMAF
                    await log_event(
                        file_path,
                        EVENT_VMAF,
                        f"Rejected: {reason}",
                        {
                            "vmaf_score": result.get("vmaf_score"),
                            "vmaf_min_score": result.get("vmaf_min_score"),
                            "rejected": True,
                            "job_id": job_id,
                        },
                    )
                except Exception:
                    pass
                print(f"[WORKER] {reason}", flush=True)

        # For "combined" jobs, track removal is now handled inline during conversion
        # (no separate remux pass). Skip the remux block unless this is an "audio"-only job.
        if job_type == "audio":
            # Determine keep indices from probe
            raw_tracks = probe.get("audio_tracks", [])
            all_indices = [t["stream_index"] for t in raw_tracks]
            keep_indices = [i for i in all_indices if i not in audio_tracks_to_remove]

            # Reorder: native language tracks first so they become the default playback track
            # (only if enabled in audio settings — default on)
            try:
                from backend.scanner import detect_native_language, languages_match, _is_cleanup_enabled
                if not _is_cleanup_enabled("reorder_native_audio"):
                    raise Exception("reorder disabled")
                native_lang = detect_native_language(raw_tracks)
                try:
                    db_nl = await self._db()
                    try:
                        async with db_nl.execute(
                            "SELECT native_language FROM scan_results WHERE file_path = ?",
                            (current_file_path,),
                        ) as cur:
                            nl_row = await cur.fetchone()
                        if nl_row and nl_row["native_language"]:
                            native_lang = nl_row["native_language"]
                    finally:
                        await db_nl.close()
                except Exception:
                    pass
                if native_lang and native_lang.lower() != "und":
                    native_indices = []
                    other_indices = []
                    for idx in keep_indices:
                        track = next((t for t in raw_tracks if t["stream_index"] == idx), None)
                        if track and languages_match((track.get("language") or "").lower(), native_lang.lower()):
                            native_indices.append(idx)
                        else:
                            other_indices.append(idx)
                    if native_indices and native_indices[0] != keep_indices[0]:
                        keep_indices = native_indices + other_indices
                        print(f"[WORKER] Reordered audio: native ({native_lang}) tracks first", flush=True)
            except Exception as exc:
                print(f"[WORKER] Audio reorder failed (non-fatal): {exc}", flush=True)

            # Determine subtitle keep indices
            raw_subs = probe.get("subtitle_tracks", [])
            all_sub_indices = [t["stream_index"] for t in raw_subs]
            keep_sub_indices = [i for i in all_sub_indices if i not in subtitle_tracks_to_remove] if subtitle_tracks_to_remove else None

            if keep_indices != all_indices or (keep_sub_indices is not None and keep_sub_indices != all_sub_indices):
                # Same DB-write throttle as the convert path — see comment
                # on the convert progress_cb above.
                _audio_last_db = [0.0]
                async def audio_progress_cb(progress: float, eta_seconds=None, speed=None):
                    now = time.monotonic()
                    is_terminal = progress >= 99.99
                    if is_terminal or (now - _audio_last_db[0]) >= _PROGRESS_DB_WRITE_INTERVAL:
                        await self.queue.update_progress(job_id, progress, eta=eta_seconds)
                        _audio_last_db[0] = now
                    await ws_manager.send_job_progress(
                        job_id=job_id,
                        file_name=file_name,
                        progress=progress,
                        fps=speed,
                        eta=eta_seconds,
                        step="removing tracks" if audio_tracks_to_remove else ("removing subtitles" if subtitle_tracks_to_remove else "reordering audio"),
                        jobs_completed=jobs_completed,
                        jobs_total=jobs_total,
                        total_saved=total_saved,
                    )

                result = await remux_audio(
                    input_path=current_file_path,
                    keep_audio_indices=keep_indices,
                    duration=duration,
                    progress_callback=audio_progress_cb,
                    keep_subtitle_indices=keep_sub_indices,
                )
                if not result["success"]:
                    await self.queue.update_status(job_id, "failed", error_log=result["error"])
                    await ws_manager.send_job_complete(job_id, "failed", space_saved, result["error"])
                    return
                space_saved += result.get("space_saved", 0)

        # Rename audio codec in filename if it changed
        try:
            from backend.converter import get_audio_display_name, rename_audio_codec_in_filename
            final_probe = await probe_file(current_file_path)
            if final_probe and final_probe.get("audio_tracks"):
                primary_track = final_probe["audio_tracks"][0]
                primary_codec = primary_track.get("codec", "")
                primary_profile = primary_track.get("profile", "")
                new_audio_tag = get_audio_display_name(primary_codec, primary_profile)
                current_p = Path(current_file_path)
                new_stem = rename_audio_codec_in_filename(current_p.stem, new_audio_tag)
                if new_stem != current_p.stem:
                    new_path = current_p.parent / (new_stem + current_p.suffix)
                    old_path = current_file_path
                    current_p.rename(new_path)
                    print(f"[WORKER] Renamed audio codec in filename: {current_p.name} -> {new_path.name}", flush=True)
                    current_file_path = str(new_path)
                    # Update DB so jobs + scan_results reflect the new filename
                    try:
                        db_ac = await self.queue._connect()
                        try:
                            await db_ac.execute(
                                "UPDATE jobs SET file_path = ? WHERE id = ?",
                                (current_file_path, job_id),
                            )
                            await db_ac.execute(
                                "UPDATE scan_results SET file_path = ? WHERE file_path = ?",
                                (current_file_path, old_path),
                            )
                            await db_ac.commit()
                        finally:
                            await db_ac.close()
                    except Exception as exc2:
                        print(f"[WORKER] Audio-rename DB update failed (non-fatal): {exc2}", flush=True)
        except Exception as exc:
            print(f"[WORKER] Audio codec rename failed (non-fatal): {exc}", flush=True)

        await self.queue.update_space_saved(job_id, space_saved)

        # Inline post-conversion health check (keeps the same job card up; no re-queue)
        try:
            db = await self._db()
            try:
                async with db.execute(
                    "SELECT value FROM settings WHERE key = 'health_check_after_conversion'"
                ) as cur:
                    hc_row = await cur.fetchone()
                    _hc_raw = (str(hc_row["value"]).lower() if hc_row else "off")
                    hc_mode_post = {"true": "quick", "false": "off"}.get(_hc_raw, _hc_raw)
                    if hc_mode_post not in ("quick", "thorough"):
                        hc_mode_post = "off"
            finally:
                await db.close()

            if hc_mode_post != "off" and os.path.exists(current_file_path):
                from backend.health_check import run_check
                step_label = f"health check ({hc_mode_post})"
                print(f"[WORKER] Running inline {hc_mode_post} health check on {current_file_path}", flush=True)
                await ws_manager.send_job_progress(
                    job_id=job_id,
                    file_name=os.path.basename(current_file_path),
                    progress=100.0,
                    fps=None,
                    eta=None,
                    step=step_label,
                    jobs_completed=jobs_completed,
                    jobs_total=jobs_total,
                    total_saved=total_saved,
                )
                async def _hc_progress_cb(pct: float):
                    await self.queue.update_progress(job_id, pct)
                    await ws_manager.send_job_progress(
                        job_id=job_id,
                        file_name=os.path.basename(current_file_path),
                        progress=pct,
                        fps=None,
                        eta=None,
                        step=step_label,
                        jobs_completed=jobs_completed,
                        jobs_total=jobs_total,
                        total_saved=total_saved,
                    )
                try:
                    hc_result = await run_check(
                        current_file_path,
                        mode=hc_mode_post,
                        progress_cb=_hc_progress_cb,
                        duration_seconds_hint=duration,
                    )
                    hc_status = hc_result.get("status", "healthy")
                    hc_errors = hc_result.get("errors", [])
                    from datetime import datetime, timezone
                    now_iso = datetime.now(timezone.utc).isoformat()
                    db = await self._db()
                    try:
                        # scan_results still keyed by the original path here —
                        # the rename to current_file_path happens a bit later.
                        # Match both to be safe in case a previous run already renamed.
                        _errs_json = json.dumps(hc_errors) if hc_errors else None
                        await db.execute(
                            "UPDATE scan_results SET health_status = ?, health_errors_json = ?, "
                            "health_checked_at = ?, health_check_type = ? "
                            "WHERE file_path = ? OR file_path = ?",
                            (
                                hc_status,
                                _errs_json,
                                now_iso,
                                hc_mode_post,
                                file_path,
                                current_file_path,
                            ),
                        )
                        # Mirror onto the job row so the expanded Completed view sees it
                        await db.execute(
                            "UPDATE jobs SET health_status = ?, health_errors_json = ?, "
                            "health_check_type = ?, health_check_seconds = ? WHERE id = ?",
                            (
                                hc_status,
                                _errs_json,
                                hc_mode_post,
                                hc_result.get("duration_seconds"),
                                job_id,
                            ),
                        )
                        await db.commit()
                    finally:
                        await db.close()
                    print(f"[WORKER] Inline health check: {hc_status} ({hc_result.get('duration_seconds', 0)}s)", flush=True)
                    await self.queue._log_event(
                        current_file_path,
                        "health_check",
                        f"Health check: {hc_status} ({hc_mode_post})",
                        {
                            "status": hc_status,
                            "check_type": hc_mode_post,
                            "duration_seconds": hc_result.get("duration_seconds"),
                            "errors": hc_errors[:5] if hc_errors else None,
                            "job_id": job_id,
                        },
                    )
                except Exception as hc_exc:
                    print(f"[WORKER] Inline health check failed: {hc_exc}", flush=True)
        except Exception as exc:
            print(f"[WORKER] Failed to run inline health check: {exc}", flush=True)

        await self.queue.update_status(job_id, "completed")

        # Log the completed conversion to file_events. Three outcomes:
        #   1) Real conversion that saved space → "Converted: saved X GB"
        #   2) VMAF-rejected — encode completed but scored below threshold,
        #      original kept in place → "Kept original (VMAF below threshold)"
        #   3) skipped_larger — encode completed but was larger than source,
        #      original kept in place → "Kept original (encode was larger)"
        #   4) Fallback: conversion with zero savings → "Converted (no savings)"
        try:
            from backend.file_events import log_event, EVENT_COMPLETED
            if result.get("vmaf_rejected"):
                summary = "Kept original — VMAF below threshold"
            elif result.get("skipped_larger"):
                summary = "Kept original — encode was larger than source"
            elif space_saved > 0:
                gb = space_saved / (1024 ** 3)
                pct = (space_saved / file_size * 100) if file_size else 0
                summary = f"Converted: saved {gb:.2f} GB ({pct:.0f}%)"
            else:
                summary = "Converted (no savings)"
            await log_event(
                current_file_path,
                EVENT_COMPLETED,
                summary,
                {
                    "job_id": job_id,
                    "job_type": job_type,
                    "space_saved": space_saved,
                    "original_size": file_size,
                    "encoder": encoder,
                    "vmaf_score": locals().get("vmaf_score"),
                    "vmaf_rejected": bool(result.get("vmaf_rejected")),
                    "skipped_larger": bool(result.get("skipped_larger")),
                    "original_path": file_path if current_file_path != file_path else None,
                },
            )
        except Exception:
            pass

        # Update scan_results: change file_path to new name, mark as converted (x265)
        if current_file_path != file_path:
            try:
                db = await self._db()
                try:
                    # Update existing entry to new path
                    # Get new file size
                    try:
                        new_size = Path(current_file_path).stat().st_size
                    except OSError:
                        new_size = None
                    if new_size:
                        await db.execute(
                            "UPDATE scan_results SET file_path = ?, file_size = ?, video_codec = 'hevc', needs_conversion = 0, converted = 1 "
                            "WHERE file_path = ?",
                            (current_file_path, new_size, file_path),
                        )
                    else:
                        await db.execute(
                            "UPDATE scan_results SET file_path = ?, video_codec = 'hevc', needs_conversion = 0, converted = 1 "
                            "WHERE file_path = ?",
                            (current_file_path, file_path),
                        )
                    await db.commit()
                    print(f"[WORKER] Updated scan_results: {file_path} -> {current_file_path}", flush=True)
                finally:
                    await db.close()
            except Exception as exc:
                print(f"[WORKER] Failed to update scan_results (non-fatal): {exc}", flush=True)
        else:
            # Same path but might have had audio cleanup — update size and mark converted
            try:
                db = await self._db()
                try:
                    try:
                        new_size = Path(file_path).stat().st_size
                        await db.execute(
                            "UPDATE scan_results SET file_size = ?, needs_conversion = 0, converted = 1 WHERE file_path = ?",
                            (new_size, file_path),
                        )
                    except OSError:
                        await db.execute(
                            "UPDATE scan_results SET needs_conversion = 0, converted = 1 WHERE file_path = ?",
                            (file_path,),
                        )
                    await db.commit()
                finally:
                    await db.close()
            except Exception as exc:
                pass

        # Trigger Plex partial scan for the converted file's folder (if enabled)
        try:
            plex_scan_enabled = True
            try:
                db = await self._db()
                try:
                    async with db.execute("SELECT value FROM settings WHERE key = 'plex_scan_after_conversion'") as cur:
                        row = await cur.fetchone()
                        if row and row[0].lower() == "false":
                            plex_scan_enabled = False
                finally:
                    await db.close()
            except Exception:
                pass
            from backend.plex import trigger_plex_scan, empty_plex_trash
            section_id = await trigger_plex_scan(current_file_path) if plex_scan_enabled else None
            if section_id:
                # Only empty trash if the setting is enabled
                try:
                    db = await self._db()
                    try:
                        async with db.execute(
                            "SELECT value FROM settings WHERE key = 'plex_empty_trash_after_scan'"
                        ) as cur:
                            row = await cur.fetchone()
                            should_empty = row and row[0].lower() == "true"
                    finally:
                        await db.close()
                    if should_empty:
                        await empty_plex_trash(section_id)
                except Exception:
                    pass
        except Exception as exc:
            print(f"[WORKER] Plex scan/trash cleanup failed (non-fatal): {exc}", flush=True)

        # Trigger Jellyfin library refresh (if enabled)
        try:
            jf_scan_enabled = True
            try:
                db = await self._db()
                try:
                    async with db.execute("SELECT value FROM settings WHERE key = 'jellyfin_scan_after_conversion'") as cur:
                        row = await cur.fetchone()
                        if row and row[0].lower() == "false":
                            jf_scan_enabled = False
                finally:
                    await db.close()
            except Exception:
                pass
            if jf_scan_enabled:
                from backend.jellyfin import trigger_jellyfin_scan
                await trigger_jellyfin_scan(current_file_path)
        except Exception as exc:
            print(f"[WORKER] Jellyfin library refresh failed (non-fatal): {exc}", flush=True)

        # Auto-rename after conversion, if enabled
        try:
            import os as _os
            from backend.rename import get_settings as get_rename_settings, build_plan, apply_plan
            rs = await get_rename_settings()
            if rs.enabled_auto and space_saved > 0:
                plan = await build_plan(current_file_path, settings=rs)
                if plan.reason != "noop":
                    rename_result = await apply_plan(plan)
                    if rename_result.get("applied"):
                        old_path = current_file_path
                        current_file_path = rename_result.get("new_path", current_file_path)
                        print(f"[WORKER] Auto-renamed: {_os.path.basename(old_path)} → {_os.path.basename(current_file_path)}", flush=True)
                        # Update scan_results with the new path
                        try:
                            db_r = await self._db()
                            try:
                                await db_r.execute(
                                    "UPDATE scan_results SET file_path = ? WHERE file_path = ?",
                                    (current_file_path, old_path),
                                )
                                await db_r.commit()
                            finally:
                                await db_r.close()
                        except Exception:
                            pass
                    elif rename_result.get("error"):
                        print(f"[WORKER] Auto-rename failed (non-fatal): {rename_result['error']}", flush=True)
        except Exception as exc:
            print(f"[WORKER] Auto-rename hook error (non-fatal): {exc}", flush=True)

        # Trigger Sonarr/Radarr rescan (skip for NZBGet-sourced files —
        # Sonarr/Radarr will import them automatically after post-processing)
        from backend.config import settings
        media_root = getattr(settings, "media_root", "/media")
        is_media_path = current_file_path.startswith(media_root)
        if is_media_path:
            try:
                from backend.arr import trigger_arr_rescan
                arr_result = await trigger_arr_rescan(current_file_path)
                if arr_result.get("sonarr"):
                    print(f"[WORKER] Sonarr rescan triggered for {file_name}", flush=True)
                elif arr_result.get("radarr"):
                    print(f"[WORKER] Radarr rescan triggered for {file_name}", flush=True)
                elif any(arr_result.values()):
                    pass  # Already logged
                else:
                    print(f"[WORKER] No Sonarr/Radarr match for {file_name}", flush=True)
            except Exception as exc:
                print(f"[WORKER] Sonarr/Radarr rescan failed (non-fatal): {exc}", flush=True)
        else:
            print(f"[WORKER] Skipping arr rescan for non-library file: {file_name}", flush=True)

        # Mark file as ignored if no space was saved (so future scans tag it)
        # Store both original and current path (file may have been renamed x264→x265)
        if space_saved <= 0:
            try:
                db = await self._db()
                try:
                    for p in {file_path, current_file_path}:
                        await db.execute(
                            "INSERT OR IGNORE INTO ignored_files (file_path, reason, ignored_at) VALUES (?, ?, ?)",
                            (p, "no_savings", _utcnow()),
                        )
                    await db.commit()
                    print(f"[WORKER] Marked as ignored (no savings): {current_file_path}", flush=True)
                finally:
                    await db.close()
            except Exception as exc:
                print(f"[WORKER] Failed to mark ignored: {exc}", flush=True)
        await ws_manager.send_job_complete(job_id, "completed", space_saved, None)

        # Update daily stats aggregation
        try:
            from backend.database import update_daily_stats_for_job
            await update_daily_stats_for_job({
                "completed_at": _utcnow(), "started_at": job.get("started_at"),
                "space_saved": space_saved, "original_size": job.get("original_size", 0),
                "job_type": job_type, "file_path": current_file_path,
            })
        except Exception as exc:
            print(f"[WORKER] daily_stats update failed (non-fatal): {exc}", flush=True)

        # Cleanup expired backups (runs after each job — lightweight check)
        try:
            await _cleanup_expired_backups()
        except Exception:
            pass

        # Notifications: check if queue is now empty
        try:
            from backend.notifications import send_notification
            import os
            stats = await self.queue.get_stats()
            if stats["pending"] == 0 and stats["running"] <= 1:
                def _fmt(b: int) -> str:
                    if b >= 1024**4: return f"{b / 1024**4:.2f} TB"
                    if b >= 1024**3: return f"{b / 1024**3:.1f} GB"
                    return f"{b / 1024**2:.0f} MB"
                await send_notification("queue_complete", "Queue Complete",
                    f"All jobs finished! {stats['completed']} completed.",
                    {"Total saved": _fmt(stats["total_space_saved"])})
        except Exception:
            pass

        # Run post-conversion script (non-blocking — failure doesn't affect job)
        try:
            await _run_post_conversion_script(job_id, current_file_path, file_path, result, dict(job))
        except Exception as exc:
            print(f"[WORKER] Post-conversion script wrapper failed: {exc}", flush=True)

        # (post-conversion health check runs inline before the job is marked complete)
