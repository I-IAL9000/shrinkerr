import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger("squeezarr.queue")


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
    env["SQUEEZARR_EVENT"] = "job_completed"
    env["SQUEEZARR_JOB_ID"] = str(job_id)
    env["SQUEEZARR_FILE_PATH"] = str(file_path)
    env["SQUEEZARR_ORIGINAL_PATH"] = str(original_path or file_path)
    env["SQUEEZARR_JOB_TYPE"] = str(job_data.get("job_type", ""))
    env["SQUEEZARR_SPACE_SAVED"] = str(result.get("space_saved", 0))
    env["SQUEEZARR_ORIGINAL_SIZE"] = str(job_data.get("original_size", 0))
    env["SQUEEZARR_ENCODER"] = str(job_data.get("encoder", ""))
    env["SQUEEZARR_PRESET"] = str(job_data.get("nvenc_preset", ""))
    env["SQUEEZARR_CQ"] = str(job_data.get("nvenc_cq", ""))
    env["SQUEEZARR_FPS"] = str(round(job_data.get("fps", 0) or 0, 1))
    env["SQUEEZARR_VMAF_SCORE"] = str(result.get("vmaf_score", ""))
    env["SQUEEZARR_STATUS"] = "completed" if result.get("success") else "failed"
    env["SQUEEZARR_ERROR"] = str(result.get("error", ""))

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
                    nvenc_preset, nvenc_cq, audio_codec, audio_bitrate, libx265_crf, target_resolution, priority)
                   VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (file_path, job_type, encoder, audio_json, sub_json, now, next_order,
                 original_size or 0, nvenc_preset, nvenc_cq, audio_codec, audio_bitrate,
                 libx265_crf, target_resolution, priority),
            ) as cur:
                job_id = cur.lastrowid
            await db.commit()
            return job_id
        finally:
            await db.close()

    async def reset_stale_running(self) -> int:
        """Reset jobs stuck in 'running' status back to 'pending' (e.g. after a restart)."""
        db = await self._connect()
        try:
            async with db.execute(
                "UPDATE jobs SET status = 'pending', progress = 0, fps = NULL, "
                "eta_seconds = NULL, started_at = NULL, error_log = NULL "
                "WHERE status = 'running'"
            ) as cur:
                count = cur.rowcount
            await db.commit()
            if count:
                print(f"[QUEUE] Reset {count} stale running job(s) back to pending", flush=True)
            return count
        finally:
            await db.close()

    async def get_next_job(self, exclude_ids: list[int] | None = None) -> Optional[dict]:
        db = await self._connect()
        try:
            # Priority DESC ensures Highest (2) before High (1) before Normal (0)
            # Within same priority, FIFO by queue_order
            if exclude_ids:
                placeholders = ",".join("?" * len(exclude_ids))
                query = f"SELECT * FROM jobs WHERE status = 'pending' AND id NOT IN ({placeholders}) ORDER BY priority DESC, queue_order ASC LIMIT 1"
                async with db.execute(query, exclude_ids) as cur:
                    row = await cur.fetchone()
            else:
                async with db.execute(
                    "SELECT * FROM jobs WHERE status = 'pending' ORDER BY priority DESC, queue_order ASC LIMIT 1"
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

    # Clean .squeezarr_backup in media dirs
    for media_dir in media_dirs:
        for root, dirs, _files in os.walk(media_dir):
            if ".squeezarr_backup" in dirs:
                cleanup_dir(os.path.join(root, ".squeezarr_backup"))
            dirs[:] = [d for d in dirs if d != ".squeezarr_backup"]

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
                max_parallel = await self._get_parallel_limit()
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
                job = await self.queue.get_next_job(exclude_ids=running_ids)
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
        finally:
            self._active_procs.pop(job_id, None)
            self._active_tasks.pop(job_id, None)
            self._cancel_flags.discard(job_id)

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
        print(f"[WORKER] Job {job_id} status set to running", flush=True)

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
            async def progress_cb(progress: float, fps=None, eta_seconds=None, step=None):
                await self.queue.update_progress(job_id, progress, fps=fps, eta=eta_seconds)
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

            # Pass per-job encoding overrides (None means use global settings)
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
                override_target_resolution=job.get("target_resolution"),
                nice=use_nice,
            )
            if not result["success"]:
                if job_id in self._cancel_flags:
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

            # Store VMAF score from converter (analysis runs inside convert_file before original is moved)
            vmaf_score = result.get("vmaf_score")
            if vmaf_score is not None:
                try:
                    db = await self._db()
                    try:
                        await db.execute("UPDATE jobs SET vmaf_score = ? WHERE id = ?", (vmaf_score, job_id))
                        # Use original file_path — scan_results hasn't been renamed yet at this point
                        cur = await db.execute("UPDATE scan_results SET vmaf_score = ? WHERE file_path = ?", (vmaf_score, file_path))
                        rows_updated = cur.rowcount
                        await db.commit()
                        if rows_updated == 0:
                            print(f"[WORKER] VMAF score {vmaf_score} NOT saved to scan_results — file_path not found: {file_path}", flush=True)
                        else:
                            print(f"[WORKER] VMAF score {vmaf_score} saved to scan_results for {file_name}", flush=True)
                    finally:
                        await db.close()
                except Exception as exc:
                    print(f"[WORKER] Failed to store VMAF score: {exc}", flush=True)

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

        if job_type in ("audio", "combined"):
            # Determine keep indices from probe
            raw_tracks = probe.get("audio_tracks", [])
            all_indices = [t["stream_index"] for t in raw_tracks]
            keep_indices = [i for i in all_indices if i not in audio_tracks_to_remove]

            # Reorder: native language tracks first so they become the default playback track
            try:
                from backend.scanner import detect_native_language, languages_match
                native_lang = detect_native_language(raw_tracks)
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
                async def audio_progress_cb(progress: float, eta_seconds=None, speed=None):
                    await self.queue.update_progress(job_id, progress, eta=eta_seconds)
                    await ws_manager.send_job_progress(
                        job_id=job_id,
                        file_name=file_name,
                        progress=progress,
                        fps=speed,
                        eta=eta_seconds,
                        step="removing tracks" if audio_tracks_to_remove else "removing subtitles",
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
                    current_p.rename(new_path)
                    print(f"[WORKER] Renamed audio codec in filename: {current_p.name} -> {new_path.name}", flush=True)
                    current_file_path = str(new_path)
        except Exception as exc:
            print(f"[WORKER] Audio codec rename failed (non-fatal): {exc}", flush=True)

        await self.queue.update_space_saved(job_id, space_saved)
        await self.queue.update_status(job_id, "completed")

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

        # Trigger Plex partial scan for the converted file's folder
        try:
            from backend.plex import trigger_plex_scan, empty_plex_trash
            section_id = await trigger_plex_scan(current_file_path)
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
