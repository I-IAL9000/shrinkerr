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
from backend.database import connect_db
from backend.file_events import log_event, EVENT_ARR_ACTION


router = APIRouter(prefix="/api/arr")


def _summary_for_action(action: str, result: dict) -> str:
    """Build a one-line human-readable summary from a per-file result dict."""
    service = result.get("service") or "?"
    if action == "replace":
        title = result.get("series") or result.get("movie") or ""
        bits = []
        if result.get("blocklisted"):
            bits.append("blocklisted")
        if result.get("deleted"):
            bits.append("deleted file")
        if result.get("searched"):
            bits.append("search triggered")
        tail = f" — {', '.join(bits)}" if bits else ""
        return f"Replace ({service}): {title}{tail}" if title else f"Replace ({service}){tail}"
    if action == "upgrade":
        if service == "sonarr":
            ep_ids = result.get("episode_ids") or []
            return f"Upgrade search (sonarr): {result.get('series', '?')} — {len(ep_ids)} episode(s)"
        return f"Upgrade search (radarr): {result.get('movie', '?')}"
    if action == "missing":
        return "Missing-episode search"
    return f"*arr action: {action}"


async def _log_arr_event(action: str, file_path: str, result: dict) -> None:
    """Write a file_events row for an *arr action. Swallows all errors."""
    success = bool(result.get("success"))
    summary = _summary_for_action(action, result)
    if not success:
        summary = f"Failed {action}: {result.get('error', 'unknown error')}"
    details = {
        "action": action,
        "success": success,
        # Trim the blob — response dicts can carry large episode_ids lists;
        # the activity log doesn't need them, the caller already got them.
        **{k: v for k, v in result.items() if k not in ("results", "details")},
    }
    try:
        await log_event(file_path, EVENT_ARR_ACTION, summary, details)
        tag = action.upper()
        print(f"[ARR-ACTION] {tag} {'OK' if success else 'FAIL'} → {file_path} · {summary}", flush=True)
    except Exception:
        pass


Action = Literal["replace", "upgrade", "missing"]


class ActionRequest(BaseModel):
    file_path: str
    action: Action = "replace"
    delete_file: bool = True  # only used when action == "replace"


class BulkActionRequest(BaseModel):
    file_paths: list[str]
    action: Action = "replace"
    delete_file: bool = True  # only used when action == "replace"


async def _expand_folder_paths(paths: list[str]) -> list[str]:
    """Expand any folder paths (ending with /) to the files inside them by
    looking them up in scan_results. File paths pass through unchanged.

    This is what makes per-file actions (upgrade / replace) work on folder
    selections like "/TV/Bluey/" — the backend fans out to every episode in
    scan_results rather than requiring the frontend to have pre-loaded the
    folder's children.
    """
    folders = [p for p in paths if p.endswith("/")]
    files = [p for p in paths if not p.endswith("/")]
    if not folders:
        return files

    expanded: list[str] = list(files)
    seen: set[str] = set(files)

    db = await connect_db()
    try:
        for folder in folders:
            # scan_results file_path is the full absolute path; folders end
            # with "/" and every file inside starts with that prefix.
            async with db.execute(
                "SELECT file_path FROM scan_results "
                "WHERE file_path LIKE ? AND removed_from_list = 0",
                (folder + "%",),
            ) as cur:
                async for row in cur:
                    fp = row["file_path"]
                    if fp not in seen:
                        seen.add(fp)
                        expanded.append(fp)
    finally:
        await db.close()

    return expanded


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
    result = await dispatch_action(payload.action, payload.file_path, delete_file=payload.delete_file)
    await _log_arr_event(payload.action, payload.file_path, result)
    return result


@router.post("/action/bulk")
async def action_bulk(payload: BulkActionRequest):
    """Run an *arr action on a batch of files.

    Folder paths (those ending with "/") are accepted and expanded to file
    paths via scan_results so selecting a whole series folder works without
    the frontend having to pre-load its children. For `missing` the batch
    is deduped to unique series first, so selecting 154 files from one show
    fires exactly one per-series missing search.
    """
    if not payload.file_paths:
        raise HTTPException(status_code=400, detail="file_paths required")

    # "missing" is inherently bulk + series-level — search_missing_episodes
    # handles folder paths directly (walks up to find the containing series)
    if payload.action == "missing":
        result = await search_missing_episodes(payload.file_paths)

        # Log one event per resolved series so the per-file History + Activity
        # page both reflect the action. We use the first file_path we have
        # for that series (if any) as the event's file_path.
        try:
            import os
            details_list = result.get("details") or []
            # Build a lookup: series_title → first path from the selection
            # whose folder-name matches. Loose but good enough for attribution.
            first_path_for_title: dict[str, str] = {}
            for p in payload.file_paths:
                # Series folders usually include the show title; fall back to
                # the selection path itself.
                parts = os.path.basename(p.rstrip("/"))
                first_path_for_title.setdefault(parts, p)

            for d in details_list:
                title = d.get("series_title", "")
                attribution_path = ""
                # Try to find a path from the selection matching this series.
                for p in payload.file_paths:
                    if title and title.lower() in p.lower():
                        attribution_path = p
                        break
                if not attribution_path and payload.file_paths:
                    attribution_path = payload.file_paths[0]
                summary = (f"Missing search (sonarr): {title} — "
                           f"{d.get('missing_count', 0)} missing episode(s)"
                           + (", search triggered" if d.get("searched") else ""))
                await log_event(attribution_path, EVENT_ARR_ACTION, summary, {
                    "action": "missing",
                    "success": bool(d.get("searched")),
                    "series_id": d.get("series_id"),
                    "series_title": title,
                    "missing_count": d.get("missing_count"),
                    "note": d.get("note"),
                })
            print(
                f"[ARR-ACTION] MISSING {'OK' if result.get('success') else 'FAIL'} — "
                f"{result.get('series_searched', 0)}/{result.get('series_resolved', 0)} series, "
                f"{result.get('total_episode_ids', 0)} episode search(es) fired",
                flush=True,
            )
        except Exception:
            pass

        return result

    # "replace" and "upgrade" operate per-file — expand any folder selections
    # into the files they contain before we process each one.
    file_paths = await _expand_folder_paths(payload.file_paths)
    if not file_paths:
        return {
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "action": payload.action,
            "results": [],
            "error": "No files to process — selected folders had no known files in scan_results",
        }

    results: list[dict] = []
    ok_count = 0
    for path in file_paths:
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
        # Log every per-file result so the History tab and Activity page
        # reflect exactly what happened.
        await _log_arr_event(payload.action, path, r)
        results.append(r)
        if r.get("success"):
            ok_count += 1

    print(
        f"[ARR-ACTION] BULK {payload.action.upper()}: {ok_count}/{len(file_paths)} succeeded",
        flush=True,
    )

    return {
        "total": len(file_paths),
        "succeeded": ok_count,
        "failed": len(file_paths) - ok_count,
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
    result = await research_file(payload.file_path, delete_file=payload.delete_file)
    await _log_arr_event("replace", payload.file_path, result)
    return result


@router.post("/research/bulk")
async def research_bulk(payload: BulkResearchRequest):
    """Alias for /action/bulk with action=replace. Also expands folder paths."""
    if not payload.file_paths:
        raise HTTPException(status_code=400, detail="file_paths required")

    file_paths = await _expand_folder_paths(payload.file_paths)

    results: list[dict] = []
    ok_count = 0
    for path in file_paths:
        try:
            r = await research_file(path, delete_file=payload.delete_file)
        except Exception as exc:
            r = {"success": False, "error": str(exc)}
        r["file_path"] = path
        await _log_arr_event("replace", path, r)
        results.append(r)
        if r.get("success"):
            ok_count += 1

    print(f"[ARR-ACTION] BULK REPLACE (legacy /research/bulk): {ok_count}/{len(file_paths)} succeeded", flush=True)

    return {
        "total": len(file_paths),
        "succeeded": ok_count,
        "failed": len(file_paths) - ok_count,
        "results": results,
    }
