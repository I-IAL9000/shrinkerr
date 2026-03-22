from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from backend.database import init_db, DB_PATH
from backend.queue import JobQueue, QueueWorker
from backend.websocket import ws_manager
from backend.scheduler import init_scheduler
from backend.routes.jobs import router as jobs_router, init_job_routes
from backend.routes.scan import router as scan_router
from backend.routes.schedule import router as schedule_router
from backend.routes.settings import router as settings_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    queue = JobQueue(DB_PATH)
    worker = QueueWorker(DB_PATH)
    init_job_routes(worker, queue)
    init_scheduler(worker.start)
    yield
    worker.stop()


app = FastAPI(title="Shrinkarr", lifespan=lifespan)

# Include routers
app.include_router(jobs_router)
app.include_router(scan_router)
app.include_router(schedule_router)
app.include_router(settings_router)


@app.get("/api/health")
async def health_check():
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
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
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host="0.0.0.0", port=6680, reload=True)
