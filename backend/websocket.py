import time
from typing import Optional

from fastapi import WebSocket


# Minimum wall-clock interval (seconds) between `job_progress` broadcasts for
# the same job_id. ffmpeg emits a "frame=" line per frame — that's 2-4 updates
# per second per job, each of which causes a full React re-render in the UI.
# Throttling to 2 Hz gives a smooth progress bar without making Chrome cry.
_JOB_PROGRESS_MIN_INTERVAL = 0.5


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []
        # Per-job last-emit timestamps for throttling job_progress messages
        self._last_job_progress_emit: dict[int, float] = {}

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict) -> None:
        dead = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                dead.append(connection)
        for connection in dead:
            self.disconnect(connection)

    async def send_scan_progress(
        self,
        status: str,
        current_file: str,
        total: int,
        probed: int,
    ) -> None:
        await self.broadcast({
            "type": "scan_progress",
            "status": status,
            "current_file": current_file,
            "total": total,
            "probed": probed,
        })

    async def send_job_progress(
        self,
        job_id: int,
        file_name: str,
        progress: float,
        fps: Optional[float],
        eta: Optional[int],
        step: str,
        jobs_completed: int,
        jobs_total: int,
        total_saved: int,
        node_name: Optional[str] = None,
        node_id: Optional[str] = None,
    ) -> None:
        # Throttle per-job updates to _JOB_PROGRESS_MIN_INTERVAL. Always let
        # through the first update for a job (last is None) and terminal
        # updates (progress >= 99.99 — covers both the final ffmpeg frame
        # and our explicit progress=100 emits when switching into VMAF
        # analysis), so the UI never stalls on a stale number just before
        # the step changes.
        now = time.monotonic()
        last = self._last_job_progress_emit.get(job_id)
        is_terminal = progress >= 99.99
        if last is not None and not is_terminal and (now - last) < _JOB_PROGRESS_MIN_INTERVAL:
            return
        self._last_job_progress_emit[job_id] = now

        msg: dict = {
            "type": "job_progress",
            "job_id": job_id,
            "file_name": file_name,
            "progress": progress,
            "fps": fps,
            "eta": eta,
            "step": step,
            "jobs_completed": jobs_completed,
            "jobs_total": jobs_total,
            "total_saved": total_saved,
        }
        if node_name:
            msg["node_name"] = node_name
        if node_id:
            msg["node_id"] = node_id
        await self.broadcast(msg)

    def release_job_throttle(self, job_id: int) -> None:
        """
        Drop the per-job throttle entry for `job_id`. Safe to call multiple
        times. Used by the job-worker `finally` block so that non-terminal
        exits (node-pause requeue, cancelled, unhandled exceptions) don't
        leave stale entries in `_last_job_progress_emit` forever — and so a
        retry of the same job_id isn't silently swallowed until the 500ms
        interval elapses.
        """
        self._last_job_progress_emit.pop(job_id, None)

    async def send_job_complete(
        self,
        job_id: int,
        status: str,
        space_saved: int,
        error: Optional[str],
    ) -> None:
        # Also release on normal completion — belt-and-suspenders with the
        # worker's `finally` cleanup.
        self.release_job_throttle(job_id)
        await self.broadcast({
            "type": "job_complete",
            "job_id": job_id,
            "status": status,
            "space_saved": space_saved,
            "error": error,
        })


ws_manager = ConnectionManager()
