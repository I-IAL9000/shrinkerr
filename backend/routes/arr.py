"""API endpoints for *arr (Sonarr/Radarr) actions — replace / upgrade / missing."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.arr import (
    research_file,
    upgrade_file,
    search_missing_episodes,
    dispatch_action,
)


router = APIRouter(prefix="/api/arr")


Action = Literal["replace", "upgrade", "missing"]


class ActionRequest(BaseModel):
    file_path: str
    action: Action = "replace"
    delete_file: bool = True  # only used when action == "replace"


class BulkActionRequest(BaseModel):
    file_paths: list[str]
    action: Action = "replace"
    delete_file: bool = True  # only used when action == "replace"


# ── Unified action endpoint ───────────────────────────────────────────────


@router.post("/action")
async def action_single(payload: ActionRequest):
    """Run a single-file *arr action.

    Actions:
      * replace — blocklist current release, delete file, search for a fresh
        download. Use for corrupt or unwanted releases.
      * upgrade — search for a better release per the quality profile cutoff.
        No blocklist, no delete. Safe to run repeatedly.
      * missing — search for missing monitored episodes in this file's series
        (Sonarr only; movies have no per-file missing concept).
    """
    if not payload.file_path:
        raise HTTPException(status_code=400, detail="file_path required")
    return await dispatch_action(payload.action, payload.file_path, delete_file=payload.delete_file)


@router.post("/action/bulk")
async def action_bulk(payload: BulkActionRequest):
    """Run an *arr action on a batch of files.

    For `missing` the batch is deduped to unique series first, so selecting
    154 files from one show fires exactly one per-series missing search.
    """
    if not payload.file_paths:
        raise HTTPException(status_code=400, detail="file_paths required")

    # "missing" is inherently bulk — dedup happens inside search_missing_episodes
    if payload.action == "missing":
        return await search_missing_episodes(payload.file_paths)

    # "replace" and "upgrade" operate per-file
    results: list[dict] = []
    ok_count = 0
    for path in payload.file_paths:
        try:
            if payload.action == "replace":
                r = await research_file(path, delete_file=payload.delete_file)
            elif payload.action == "upgrade":
                r = await upgrade_file(path)
            else:
                r = {"success": False, "error": f"Unknown action: {payload.action}"}
        except Exception as exc:
            r = {"success": False, "error": str(exc)}
        r["file_path"] = path
        results.append(r)
        if r.get("success"):
            ok_count += 1

    return {
        "total": len(payload.file_paths),
        "succeeded": ok_count,
        "failed": len(payload.file_paths) - ok_count,
        "action": payload.action,
        "results": results,
    }


# ── Backwards-compat aliases — the Queue Failed tab + FileDetail were wired
#    to /research and /research/bulk before the action-based unification.
#    Keeping them so nothing breaks until callers migrate.
# ─────────────────────────────────────────────────────────────────────────


class ResearchRequest(BaseModel):
    file_path: str
    delete_file: bool = True


class BulkResearchRequest(BaseModel):
    file_paths: list[str]
    delete_file: bool = True


@router.post("/research")
async def research_single(payload: ResearchRequest):
    """Alias for /action with action=replace."""
    if not payload.file_path:
        raise HTTPException(status_code=400, detail="file_path required")
    return await research_file(payload.file_path, delete_file=payload.delete_file)


@router.post("/research/bulk")
async def research_bulk(payload: BulkResearchRequest):
    """Alias for /action/bulk with action=replace."""
    if not payload.file_paths:
        raise HTTPException(status_code=400, detail="file_paths required")

    results: list[dict] = []
    ok_count = 0
    for path in payload.file_paths:
        try:
            r = await research_file(path, delete_file=payload.delete_file)
        except Exception as exc:
            r = {"success": False, "error": str(exc)}
        r["file_path"] = path
        results.append(r)
        if r.get("success"):
            ok_count += 1

    return {
        "total": len(payload.file_paths),
        "succeeded": ok_count,
        "failed": len(payload.file_paths) - ok_count,
        "results": results,
    }
