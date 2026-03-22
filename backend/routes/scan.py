import json
from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, BackgroundTasks, HTTPException

from backend.database import DB_PATH
from backend.models import ScanRequest
from backend.scanner import scan_directory
from backend.websocket import ws_manager

router = APIRouter(prefix="/api/scan")


async def _run_scan(paths: list[str]) -> None:
    """Background task: scan paths and store results in DB."""

    async def progress_cb(
        status: str,
        current_file: str = "",
        files_found: int = 0,
        files_probed: int = 0,
        total_files: int = 0,
    ):
        await ws_manager.send_scan_progress(
            status=status,
            current_file=current_file,
            total=total_files,
            probed=files_probed,
        )

    all_results = []
    for path in paths:
        results = await scan_directory(path, progress_callback=progress_cb)
        all_results.extend(results)

    now = datetime.now(timezone.utc).isoformat()

    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        for scanned in all_results:
            audio_json = json.dumps([t.model_dump() for t in scanned.audio_tracks])
            await db.execute(
                """INSERT INTO scan_results
                   (file_path, file_size, video_codec, needs_conversion,
                    audio_tracks_json, native_language, scan_timestamp, removed_from_list)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                   ON CONFLICT(file_path) DO UPDATE SET
                       file_size=excluded.file_size,
                       video_codec=excluded.video_codec,
                       needs_conversion=excluded.needs_conversion,
                       audio_tracks_json=excluded.audio_tracks_json,
                       native_language=excluded.native_language,
                       scan_timestamp=excluded.scan_timestamp,
                       removed_from_list=0
                """,
                (
                    scanned.file_path,
                    scanned.file_size,
                    scanned.video_codec,
                    1 if scanned.needs_conversion else 0,
                    audio_json,
                    scanned.native_language,
                    now,
                ),
            )
        await db.commit()
    finally:
        await db.close()


@router.post("/start")
async def start_scan(request: ScanRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_scan, request.paths)
    return {"status": "started", "paths": request.paths}


@router.get("/results")
async def get_scan_results():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT * FROM scan_results WHERE removed_from_list = 0 ORDER BY id ASC"
        ) as cur:
            rows = await cur.fetchall()
            results = []
            for row in rows:
                r = dict(row)
                if r.get("audio_tracks_json"):
                    try:
                        r["audio_tracks"] = json.loads(r["audio_tracks_json"])
                    except (json.JSONDecodeError, ValueError):
                        r["audio_tracks"] = []
                else:
                    r["audio_tracks"] = []
                results.append(r)
            return results
    finally:
        await db.close()


@router.delete("/results/{result_id}")
async def delete_scan_result(result_id: int):
    db = await aiosqlite.connect(DB_PATH)
    try:
        await db.execute(
            "UPDATE scan_results SET removed_from_list = 1 WHERE id = ?", (result_id,)
        )
        await db.commit()
    finally:
        await db.close()
    return {"status": "deleted", "id": result_id}
