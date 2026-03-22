import json
from typing import Optional

import aiosqlite
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.database import DB_PATH
from backend.models import MediaDir, SettingsUpdate

router = APIRouter(prefix="/api/settings")

# Default encoding settings
_ENCODING_DEFAULTS = {
    "default_encoder": "nvenc",
    "nvenc_cq": "20",
    "libx265_crf": "20",
    "always_keep_languages": '["eng", "isl", "ice"]',
    "ignore_unknown_tracks": "true",
}


@router.get("/dirs")
async def list_media_dirs():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT * FROM media_dirs ORDER BY id ASC") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
    finally:
        await db.close()


@router.post("/dirs")
async def add_media_dir(media_dir: MediaDir):
    db = await aiosqlite.connect(DB_PATH)
    try:
        try:
            async with db.execute(
                "INSERT INTO media_dirs (path, label, enabled) VALUES (?, ?, ?)",
                (media_dir.path, media_dir.label, 1 if media_dir.enabled else 0),
            ) as cur:
                new_id = cur.lastrowid
            await db.commit()
        except aiosqlite.IntegrityError:
            raise HTTPException(status_code=409, detail="Directory already exists")
    finally:
        await db.close()
    return {"id": new_id, "path": media_dir.path, "label": media_dir.label, "enabled": media_dir.enabled}


@router.delete("/dirs/{dir_id}")
async def delete_media_dir(dir_id: int):
    db = await aiosqlite.connect(DB_PATH)
    try:
        await db.execute("DELETE FROM media_dirs WHERE id = ?", (dir_id,))
        await db.commit()
    finally:
        await db.close()
    return {"status": "deleted", "id": dir_id}


@router.get("/encoding")
async def get_encoding_settings():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT key, value FROM settings") as cur:
            rows = await cur.fetchall()
            db_settings = {r["key"]: r["value"] for r in rows}
    finally:
        await db.close()

    # Merge with defaults
    merged = {**_ENCODING_DEFAULTS, **db_settings}

    # Parse types
    result = {
        "default_encoder": merged.get("default_encoder", "nvenc"),
        "nvenc_cq": int(merged.get("nvenc_cq", 20)),
        "libx265_crf": int(merged.get("libx265_crf", 20)),
        "ignore_unknown_tracks": merged.get("ignore_unknown_tracks", "true").lower() == "true",
    }
    try:
        result["always_keep_languages"] = json.loads(
            merged.get("always_keep_languages", '["eng", "isl", "ice"]')
        )
    except (json.JSONDecodeError, ValueError):
        result["always_keep_languages"] = ["eng", "isl", "ice"]

    return result


@router.put("/encoding")
async def update_encoding_settings(update: SettingsUpdate):
    db = await aiosqlite.connect(DB_PATH)
    try:
        updates = {}
        if update.default_encoder is not None:
            updates["default_encoder"] = update.default_encoder
        if update.nvenc_cq is not None:
            updates["nvenc_cq"] = str(update.nvenc_cq)
        if update.libx265_crf is not None:
            updates["libx265_crf"] = str(update.libx265_crf)
        if update.always_keep_languages is not None:
            updates["always_keep_languages"] = json.dumps(update.always_keep_languages)
        if update.ignore_unknown_tracks is not None:
            updates["ignore_unknown_tracks"] = "true" if update.ignore_unknown_tracks else "false"

        for key, value in updates.items():
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        await db.commit()
    finally:
        await db.close()
    return {"status": "updated", "keys": list(updates.keys())}
