import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

logger = logging.getLogger("shrinkarr.queue")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobQueue:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def _connect(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        return db

    async def add_job(
        self,
        file_path: str,
        job_type: str,
        encoder: Optional[str] = None,
        audio_tracks_to_remove: Optional[list] = None,
    ) -> int:
        db = await self._connect()
        try:
            # Get current max queue_order
            async with db.execute("SELECT COALESCE(MAX(queue_order), 0) FROM jobs") as cur:
                row = await cur.fetchone()
                next_order = (row[0] or 0) + 1

            audio_json = json.dumps(audio_tracks_to_remove or [])
            now = _utcnow()
            async with db.execute(
                """INSERT INTO jobs
                   (file_path, job_type, status, encoder, audio_tracks_to_remove,
                    created_at, queue_order)
                   VALUES (?, ?, 'pending', ?, ?, ?, ?)""",
                (file_path, job_type, encoder, audio_json, now, next_order),
            ) as cur:
                job_id = cur.lastrowid
            await db.commit()
            return job_id
        finally:
            await db.close()

    async def get_next_job(self) -> Optional[dict]:
        db = await self._connect()
        try:
            async with db.execute(
                "SELECT * FROM jobs WHERE status = 'pending' ORDER BY queue_order ASC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
                if row is None:
                    return None
                return dict(row)
        finally:
            await db.close()

    async def get_jobs_by_status(self, status: str) -> list[dict]:
        db = await self._connect()
        try:
            async with db.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY queue_order ASC", (status,)
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]
        finally:
            await db.close()

    async def get_all_jobs(self) -> list[dict]:
        db = await self._connect()
        try:
            async with db.execute("SELECT * FROM jobs ORDER BY queue_order ASC") as cur:
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

    async def get_stats(self) -> dict:
        db = await self._connect()
        try:
            async with db.execute("SELECT COUNT(*) FROM jobs") as cur:
                total = (await cur.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'pending'"
            ) as cur:
                pending = (await cur.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'running'"
            ) as cur:
                running = (await cur.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'completed'"
            ) as cur:
                completed = (await cur.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'failed'"
            ) as cur:
                failed = (await cur.fetchone())[0]
            async with db.execute(
                "SELECT COALESCE(SUM(space_saved), 0) FROM jobs WHERE status = 'completed'"
            ) as cur:
                total_space_saved = (await cur.fetchone())[0]
            return {
                "total_jobs": total,
                "pending": pending,
                "running": running,
                "completed": completed,
                "failed": failed,
                "total_space_saved": total_space_saved,
            }
        finally:
            await db.close()


class QueueWorker:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.queue = JobQueue(db_path)
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._paused = False

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        self._task.add_done_callback(self._task_done)

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

    async def _run_loop(self) -> None:
        print("[WORKER] Loop started, running=%s paused=%s" % (self._running, self._paused), flush=True)
        while self._running:
            if self._paused:
                await asyncio.sleep(1)
                continue
            try:
                job = await self.queue.get_next_job()
            except Exception as exc:
                print(f"[WORKER] Failed to get next job: {exc}", flush=True)
                import traceback; traceback.print_exc()
                await asyncio.sleep(5)
                continue
            if job is None:
                await asyncio.sleep(1)
                continue
            print(f"[WORKER] Processing job {job['id']}: {job['file_path']}", flush=True)
            try:
                await self._process_job(job)
                print(f"[WORKER] Job {job['id']} completed", flush=True)
            except Exception as exc:
                print(f"[WORKER] Job {job['id']} FAILED: {exc}", flush=True)
                import traceback; traceback.print_exc()
                try:
                    await self.queue.update_status(job["id"], "failed", error_log=str(exc))
                except Exception:
                    pass

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

        await self.queue.update_status(job_id, "running")
        print(f"[WORKER] Job {job_id} status set to running", flush=True)

        # Probe for duration
        probe = await probe_file(file_path)
        if probe is None:
            print(f"[WORKER] Job {job_id}: FAILED to probe {file_path}", flush=True)
            await self.queue.update_status(job_id, "failed", error_log="Failed to probe file")
            return
        print(f"[WORKER] Job {job_id}: probed OK, duration={probe.get('duration', 0):.1f}s, codec={probe.get('video_codec', '?')}", flush=True)

        duration = probe.get("duration", 0.0)
        import os
        file_name = os.path.basename(file_path)

        # Get stats for websocket
        stats = await self.queue.get_stats()
        jobs_total = stats["total_jobs"]
        jobs_completed = stats["completed"]
        total_saved = stats["total_space_saved"]

        space_saved = 0
        current_file_path = file_path

        if job_type in ("convert", "combined"):
            async def progress_cb(progress: float, fps=None, eta_seconds=None):
                await self.queue.update_progress(job_id, progress, fps=fps, eta=eta_seconds)
                await ws_manager.send_job_progress(
                    job_id=job_id,
                    file_name=file_name,
                    progress=progress,
                    fps=fps,
                    eta=eta_seconds,
                    step="converting",
                    jobs_completed=jobs_completed,
                    jobs_total=jobs_total,
                    total_saved=total_saved,
                )

            result = await convert_file(
                input_path=current_file_path,
                encoder=encoder,
                duration=duration,
                progress_callback=progress_cb,
            )
            if not result["success"]:
                await self.queue.update_status(job_id, "failed", error_log=result["error"])
                await ws_manager.send_job_complete(job_id, "failed", 0, result["error"])
                return
            space_saved += result.get("space_saved", 0)
            current_file_path = result["output_path"]

        if job_type in ("audio", "combined"):
            # Determine keep indices from probe
            raw_tracks = probe.get("audio_tracks", [])
            all_indices = [t["stream_index"] for t in raw_tracks]
            keep_indices = [i for i in all_indices if i not in audio_tracks_to_remove]

            if keep_indices != all_indices:
                result = await remux_audio(
                    input_path=current_file_path,
                    keep_audio_indices=keep_indices,
                )
                if not result["success"]:
                    await self.queue.update_status(job_id, "failed", error_log=result["error"])
                    await ws_manager.send_job_complete(job_id, "failed", space_saved, result["error"])
                    return
                space_saved += result.get("space_saved", 0)

        await self.queue.update_space_saved(job_id, space_saved)
        await self.queue.update_status(job_id, "completed")
        await ws_manager.send_job_complete(job_id, "completed", space_saved, None)
