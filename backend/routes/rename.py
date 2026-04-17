"""API endpoints for file renaming — settings, preview, apply."""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import aiosqlite
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.database import DB_PATH, connect_db
from backend.rename import (
    RenameSettings,
    TOKEN_CATEGORIES,
    get_settings,
    save_settings,
    build_plan,
    apply_plan,
    render_pattern,
    resolve_metadata,
)


router = APIRouter(prefix="/api/rename")


# ── Settings ─────────────────────────────────────────────────────────────────

class SettingsPayload(BaseModel):
    enabled_auto: bool | None = None
    rename_folders: bool | None = None
    movie_file_pattern: str | None = None
    movie_folder_pattern: str | None = None
    tv_file_pattern: str | None = None
    tv_folder_pattern: str | None = None
    season_folder_pattern: str | None = None
    separator: str | None = None
    case_mode: str | None = None
    remove_illegal: bool | None = None


@router.get("/settings")
async def get_rename_settings():
    s = await get_settings()
    return asdict(s)


@router.put("/settings")
async def put_rename_settings(payload: SettingsPayload):
    data = {k: v for k, v in payload.dict().items() if v is not None}
    # Validate
    if "separator" in data and data["separator"] not in ("space", "dot", "dash", "underscore"):
        raise HTTPException(400, "separator must be one of: space, dot, dash, underscore")
    if "case_mode" in data and data["case_mode"] not in ("default", "lower", "upper"):
        raise HTTPException(400, "case_mode must be one of: default, lower, upper")
    s = await save_settings(data)
    return asdict(s)


@router.get("/tokens")
async def get_tokens():
    """List available tokens (for the frontend picker)."""
    return {"categories": TOKEN_CATEGORIES}


# ── Preview / Apply ──────────────────────────────────────────────────────────

class PreviewRequest(BaseModel):
    file_paths: list[str]
    # Optional in-memory settings override (so users can preview without saving)
    settings_override: dict | None = None


async def _expand_folder_selections(paths: list[str]) -> list[str]:
    """If any path ends with '/', expand it to the file paths inside by querying scan_results.
    Returns a deduped list of file paths (no folder paths)."""
    if not paths:
        return []
    db = await connect_db()
    try:
        file_paths: set[str] = set()
        folder_prefixes = []
        for p in paths:
            if p.endswith("/"):
                folder_prefixes.append(p)
            else:
                file_paths.add(p)
        for prefix in folder_prefixes:
            async with db.execute(
                "SELECT file_path FROM scan_results WHERE file_path LIKE ? AND removed_from_list = 0",
                (prefix + "%",),
            ) as cur:
                rows = await cur.fetchall()
            for row in rows:
                file_paths.add(row["file_path"])
    finally:
        await db.close()
    return sorted(file_paths)


async def _load_probe_info(file_paths: list[str]) -> dict[str, dict]:
    """Pull cached audio tracks + resolution from scan_results for a set of files."""
    if not file_paths:
        return {}
    db = await connect_db()
    try:
        placeholders = ",".join("?" * len(file_paths))
        async with db.execute(
            f"SELECT file_path, audio_tracks_json, video_height FROM scan_results "
            f"WHERE file_path IN ({placeholders})",
            file_paths,
        ) as cur:
            rows = await cur.fetchall()
    finally:
        await db.close()
    out: dict = {}
    for row in rows:
        tracks = []
        try:
            tracks = json.loads(row["audio_tracks_json"] or "[]")
        except Exception:
            pass
        out[row["file_path"]] = {
            "audio_tracks": tracks,
            "video_height": row["video_height"] or 0,
        }
    return out


@router.post("/preview")
async def preview_rename(req: PreviewRequest):
    """Return old/new paths for each file without touching disk."""
    if not req.file_paths:
        return {"plans": []}

    # Expand any folder-style paths (ending with /) to the files they contain
    file_paths = await _expand_folder_selections(req.file_paths)
    if not file_paths:
        return {"plans": []}

    settings = await get_settings()
    if req.settings_override:
        # Merge override into current settings (transient, doesn't save)
        import dataclasses
        kwargs = {**asdict(settings), **{k: v for k, v in req.settings_override.items() if v is not None}}
        # Validate enums on override
        if kwargs.get("separator") not in ("space", "dot", "dash", "underscore"):
            kwargs["separator"] = settings.separator
        if kwargs.get("case_mode") not in ("default", "lower", "upper"):
            kwargs["case_mode"] = settings.case_mode
        settings = RenameSettings(**kwargs)

    probe_map = await _load_probe_info(file_paths)

    plans = []
    for fp in file_paths:
        try:
            plan = await build_plan(fp, probe_map.get(fp), settings)
            plans.append({
                "old_path": plan.old_path,
                "new_path": plan.new_path,
                "old_folder": plan.old_folder,
                "new_folder": plan.new_folder,
                "old_season_folder": plan.old_season_folder,
                "new_season_folder": plan.new_season_folder,
                "reason": plan.reason,
                "changed": plan.reason != "noop",
            })
        except Exception as exc:
            plans.append({
                "old_path": fp,
                "new_path": fp,
                "error": str(exc),
                "changed": False,
                "reason": "error",
            })

    return {"plans": plans}


class ApplyRequest(BaseModel):
    file_paths: list[str]
    settings_override: dict | None = None
    rescan_arr: bool = True  # trigger Sonarr/Radarr refresh
    rescan_plex: bool = True


@router.post("/apply")
async def apply_rename(req: ApplyRequest):
    """Apply renames to disk and optionally rescan *arr/Plex."""
    if not req.file_paths:
        return {"results": []}

    file_paths = await _expand_folder_selections(req.file_paths)
    if not file_paths:
        return {"results": []}

    settings = await get_settings()
    if req.settings_override:
        kwargs = {**asdict(settings), **{k: v for k, v in req.settings_override.items() if v is not None}}
        settings = RenameSettings(**kwargs)

    probe_map = await _load_probe_info(file_paths)

    results = []
    renamed_paths = []
    for fp in file_paths:
        try:
            plan = await build_plan(fp, probe_map.get(fp), settings)
            if plan.reason == "noop":
                results.append({"old_path": fp, "new_path": fp, "applied": False, "error": "No changes"})
                continue
            result = await apply_plan(plan)
            results.append(result)
            if result.get("applied") and result.get("new_path"):
                renamed_paths.append((fp, result["new_path"]))

                # Update scan_results with the new path so the UI reflects it
                try:
                    db = await connect_db()
                    try:
                        await db.execute(
                            "UPDATE scan_results SET file_path = ? WHERE file_path = ?",
                            (result["new_path"], fp),
                        )
                        await db.commit()
                    finally:
                        await db.close()
                except Exception:
                    pass
        except Exception as exc:
            results.append({"old_path": fp, "new_path": fp, "applied": False, "error": str(exc)})

    # Trigger rescans for renamed files
    rescan_report: dict = {"arr": [], "plex": []}
    if renamed_paths:
        if req.rescan_arr:
            try:
                from backend.arr import trigger_arr_rescan
                # Dedupe folders so we don't hammer the *arr apps
                seen = set()
                for _, new_path in renamed_paths:
                    folder = "/".join(new_path.split("/")[:-1])
                    if folder in seen:
                        continue
                    seen.add(folder)
                    r = await trigger_arr_rescan(new_path)
                    rescan_report["arr"].append(r)
            except Exception as exc:
                rescan_report["arr_error"] = str(exc)
        if req.rescan_plex:
            try:
                from backend.plex import trigger_plex_scan
                seen = set()
                for _, new_path in renamed_paths:
                    folder = "/".join(new_path.split("/")[:-1])
                    if folder in seen:
                        continue
                    seen.add(folder)
                    await trigger_plex_scan(folder)
                    rescan_report["plex"].append(folder)
            except Exception as exc:
                rescan_report["plex_error"] = str(exc)

    return {"results": results, "rescans": rescan_report}


@router.post("/preview-pattern")
async def preview_pattern(payload: dict):
    """Render a pattern against a sample file path, without saving settings.

    Used for the live preview in the settings UI.
    """
    sample_path = payload.get("file_path") or "/media/Movies/Dragonfly (2002) [tmdb-10497]/Dragonfly.2002.1080p.BluRay.HDR.x265.DTS.5.1-DiMEPiECE.mkv"
    pattern = payload.get("pattern", "")
    settings_override = payload.get("settings", {})
    settings = await get_settings()
    import dataclasses
    kwargs = {**asdict(settings), **{k: v for k, v in settings_override.items() if v is not None}}
    settings = RenameSettings(**kwargs)

    meta = await resolve_metadata(sample_path)
    rendered = render_pattern(pattern, meta, settings)
    return {
        "rendered": rendered,
        "metadata": asdict(meta),
    }
