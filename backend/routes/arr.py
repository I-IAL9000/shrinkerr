"""API endpoints for *arr (Sonarr/Radarr) actions — research, rescan, etc."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.arr import research_file


router = APIRouter(prefix="/api/arr")


class ResearchRequest(BaseModel):
    file_path: str
    delete_file: bool = True


class BulkResearchRequest(BaseModel):
    file_paths: list[str]
    delete_file: bool = True


@router.post("/research")
async def research_single(payload: ResearchRequest):
    """Blocklist the current release and trigger a fresh download.

    Used for corrupt files or manual "I want a different release" replacements.
    Routes to Sonarr or Radarr based on folder naming conventions.
    """
    if not payload.file_path:
        raise HTTPException(status_code=400, detail="file_path required")
    result = await research_file(payload.file_path, delete_file=payload.delete_file)
    if not result.get("success"):
        # Return 200 with error details so the frontend can display a helpful message
        # rather than a generic HTTP error. The `success` field tells the UI.
        return result
    return result


@router.post("/research/bulk")
async def research_bulk(payload: BulkResearchRequest):
    """Re-request a batch of files in one call. Used on the Queue Failed tab."""
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
