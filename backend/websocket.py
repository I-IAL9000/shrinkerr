from typing import Optional

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

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

    async def send_job_complete(
        self,
        job_id: int,
        status: str,
        space_saved: int,
        error: Optional[str],
    ) -> None:
        await self.broadcast({
            "type": "job_complete",
            "job_id": job_id,
            "status": status,
            "space_saved": space_saved,
            "error": error,
        })


ws_manager = ConnectionManager()
