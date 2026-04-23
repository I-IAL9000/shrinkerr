import json
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite
from fastapi import APIRouter, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.database import DB_PATH, connect_db
from backend.models import MediaDir, SettingsUpdate

BACKUP_DIR = Path(DB_PATH).parent / "backups"

router = APIRouter(prefix="/api/settings")

# Default encoding settings
_ENCODING_DEFAULTS = {
    "default_encoder": "nvenc",
    "nvenc_cq": "20",
    "libx265_crf": "20",
    "nvenc_preset": "p6",
    "libx265_preset": "medium",
    # libx265 fallback used by CPU workers when they have to take an NVENC
    # job. Empty = fall back to the NVENC→libx265 translation table.
    "nvenc_cpu_fallback_preset": "",
    "nvenc_cpu_fallback_crf": "",
    # NVENC fallback used by GPU workers when they have to take a libx265
    # job. Empty = fall back to the server's nvenc defaults or hardcoded p6/20.
    "libx265_gpu_fallback_preset": "",
    "libx265_gpu_fallback_cq": "",
    "parallel_jobs": "1",
    "ffmpeg_timeout": "21600",
    "ffprobe_timeout": "30",
    "audio_cleanup_enabled": "true",
    "always_keep_languages": '[]',
    "ignore_unknown_tracks": "true",
    "keep_native_language": "true",
    "target_codec": "hevc",
    "target_resolution": "copy",
    "source_codecs": '["h264", "mpeg2", "mpeg4", "vc1"]',
    "sub_cleanup_enabled": "true",
    "sub_keep_languages": '[]',
    "sub_keep_unknown": "true",
    "audio_codec": "copy",
    "audio_bitrate": "128",
    "audio_downmix": "false",
    "auto_queue_new": "false",
    "auto_convert_lossless": "false",
    "lossless_target_codec": "eac3",
    "lossless_target_bitrate": "640",
    "tmdb_api_key": "",
    "plex_url": "",
    "plex_token": "",
    "plex_path_mapping": "",
    "plex_scan_after_conversion": "true",
    "plex_empty_trash_after_scan": "false",
    "plex_ignore_labels": "",
    "plex_prioritize_unwatched": "false",
    "plex_pause_on_stream": "false",
    "plex_pause_stream_threshold": "1",
    "plex_pause_transcode_only": "true",
    "jellyfin_url": "",
    "jellyfin_api_key": "",
    "jellyfin_user_id": "",
    "jellyfin_path_mapping": "",
    "jellyfin_scan_after_conversion": "true",
    "jellyfin_empty_trash": "false",
    "jellyfin_pause_on_stream": "false",
    "jellyfin_pause_stream_threshold": "1",
    "jellyfin_pause_transcode_only": "true",
    # Conversion filters
    "min_bitrate_mbps": "0",  # 0 = disabled; skip files below this bitrate (Mbps)
    "max_bitrate_mbps": "0",  # 0 = disabled; only convert files above this bitrate (Mbps)
    "min_file_size_mb": "0",  # 0 = disabled; skip files smaller than this (MB)
    # Smart encoding
    "content_type_detection": "true",
    "vmaf_analysis_enabled": "true",
    # Minimum VMAF score (0-100) required to accept an encode. Scores below
    # this threshold will revert the job: the encoded temp file is deleted,
    # the original is left in place, and the job completes with a rejection
    # notice. 0 = disabled (never reject). Only applied when vmaf_analysis_enabled
    # is true AND an encode produced a valid VMAF score.
    "vmaf_min_score": "0",
    "resolution_aware_cq": "false",
    "resolution_cq_4k": "24",
    "resolution_cq_1080p": "20",
    "resolution_cq_720p": "18",
    "resolution_cq_sd": "16",
    # Filename
    "filename_suffix": "",  # e.g. "-Shrinkerr" — appended to filename after conversion
    # Post-conversion
    "trash_original_after_conversion": "false",
    "backup_original_days": "0",  # 0 = disabled; keep original in .shrinkerr_backup for X days
    "backup_folder": "",  # Empty = .shrinkerr_backup in same dir as file; set a path for centralized backups
    # Advanced
    "custom_ffmpeg_flags": "",  # Extra flags appended to ffmpeg command
    "max_plex_api_calls": "0",  # 0 = unlimited; max concurrent Plex API calls
    # Authentication
    "api_key": "",  # Empty = no auth required
    "auth_enabled": "false",
    "auth_username": "",
    "auth_password_hash": "",
    "session_secret": "",
    # File age
    "skip_files_newer_enabled": "false",
    "skip_files_newer_than_minutes": "10",
    # Health checks — values: "off" | "quick" | "thorough"
    "health_check_on_scan": "off",
    "health_check_after_conversion": "off",
    # Sonarr / Radarr
    "sonarr_url": "",
    "sonarr_api_key": "",
    "sonarr_path_mapping": "",
    "radarr_url": "",
    "radarr_api_key": "",
    "radarr_path_mapping": "",
    # Quiet hours
    "quiet_hours_enabled": "false",
    "quiet_hours_start": "22",
    "quiet_hours_end": "8",
    "quiet_hours_parallel": "1",
    "quiet_hours_nice": "true",
    # Notifications
    "discord_webhook_url": "",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_pass": "",
    "smtp_from": "",
    "email_to": "",
    "webhook_url": "",
    "notify_queue_complete": "false",
    "notify_job_failed": "false",
    "notify_disk_low": "false",
    "disk_space_threshold_gb": "50",
    # NZBGet integration
    "nzbget_enabled": "false",
    "nzbget_tags": '[]',
    "nzbget_categories": '[]',
    "nzbget_path_mappings": '[]',
    "nzbget_priority": "High",
    "nzbget_wait_for_completion": "true",
    "nzbget_check_sonarr_tags": "true",
    "nzbget_check_radarr_tags": "true",
    # Post-conversion script
    "post_conversion_script": "",
    "post_conversion_script_timeout": "300",
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


_DISALLOWED_MEDIA_DIR_PREFIXES = (
    "/etc", "/root", "/proc", "/sys", "/boot", "/dev", "/app/data",
)


def _validate_filesystem_path(
    raw_path: str,
    *,
    label: str,
    must_exist: bool = True,
    forbid_system_dirs: bool = True,
) -> str:
    """Normalise and validate a filesystem path from settings input.

    Returns the resolved (symlink-followed, absolute) path. Raises
    HTTPException(400) with a descriptive message on any violation.

    Every path-bearing setting (media directories, backup folder, etc.)
    funnels through this so the same rules apply everywhere: must be
    absolute, must resolve to an existing directory (when asked), must
    not land inside a privileged filesystem root. Previously the only
    check was "is a string" — an attacker could post `/` as a media dir
    and bypass every downstream containment guard.
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise HTTPException(status_code=400, detail=f"{label} must be a non-empty string")
    raw_path = raw_path.strip()
    p = Path(raw_path)
    if not p.is_absolute():
        raise HTTPException(status_code=400, detail=f"{label} must be an absolute path")
    try:
        resolved = p.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=f"{label} cannot be resolved: {exc}")
    if must_exist:
        if not resolved.exists():
            raise HTTPException(status_code=400, detail=f"{label} does not exist: {resolved}")
        if not resolved.is_dir():
            raise HTTPException(status_code=400, detail=f"{label} is not a directory: {resolved}")
    if forbid_system_dirs:
        resolved_str = str(resolved)
        if resolved_str == "/":
            raise HTTPException(status_code=400, detail=f"{label} cannot be the filesystem root")
        for forbidden in _DISALLOWED_MEDIA_DIR_PREFIXES:
            if resolved_str == forbidden or resolved_str.startswith(forbidden + "/"):
                raise HTTPException(
                    status_code=400,
                    detail=f"{label} is not allowed under {forbidden} (system directory)",
                )
    return str(resolved)


@router.post("/dirs")
async def add_media_dir(media_dir: MediaDir):
    # Validate the path before persisting — without this every downstream
    # containment check (is-this-file-inside-a-configured-media-dir) was
    # trivially defeatable by POSTing `{"path": "/"}` and then referencing
    # any path on the host.
    resolved_path = _validate_filesystem_path(media_dir.path, label="Media directory")
    db = await aiosqlite.connect(DB_PATH)
    try:
        try:
            async with db.execute(
                "INSERT INTO media_dirs (path, label, enabled) VALUES (?, ?, ?)",
                (resolved_path, media_dir.label, 1 if media_dir.enabled else 0),
            ) as cur:
                new_id = cur.lastrowid
            await db.commit()
        except aiosqlite.IntegrityError:
            raise HTTPException(status_code=409, detail="Directory already exists")
    finally:
        await db.close()
    return {"id": new_id, "path": resolved_path, "label": media_dir.label, "enabled": media_dir.enabled}


@router.delete("/dirs/{dir_id}")
async def delete_media_dir(dir_id: int):
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        # Get the path before deleting so we can clean up scan results
        async with db.execute("SELECT path FROM media_dirs WHERE id = ?", (dir_id,)) as cur:
            row = await cur.fetchone()
        path = row["path"] if row else None

        await db.execute("DELETE FROM media_dirs WHERE id = ?", (dir_id,))

        # Also remove scan results for this directory
        if path:
            await db.execute(
                "DELETE FROM scan_results WHERE file_path LIKE ?",
                (path.rstrip("/") + "/%",),
            )

        await db.commit()
    finally:
        await db.close()
    return {"status": "deleted", "id": dir_id}


@router.get("/api-key")
async def get_api_key():
    """Return the full (unmasked) Shrinkerr API key.

    Exposed separately from the main settings GET so the bulk response can
    mask it like every other secret (defense-in-depth against session
    hijack / XSS / cached responses). The admin still needs to copy the
    key out for NZBGet / SABnzbd / worker setup, so this endpoint returns
    it in the clear — reachable only with valid auth via the same
    middleware that gates the rest of /api/settings/*.
    """
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT value FROM settings WHERE key = 'api_key'"
        ) as cur:
            row = await cur.fetchone()
        return {"api_key": (row["value"] if row else "") or ""}
    finally:
        await db.close()


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
        "nvenc_preset": merged.get("nvenc_preset", "p6"),
        "libx265_preset": merged.get("libx265_preset", "medium"),
        "nvenc_cpu_fallback_preset": merged.get("nvenc_cpu_fallback_preset", ""),
        "nvenc_cpu_fallback_crf": merged.get("nvenc_cpu_fallback_crf", ""),
        "libx265_gpu_fallback_preset": merged.get("libx265_gpu_fallback_preset", ""),
        "libx265_gpu_fallback_cq": merged.get("libx265_gpu_fallback_cq", ""),
        "parallel_jobs": int(merged.get("parallel_jobs", 1)),
        "ffmpeg_timeout": int(merged.get("ffmpeg_timeout", 21600)),
        "ffprobe_timeout": int(merged.get("ffprobe_timeout", 30)),
        "audio_cleanup_enabled": merged.get("audio_cleanup_enabled", "true").lower() == "true",
        "ignore_unknown_tracks": merged.get("ignore_unknown_tracks", "true").lower() == "true",
        "keep_native_language": merged.get("keep_native_language", "true").lower() == "true",
        "target_codec": merged.get("target_codec", "hevc"),
        "target_resolution": merged.get("target_resolution", "copy"),
        "audio_codec": merged.get("audio_codec", "copy"),
        "audio_bitrate": int(merged.get("audio_bitrate", 128)),
        "audio_downmix": merged.get("audio_downmix", "false").lower() == "true",
        "auto_queue_new": merged.get("auto_queue_new", "false").lower() == "true",
        "auto_convert_lossless": merged.get("auto_convert_lossless", "false").lower() == "true",
        "lossless_target_codec": merged.get("lossless_target_codec", "eac3"),
        "lossless_target_bitrate": int(merged.get("lossless_target_bitrate", 640)),
        "plex_ignore_labels": merged.get("plex_ignore_labels", ""),
        "plex_scan_after_conversion": merged.get("plex_scan_after_conversion", "true").lower() == "true",
        "plex_empty_trash_after_scan": merged.get("plex_empty_trash_after_scan", "false").lower() == "true",
    }
    try:
        result["always_keep_languages"] = json.loads(
            merged.get("always_keep_languages", '[]')
        )
    except (json.JSONDecodeError, ValueError):
        result["always_keep_languages"] = []
    try:
        result["source_codecs"] = json.loads(
            merged.get("source_codecs", '["h264"]')
        )
    except (json.JSONDecodeError, ValueError):
        result["source_codecs"] = ["h264"]
    try:
        result["sub_keep_languages"] = json.loads(
            merged.get("sub_keep_languages", '[]')
        )
    except (json.JSONDecodeError, ValueError):
        result["sub_keep_languages"] = []
    result["sub_cleanup_enabled"] = merged.get("sub_cleanup_enabled", "true").lower() == "true"
    result["sub_keep_unknown"] = merged.get("sub_keep_unknown", "true").lower() == "true"

    # Mask API keys — show only last 4 chars if set
    tmdb_key = merged.get("tmdb_api_key", "")
    plex_token = merged.get("plex_token", "")
    # `tmdb_key_source` lets the UI say "using bundled key" vs "using your
    # key" so the user understands why TMDB works even when the field is
    # blank. `tmdb_configured` now accounts for the bundled fallback too.
    from backend.metadata import _env_tmdb_key  # avoid circular import at module load
    _env_key = _env_tmdb_key()
    result["tmdb_api_key"] = ("****" + tmdb_key[-4:]) if tmdb_key else ""
    result["tmdb_configured"] = bool(tmdb_key or _env_key)
    result["tmdb_key_source"] = "user" if tmdb_key else ("bundled" if _env_key else "none")
    result["plex_url"] = merged.get("plex_url", "")
    result["plex_token"] = ("****" + plex_token[-4:]) if plex_token else ""
    result["plex_configured"] = bool(plex_token and merged.get("plex_url", ""))
    result["plex_path_mapping"] = merged.get("plex_path_mapping", "")

    # Jellyfin
    jellyfin_key = merged.get("jellyfin_api_key", "")
    result["jellyfin_url"] = merged.get("jellyfin_url", "")
    result["jellyfin_api_key"] = ("****" + jellyfin_key[-4:]) if jellyfin_key else ""
    result["jellyfin_user_id"] = merged.get("jellyfin_user_id", "")
    result["jellyfin_configured"] = bool(jellyfin_key and merged.get("jellyfin_url", ""))
    result["jellyfin_path_mapping"] = merged.get("jellyfin_path_mapping", "")

    # Conversion filters
    result["min_bitrate_mbps"] = int(merged.get("min_bitrate_mbps", "0"))
    result["max_bitrate_mbps"] = int(merged.get("max_bitrate_mbps", "0"))
    result["min_file_size_mb"] = int(merged.get("min_file_size_mb", "0"))

    # Post-conversion
    result["trash_original_after_conversion"] = merged.get("trash_original_after_conversion", "false").lower() == "true"
    result["backup_original_days"] = int(merged.get("backup_original_days", "0"))
    result["backup_folder"] = merged.get("backup_folder", "")
    result["filename_suffix"] = merged.get("filename_suffix", "")
    result["vmaf_analysis_enabled"] = merged.get("vmaf_analysis_enabled", "true").lower() == "true"
    try:
        result["vmaf_min_score"] = float(merged.get("vmaf_min_score", "0") or "0")
    except (TypeError, ValueError):
        result["vmaf_min_score"] = 0.0

    # Advanced
    result["custom_ffmpeg_flags"] = merged.get("custom_ffmpeg_flags", "")
    result["max_plex_api_calls"] = int(merged.get("max_plex_api_calls", "0"))

    # Plex prioritization & streaming
    result["plex_prioritize_unwatched"] = merged.get("plex_prioritize_unwatched", "false").lower() == "true"
    result["plex_pause_on_stream"] = merged.get("plex_pause_on_stream", "false").lower() == "true"
    result["plex_pause_stream_threshold"] = int(merged.get("plex_pause_stream_threshold", "1"))
    result["plex_pause_transcode_only"] = merged.get("plex_pause_transcode_only", "true").lower() == "true"

    # Authentication — mask the api_key the same way we mask TMDB / Plex /
    # Sonarr / Radarr keys. Previously this endpoint returned the key in
    # full, which meant any authenticated session could exfiltrate the
    # raw credential used by workers + integration scripts.
    api_key_val = merged.get("api_key", "")
    result["api_key"] = ("****" + api_key_val[-4:]) if api_key_val else ""
    result["api_key_configured"] = bool(api_key_val)
    result["auth_enabled"] = merged.get("auth_enabled", "false").lower() == "true"
    result["auth_username"] = merged.get("auth_username", "")
    # Never expose password hash or session secret

    # File age
    result["skip_files_newer_enabled"] = merged.get("skip_files_newer_enabled", "false").lower() == "true"
    result["skip_files_newer_than_minutes"] = int(merged.get("skip_files_newer_than_minutes", "10"))
    # Normalize legacy boolean values ("true" -> "quick", "false" -> "off")
    def _hc_mode(raw: str) -> str:
        v = (raw or "off").lower()
        if v == "true":
            return "quick"
        if v == "false":
            return "off"
        if v in ("off", "quick", "thorough"):
            return v
        return "off"
    result["health_check_on_scan"] = _hc_mode(merged.get("health_check_on_scan", "off"))
    result["health_check_after_conversion"] = _hc_mode(merged.get("health_check_after_conversion", "off"))

    # Sonarr / Radarr
    sonarr_key = merged.get("sonarr_api_key", "")
    radarr_key = merged.get("radarr_api_key", "")
    result["sonarr_url"] = merged.get("sonarr_url", "")
    result["sonarr_api_key"] = ("****" + sonarr_key[-4:]) if sonarr_key else ""
    result["sonarr_configured"] = bool(sonarr_key and merged.get("sonarr_url", ""))
    result["sonarr_path_mapping"] = merged.get("sonarr_path_mapping", "")
    result["radarr_url"] = merged.get("radarr_url", "")
    result["radarr_api_key"] = ("****" + radarr_key[-4:]) if radarr_key else ""
    result["radarr_configured"] = bool(radarr_key and merged.get("radarr_url", ""))
    result["radarr_path_mapping"] = merged.get("radarr_path_mapping", "")

    # NZBGet integration
    result["nzbget_enabled"] = merged.get("nzbget_enabled", "false").lower() == "true"
    result["nzbget_tags"] = json.loads(merged.get("nzbget_tags", '[]'))
    result["nzbget_categories"] = json.loads(merged.get("nzbget_categories", '[]'))
    result["nzbget_path_mappings"] = json.loads(merged.get("nzbget_path_mappings", '[]'))
    result["nzbget_priority"] = merged.get("nzbget_priority", "High")
    result["nzbget_wait_for_completion"] = merged.get("nzbget_wait_for_completion", "true").lower() == "true"
    result["nzbget_check_sonarr_tags"] = merged.get("nzbget_check_sonarr_tags", "true").lower() == "true"
    result["nzbget_check_radarr_tags"] = merged.get("nzbget_check_radarr_tags", "true").lower() == "true"

    # Post-conversion script
    result["post_conversion_script"] = merged.get("post_conversion_script", "")
    result["post_conversion_script_timeout"] = int(merged.get("post_conversion_script_timeout", 300))

    # Quiet hours
    result["quiet_hours_enabled"] = merged.get("quiet_hours_enabled", "false").lower() == "true"
    result["quiet_hours_start"] = int(merged.get("quiet_hours_start", "22"))
    result["quiet_hours_end"] = int(merged.get("quiet_hours_end", "8"))
    result["quiet_hours_parallel"] = int(merged.get("quiet_hours_parallel", "1"))
    result["quiet_hours_nice"] = merged.get("quiet_hours_nice", "true").lower() == "true"

    # Notification settings — mask secrets
    for key in ["discord_webhook_url", "telegram_bot_token", "telegram_chat_id",
                 "smtp_host", "smtp_port", "smtp_user", "smtp_from", "email_to",
                 "webhook_url", "notify_queue_complete", "notify_job_failed",
                 "notify_disk_low", "disk_space_threshold_gb"]:
        result[key] = merged.get(key, "")
    smtp_pass = merged.get("smtp_pass", "")
    result["smtp_pass"] = ("****" + smtp_pass[-4:]) if smtp_pass else ""
    # Parse booleans for frontend
    for key in ["notify_queue_complete", "notify_job_failed", "notify_disk_low"]:
        result[key] = result.get(key, "false").lower() == "true"

    return result


@router.get("/ignored")
async def list_ignored_files():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT file_path FROM ignored_files") as cur:
            rows = await cur.fetchall()
            return [row["file_path"] for row in rows]
    finally:
        await db.close()


class IgnoreFileRequest(BaseModel):
    file_path: str
    reason: str = "manual"


@router.post("/ignored")
async def ignore_file(req: IgnoreFileRequest):
    from datetime import datetime, timezone
    db = await aiosqlite.connect(DB_PATH)
    try:
        await db.execute(
            "INSERT OR REPLACE INTO ignored_files (file_path, reason, ignored_at) VALUES (?, ?, ?)",
            (req.file_path, req.reason, datetime.now(timezone.utc).isoformat()),
        )
        # Remove pending jobs for ignored files
        if req.file_path.endswith("/"):
            # Folder ignore — remove all pending jobs under this folder
            await db.execute(
                "DELETE FROM jobs WHERE status = 'pending' AND file_path LIKE ?",
                (req.file_path + "%",),
            )
        else:
            await db.execute(
                "DELETE FROM jobs WHERE status = 'pending' AND file_path = ?",
                (req.file_path,),
            )
        await db.commit()
    finally:
        await db.close()
    try:
        from backend.file_events import log_event, EVENT_IGNORED
        await log_event(req.file_path, EVENT_IGNORED, f"Ignored ({req.reason})", {"reason": req.reason})
    except Exception:
        pass
    return {"status": "ignored"}


@router.delete("/ignored/{file_path:path}")
async def unignore_file(file_path: str):
    from datetime import datetime, timezone
    db = await aiosqlite.connect(DB_PATH)
    try:
        # Remove the exact file entry
        await db.execute("DELETE FROM ignored_files WHERE file_path = ?", (file_path,))

        # Add a rule_exempt entry so rule-based skip prefixes don't re-ignore this path
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT OR REPLACE INTO ignored_files (file_path, reason, ignored_at) VALUES (?, 'rule_exempt', ?)",
            (file_path, now),
        )

        # Handle folder-level ignores that cover this file
        async with db.execute(
            "SELECT file_path, reason FROM ignored_files WHERE file_path LIKE '%/'"
        ) as cur:
            rows = await cur.fetchall()
            for row in rows:
                folder = row[0]
                reason = row[1] or ""
                if file_path.startswith(folder):
                    if reason == "plex_label":
                        # Mark as exempt so Plex sync doesn't re-add it
                        await db.execute(
                            "UPDATE ignored_files SET reason = 'plex_label_exempt' WHERE file_path = ?",
                            (folder,),
                        )
                    else:
                        await db.execute("DELETE FROM ignored_files WHERE file_path = ?", (folder,))
        await db.commit()
    finally:
        await db.close()
    try:
        from backend.file_events import log_event, EVENT_UNIGNORED
        await log_event(file_path, EVENT_UNIGNORED, "Unignored")
    except Exception:
        pass
    return {"status": "unignored"}


@router.delete("/ignored")
async def clear_ignored():
    db = await aiosqlite.connect(DB_PATH)
    try:
        await db.execute("DELETE FROM ignored_files")
        await db.commit()
    finally:
        await db.close()
    return {"status": "cleared"}


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
        if update.nvenc_preset is not None:
            updates["nvenc_preset"] = update.nvenc_preset
        if update.libx265_preset is not None:
            updates["libx265_preset"] = update.libx265_preset
        if update.nvenc_cpu_fallback_preset is not None:
            # Empty string = "unset", worker falls back to NVENC→libx265 translation
            updates["nvenc_cpu_fallback_preset"] = update.nvenc_cpu_fallback_preset.strip()
        if update.nvenc_cpu_fallback_crf is not None:
            raw = update.nvenc_cpu_fallback_crf
            if isinstance(raw, str) and not raw.strip():
                updates["nvenc_cpu_fallback_crf"] = ""
            else:
                try:
                    updates["nvenc_cpu_fallback_crf"] = str(int(raw))
                except (TypeError, ValueError):
                    updates["nvenc_cpu_fallback_crf"] = ""
        if update.libx265_gpu_fallback_preset is not None:
            updates["libx265_gpu_fallback_preset"] = update.libx265_gpu_fallback_preset.strip()
        if update.libx265_gpu_fallback_cq is not None:
            raw = update.libx265_gpu_fallback_cq
            if isinstance(raw, str) and not raw.strip():
                updates["libx265_gpu_fallback_cq"] = ""
            else:
                try:
                    updates["libx265_gpu_fallback_cq"] = str(int(raw))
                except (TypeError, ValueError):
                    updates["libx265_gpu_fallback_cq"] = ""
        if update.parallel_jobs is not None:
            updates["parallel_jobs"] = str(max(1, min(16, update.parallel_jobs)))
        if update.ffmpeg_timeout is not None:
            updates["ffmpeg_timeout"] = str(update.ffmpeg_timeout)
        if update.ffprobe_timeout is not None:
            updates["ffprobe_timeout"] = str(update.ffprobe_timeout)
        if update.audio_cleanup_enabled is not None:
            updates["audio_cleanup_enabled"] = "true" if update.audio_cleanup_enabled else "false"
        if update.ignore_unknown_tracks is not None:
            updates["ignore_unknown_tracks"] = "true" if update.ignore_unknown_tracks else "false"
        if update.keep_native_language is not None:
            updates["keep_native_language"] = "true" if update.keep_native_language else "false"
        if update.target_codec is not None:
            updates["target_codec"] = update.target_codec
        if update.target_resolution is not None:
            updates["target_resolution"] = update.target_resolution
        if update.source_codecs is not None:
            updates["source_codecs"] = json.dumps(update.source_codecs)
        if update.sub_cleanup_enabled is not None:
            updates["sub_cleanup_enabled"] = "true" if update.sub_cleanup_enabled else "false"
        if update.sub_keep_languages is not None:
            updates["sub_keep_languages"] = json.dumps(update.sub_keep_languages)
        if update.sub_keep_unknown is not None:
            updates["sub_keep_unknown"] = "true" if update.sub_keep_unknown else "false"
        if update.audio_codec is not None:
            updates["audio_codec"] = update.audio_codec
        if update.audio_bitrate is not None:
            updates["audio_bitrate"] = str(update.audio_bitrate)
        if update.audio_downmix is not None:
            updates["audio_downmix"] = "true" if update.audio_downmix else "false"
        if update.auto_queue_new is not None:
            updates["auto_queue_new"] = "true" if update.auto_queue_new else "false"
        if update.auto_convert_lossless is not None:
            updates["auto_convert_lossless"] = "true" if update.auto_convert_lossless else "false"
        if update.lossless_target_codec is not None:
            updates["lossless_target_codec"] = update.lossless_target_codec
        if update.lossless_target_bitrate is not None:
            updates["lossless_target_bitrate"] = str(update.lossless_target_bitrate)
        if update.tmdb_api_key is not None and not update.tmdb_api_key.startswith("****"):
            updates["tmdb_api_key"] = update.tmdb_api_key
        if update.plex_url is not None:
            from backend.ssrf_guard import validate_outbound_url
            updates["plex_url"] = validate_outbound_url(
                update.plex_url.rstrip("/"), label="Plex URL",
            )
        if update.plex_token is not None and not update.plex_token.startswith("****"):
            updates["plex_token"] = update.plex_token
        if update.plex_path_mapping is not None:
            updates["plex_path_mapping"] = update.plex_path_mapping
        if update.plex_ignore_labels is not None:
            updates["plex_ignore_labels"] = update.plex_ignore_labels
        if update.plex_prioritize_unwatched is not None:
            updates["plex_prioritize_unwatched"] = "true" if update.plex_prioritize_unwatched else "false"
        if update.plex_pause_on_stream is not None:
            updates["plex_pause_on_stream"] = "true" if update.plex_pause_on_stream else "false"
        if update.plex_pause_stream_threshold is not None:
            updates["plex_pause_stream_threshold"] = str(update.plex_pause_stream_threshold)
        if update.plex_pause_transcode_only is not None:
            updates["plex_pause_transcode_only"] = "true" if update.plex_pause_transcode_only else "false"
        if update.plex_empty_trash_after_scan is not None:
            updates["plex_empty_trash_after_scan"] = "true" if update.plex_empty_trash_after_scan else "false"
        # Conversion filters
        for key in ["min_bitrate_mbps", "max_bitrate_mbps", "min_file_size_mb", "backup_original_days", "max_plex_api_calls"]:
            val = getattr(update, key, None)
            if val is not None:
                updates[key] = str(val)
        if update.custom_ffmpeg_flags is not None:
            updates["custom_ffmpeg_flags"] = update.custom_ffmpeg_flags
        if update.backup_folder is not None:
            # Empty string is valid (means "sprinkle .shrinkerr_backup next to
            # each file"); any non-empty value must be an absolute, existing,
            # non-system directory. Without this an attacker with settings
            # write could coerce the conversion pipeline into renaming
            # originals into /etc/cron.d/ etc.
            raw = update.backup_folder.strip()
            if raw:
                updates["backup_folder"] = _validate_filesystem_path(
                    raw, label="Backup folder"
                )
            else:
                updates["backup_folder"] = ""
        if update.filename_suffix is not None:
            updates["filename_suffix"] = update.filename_suffix
        if update.vmaf_analysis_enabled is not None:
            updates["vmaf_analysis_enabled"] = "true" if update.vmaf_analysis_enabled else "false"
        if update.vmaf_min_score is not None:
            # Clamp to 0..100 and store as plain string. 0 == disabled (never
            # rejects). The settings GET endpoint parses back to float.
            try:
                _vms = max(0.0, min(100.0, float(update.vmaf_min_score)))
            except (TypeError, ValueError):
                _vms = 0.0
            updates["vmaf_min_score"] = str(_vms)
        if update.api_key is not None and not update.api_key.startswith("****"):
            updates["api_key"] = update.api_key
        # Auth settings
        if update.auth_enabled is not None:
            updates["auth_enabled"] = "true" if update.auth_enabled else "false"
        if update.auth_username is not None:
            updates["auth_username"] = update.auth_username
        if update.auth_password is not None and update.auth_password:
            # New passwords are hashed with bcrypt (cost 12). Legacy
            # SHA-256 hashes already in the DB keep working via the
            # compat path in backend/main.py::_verify_password and get
            # transparently upgraded on next successful login.
            from backend.main import _hash_password
            updates["auth_password_hash"] = _hash_password(update.auth_password)
        # Generate session_secret if not yet set
        async with db.execute("SELECT value FROM settings WHERE key = 'session_secret'") as cur:
            row = await cur.fetchone()
            if not row or not row[0]:
                import secrets
                updates["session_secret"] = secrets.token_hex(32)
        # Post-conversion
        if update.trash_original_after_conversion is not None:
            updates["trash_original_after_conversion"] = "true" if update.trash_original_after_conversion else "false"
        # File age
        if update.skip_files_newer_enabled is not None:
            updates["skip_files_newer_enabled"] = "true" if update.skip_files_newer_enabled else "false"
        if update.skip_files_newer_than_minutes is not None:
            updates["skip_files_newer_than_minutes"] = str(update.skip_files_newer_than_minutes)
        if update.health_check_on_scan is not None:
            v = str(update.health_check_on_scan).lower()
            if v in ("true", "1"): v = "quick"
            elif v in ("false", "0"): v = "off"
            if v not in ("off", "quick", "thorough"): v = "off"
            updates["health_check_on_scan"] = v
        if update.health_check_after_conversion is not None:
            v = str(update.health_check_after_conversion).lower()
            if v in ("true", "1"): v = "quick"
            elif v in ("false", "0"): v = "off"
            if v not in ("off", "quick", "thorough"): v = "off"
            updates["health_check_after_conversion"] = v
        # Sonarr / Radarr
        if update.sonarr_url is not None:
            from backend.ssrf_guard import validate_outbound_url
            updates["sonarr_url"] = validate_outbound_url(
                update.sonarr_url.rstrip("/"), label="Sonarr URL",
            )
        if update.sonarr_api_key is not None and not update.sonarr_api_key.startswith("****"):
            updates["sonarr_api_key"] = update.sonarr_api_key
        if update.sonarr_path_mapping is not None:
            updates["sonarr_path_mapping"] = update.sonarr_path_mapping
        if update.radarr_url is not None:
            from backend.ssrf_guard import validate_outbound_url
            updates["radarr_url"] = validate_outbound_url(
                update.radarr_url.rstrip("/"), label="Radarr URL",
            )
        if update.radarr_api_key is not None and not update.radarr_api_key.startswith("****"):
            updates["radarr_api_key"] = update.radarr_api_key
        if update.radarr_path_mapping is not None:
            updates["radarr_path_mapping"] = update.radarr_path_mapping
        # NZBGet integration
        if update.nzbget_enabled is not None:
            updates["nzbget_enabled"] = "true" if update.nzbget_enabled else "false"
        if update.nzbget_tags is not None:
            updates["nzbget_tags"] = json.dumps(update.nzbget_tags)
        if update.nzbget_categories is not None:
            updates["nzbget_categories"] = json.dumps(update.nzbget_categories)
        if update.nzbget_path_mappings is not None:
            updates["nzbget_path_mappings"] = json.dumps(update.nzbget_path_mappings)
        if update.nzbget_priority is not None:
            updates["nzbget_priority"] = update.nzbget_priority
        if update.nzbget_wait_for_completion is not None:
            updates["nzbget_wait_for_completion"] = "true" if update.nzbget_wait_for_completion else "false"
        if update.nzbget_check_sonarr_tags is not None:
            updates["nzbget_check_sonarr_tags"] = "true" if update.nzbget_check_sonarr_tags else "false"
        if update.nzbget_check_radarr_tags is not None:
            updates["nzbget_check_radarr_tags"] = "true" if update.nzbget_check_radarr_tags else "false"
        # Post-conversion script
        if update.post_conversion_script is not None:
            # Arbitrary-binary-execution vector: ffmpeg-worker runs this
            # command after every successful encode. Changing it requires
            # the UI-level password login so a leaked API key alone can't
            # flip an installation into an RCE posture. Setting it empty
            # is always allowed (disables the feature).
            new_script = update.post_conversion_script
            if new_script:
                async with db.execute(
                    "SELECT value FROM settings WHERE key = 'auth_enabled'"
                ) as cur:
                    row = await cur.fetchone()
                auth_enabled = bool(row) and (row[0] == "true")
                if not auth_enabled:
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            "post_conversion_script runs arbitrary commands after every job. "
                            "Enable password auth (Settings → System → Authentication) before "
                            "configuring this setting so it can't be changed with just an API key."
                        ),
                    )
            updates["post_conversion_script"] = new_script
        if update.post_conversion_script_timeout is not None:
            updates["post_conversion_script_timeout"] = str(update.post_conversion_script_timeout)
        # Quiet hours
        for key in ["quiet_hours_start", "quiet_hours_end", "quiet_hours_parallel"]:
            val = getattr(update, key, None)
            if val is not None:
                updates[key] = str(val)
        for key in ["quiet_hours_enabled", "quiet_hours_nice", "notify_queue_complete", "notify_job_failed", "notify_disk_low"]:
            val = getattr(update, key, None)
            if val is not None:
                updates[key] = "true" if val else "false"
        # Notifications — string fields. URL fields go through SSRF
        # validation so pointing a webhook at 169.254.169.254 / ::ffff:
        # cloud-metadata endpoints is rejected at save time.
        from backend.ssrf_guard import validate_outbound_url
        _url_notify_keys = {"discord_webhook_url", "webhook_url"}
        for key in ["discord_webhook_url", "telegram_bot_token", "telegram_chat_id",
                     "smtp_host", "smtp_port", "smtp_user", "smtp_from", "email_to", "webhook_url", "disk_space_threshold_gb"]:
            val = getattr(update, key, None)
            if val is not None:
                if key in _url_notify_keys and val:
                    val = validate_outbound_url(val, label=f"{key} URL")
                updates[key] = val
        if update.smtp_pass is not None and not update.smtp_pass.startswith("****"):
            updates["smtp_pass"] = update.smtp_pass

        for key, value in updates.items():
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        await db.commit()
    finally:
        await db.close()

    # Invalidate settings cache if relevant keys changed
    cache_keys = {"sub_keep_languages", "sub_keep_unknown", "sub_cleanup_enabled", "audio_cleanup_enabled"}
    if cache_keys & set(updates.keys()):
        from backend.scanner import invalidate_sub_settings_cache
        invalidate_sub_settings_cache()

    # Invalidate auth cache if auth-related keys changed
    auth_keys = {"auth_enabled", "auth_username", "auth_password_hash", "api_key", "session_secret"}
    if auth_keys & set(updates.keys()):
        from backend.main import _auth_cache
        _auth_cache["checked_at"] = 0

    return {"status": "updated", "keys": list(updates.keys())}


@router.get("/browse")
async def browse_directory(path: str = "/"):
    """List directories at the given path for the media-dir picker.

    Previously this was an unrestricted filesystem enumerator — `?path=/etc`
    returned every config file name, `?path=/proc/self` exposed container
    runtime information. Now we refuse anything under system-only prefixes
    (the same `_DISALLOWED_MEDIA_DIR_PREFIXES` used for media-dir input
    validation) so the picker can't be used as a recon tool.
    """
    from pathlib import Path as P
    try:
        target = P(path).resolve(strict=False)
    except (OSError, RuntimeError):
        return {"path": path, "dirs": [], "error": "Invalid path"}

    target_str = str(target)
    # The picker is meant for choosing a directory the user will register
    # as a media root. Block the usual system-only roots explicitly (same
    # list we reject in /dirs POST) and refuse `/` (lists everything).
    if target_str == "/":
        return {"path": "/", "dirs": [], "error": "Refusing to list filesystem root"}
    for forbidden in _DISALLOWED_MEDIA_DIR_PREFIXES:
        if target_str == forbidden or target_str.startswith(forbidden + "/"):
            return {
                "path": target_str,
                "dirs": [],
                "error": f"{forbidden} is a restricted path",
            }

    if not target.exists() or not target.is_dir():
        return {"path": target_str, "dirs": [], "error": "Directory not found"}
    dirs = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: e.name.lower()):
            if entry.is_dir() and not entry.name.startswith("."):
                dirs.append({"name": entry.name, "path": str(entry)})
    except PermissionError:
        return {"path": target_str, "dirs": [], "error": "Permission denied"}
    parent = str(target.parent) if target_str != "/" else None
    return {"path": target_str, "parent": parent, "dirs": dirs}


# Settings keys that NEVER belong in an exported / backup bundle — they
# are either auth credentials or session-signing material. A leaked
# export file previously handed an attacker:
#   - api_key        (full Shrinkerr API access)
#   - session_secret (forge any session cookie permanently)
#   - auth_password_hash (offline cracking target)
#   - *_api_key / *_token / smtp_pass (every integrated service)
# Exports now omit this allowlist; restore leaves the live values alone.
_SECRET_SETTINGS_KEYS = frozenset({
    "api_key",
    "session_secret",
    "auth_password_hash",
    "tmdb_api_key",
    "plex_token",
    "jellyfin_api_key",
    "sonarr_api_key",
    "radarr_api_key",
    "smtp_pass",
    "discord_webhook_url",  # contains the secret path component
    "telegram_bot_token",
    "webhook_url",          # often signed / secret URL
})


@router.get("/export")
async def export_settings():
    """Export all settings as JSON for backup."""
    from fastapi.responses import StreamingResponse
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        settings = {}
        async with db.execute("SELECT key, value FROM settings") as cur:
            for row in await cur.fetchall():
                if row["key"] in _SECRET_SETTINGS_KEYS:
                    continue
                settings[row["key"]] = row["value"]
        # Also export media dirs
        async with db.execute("SELECT path, label FROM media_dirs") as cur:
            dirs = [{"path": r["path"], "label": r["label"]} for r in await cur.fetchall()]
        # Export encoding rules
        async with db.execute("SELECT * FROM encoding_rules ORDER BY priority ASC") as cur:
            rules = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()

    export_data = {
        "version": "1",
        "settings": settings,
        "media_dirs": dirs,
        "encoding_rules": rules,
    }
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return StreamingResponse(
        iter([json.dumps(export_data, indent=2)]),
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename=shrinkerr-settings-{today}.json"},
    )


class ImportSettingsRequest(BaseModel):
    settings: dict = {}
    media_dirs: list = []
    encoding_rules: list = []


@router.post("/import")
async def import_settings(payload: ImportSettingsRequest):
    """Import settings from a JSON backup. Merges with existing settings."""
    db = await aiosqlite.connect(DB_PATH)
    try:
        imported = 0
        # Import settings
        for key, value in payload.settings.items():
            await db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            imported += 1
        # Import media dirs
        for d in payload.media_dirs:
            await db.execute(
                "INSERT OR IGNORE INTO media_dirs (path, label) VALUES (?, ?)",
                (d.get("path", ""), d.get("label", "")),
            )
        # Import encoding rules
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        for rule in payload.encoding_rules:
            await db.execute(
                """INSERT INTO encoding_rules (name, match_type, match_value, match_conditions,
                   priority, action, enabled, encoder, nvenc_preset, nvenc_cq, libx265_crf,
                   target_resolution, audio_codec, audio_bitrate, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rule.get("name", ""), rule.get("match_type", ""), rule.get("match_value", ""),
                 rule.get("match_conditions"), rule.get("priority", 0), rule.get("action", "encode"),
                 rule.get("enabled", 1), rule.get("encoder"), rule.get("nvenc_preset"),
                 rule.get("nvenc_cq"), rule.get("libx265_crf"), rule.get("target_resolution"),
                 rule.get("audio_codec"), rule.get("audio_bitrate"), now),
            )
        await db.commit()
        return {"status": "imported", "settings_count": imported, "dirs_count": len(payload.media_dirs), "rules_count": len(payload.encoding_rules)}
    finally:
        await db.close()


class TestApiRequest(BaseModel):
    service: str  # "tmdb", "tvdb", or "plex"


@router.post("/test-api")
async def test_api_key(req: TestApiRequest):
    # Read settings from DB
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        settings = {}
        async with db.execute("SELECT key, value FROM settings") as cur:
            rows = await cur.fetchall()
            for r in rows:
                settings[r["key"]] = r["value"]
    finally:
        await db.close()

    try:
        if req.service == "tmdb":
            key = settings.get("tmdb_api_key", "")
            if not key:
                return {"success": False, "error": "No API key configured"}
            from backend.metadata import test_tmdb_key
            ok = await test_tmdb_key(key)
            return {"success": ok, "error": None if ok else "API key validation failed"}

        elif req.service == "plex":
            plex_url = settings.get("plex_url", "")
            plex_token = settings.get("plex_token", "")
            if not plex_url:
                return {"success": False, "error": "No Plex URL configured"}
            if not plex_token:
                return {"success": False, "error": "No Plex token configured"}
            from backend.plex import test_plex_connection
            return await test_plex_connection(plex_url, plex_token)

        elif req.service == "jellyfin":
            from backend.jellyfin import test_jellyfin_connection
            return await test_jellyfin_connection()

        elif req.service == "sonarr":
            sonarr_url = settings.get("sonarr_url", "")
            sonarr_key = settings.get("sonarr_api_key", "")
            if not sonarr_url:
                return {"success": False, "error": "No Sonarr URL configured"}
            if not sonarr_key:
                return {"success": False, "error": "No Sonarr API key configured"}
            from backend.arr import test_sonarr
            return await test_sonarr(sonarr_url, sonarr_key)

        elif req.service == "radarr":
            radarr_url = settings.get("radarr_url", "")
            radarr_key = settings.get("radarr_api_key", "")
            if not radarr_url:
                return {"success": False, "error": "No Radarr URL configured"}
            if not radarr_key:
                return {"success": False, "error": "No Radarr API key configured"}
            from backend.arr import test_radarr
            return await test_radarr(radarr_url, radarr_key)

        else:
            return {"success": False, "error": "Unknown service"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# --- Backup management ---

@router.get("/backups")
async def list_backups():
    """List all backup files with size and age."""
    import os
    from pathlib import Path

    db = await connect_db()
    try:
        # Get configured backup folder
        async with db.execute("SELECT value FROM settings WHERE key = 'backup_folder'") as cur:
            row = await cur.fetchone()
            custom_folder = row["value"] if row else ""

        # Get media dirs to find .shrinkerr_backup (+ legacy .squeezarr_backup) folders
        async with db.execute("SELECT path FROM media_dirs") as cur:
            media_dirs = [r["path"] for r in await cur.fetchall()]
    finally:
        await db.close()

    backups = []
    total_size = 0
    seen_dirs: set[str] = set()

    def scan_backup_dir(backup_dir: str):
        nonlocal total_size
        if backup_dir in seen_dirs or not os.path.isdir(backup_dir):
            return
        seen_dirs.add(backup_dir)
        for entry in os.scandir(backup_dir):
            if entry.is_file():
                try:
                    stat = entry.stat()
                    total_size += stat.st_size
                    backups.append({
                        "path": entry.path,
                        "name": entry.name,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                        "folder": backup_dir,
                    })
                except OSError:
                    pass

    # Scan custom backup folder
    if custom_folder:
        for entry in os.scandir(custom_folder) if os.path.isdir(custom_folder) else []:
            if entry.is_dir():
                scan_backup_dir(entry.path)

    # Scan both the new .shrinkerr_backup and the legacy .squeezarr_backup
    # folders — users upgrading from the old name will still have backups
    # sitting under the old folder until they clean it up.
    _BACKUP_DIRNAMES = {".shrinkerr_backup", ".squeezarr_backup"}
    for media_dir in media_dirs:
        for root, dirs, _files in os.walk(media_dir):
            for backup_name in _BACKUP_DIRNAMES & set(dirs):
                scan_backup_dir(os.path.join(root, backup_name))
            # Don't recurse into backup dirs (either flavor).
            dirs[:] = [d for d in dirs if d not in _BACKUP_DIRNAMES]

    backups.sort(key=lambda b: b["mtime"], reverse=True)
    return {
        "backups": backups,
        "total_size": total_size,
        "total_count": len(backups),
    }


class DeleteBackupsRequest(BaseModel):
    paths: list[str] = []  # Empty = delete all
    older_than_days: int | None = None  # Delete files older than N days


@router.post("/backups/delete")
async def delete_backups(req: DeleteBackupsRequest):
    """Delete backup files. Specify paths for selective delete, or older_than_days for cleanup."""
    import os
    import time

    deleted = 0
    freed = 0

    if req.paths:
        # Delete specific files
        for path in req.paths:
            if not os.path.exists(path):
                continue
            # Safety: only delete from a recognised backup dir (new or legacy)
            # or the configured centralized backup folder
            if ".shrinkerr_backup" not in path and ".squeezarr_backup" not in path:
                db = await connect_db()
                try:
                    async with db.execute("SELECT value FROM settings WHERE key = 'backup_folder'") as cur:
                        row = await cur.fetchone()
                        custom = row["value"] if row else ""
                finally:
                    await db.close()
                if not custom or not path.startswith(custom):
                    continue  # Skip — not a backup path
            try:
                size = os.path.getsize(path)
                os.unlink(path)
                deleted += 1
                freed += size
            except OSError:
                pass
    else:
        # Delete all or by age
        result = await list_backups()
        cutoff = time.time() - (req.older_than_days * 86400) if req.older_than_days else None
        for backup in result["backups"]:
            if cutoff and backup["mtime"] > cutoff:
                continue  # Too new, skip
            try:
                os.unlink(backup["path"])
                deleted += 1
                freed += backup["size"]
            except OSError:
                pass

    # Clean up empty backup directories
    result2 = await list_backups()
    for folder in set(b["folder"] for b in result2["backups"]):
        pass  # folder still has files
    # Find and remove now-empty dirs (OS handles this when unlink leaves
    # the dir empty — no explicit cleanup needed here).

    return {"deleted": deleted, "freed": freed}


@router.get("/nzbget-config")
async def get_nzbget_config(request: Request):
    """Return full NZBGet extension configuration (for runtime script)."""
    db = await connect_db()
    try:
        settings = {}
        async with db.execute("SELECT key, value FROM settings") as cur:
            for row in await cur.fetchall():
                settings[row["key"]] = row["value"]
    finally:
        await db.close()

    return {
        "shrinkerr_url": str(request.base_url).rstrip("/"),
        "shrinkerr_api_key": settings.get("api_key", ""),
        "sonarr_url": settings.get("sonarr_url", ""),
        "sonarr_api_key": settings.get("sonarr_api_key", ""),
        "radarr_url": settings.get("radarr_url", ""),
        "radarr_api_key": settings.get("radarr_api_key", ""),
        "tags": json.loads(settings.get("nzbget_tags", '[]')),
        "categories": json.loads(settings.get("nzbget_categories", '[]')),
        "path_mappings": json.loads(settings.get("nzbget_path_mappings", '[]')),
        "priority": {"Normal": 0, "High": 1, "Highest": 2}.get(settings.get("nzbget_priority", "High"), 1),
        "wait_for_completion": settings.get("nzbget_wait_for_completion", "true").lower() == "true",
        "check_sonarr_tags": settings.get("nzbget_check_sonarr_tags", "true").lower() == "true",
        "check_radarr_tags": settings.get("nzbget_check_radarr_tags", "true").lower() == "true",
    }


@router.get("/nzbget-script")
async def download_nzbget_script(request: Request):
    """Generate and download the NZBGet post-processing script with baked-in Shrinkerr connection."""
    from starlette.responses import Response

    db = await connect_db()
    try:
        settings = {}
        async with db.execute("SELECT key, value FROM settings WHERE key IN ('api_key')") as cur:
            for row in await cur.fetchall():
                settings[row["key"]] = row["value"]
    finally:
        await db.close()

    shrinkerr_url = str(request.base_url).rstrip("/")
    api_key = settings.get("api_key", "")

    # Read the template script. The NZBGet extension lives at
    # `nzbget-extension/Shrinkerr/Shrinkerr.py` and uses `__SHRINKERR_URL__` /
    # `__SHRINKERR_API_KEY__` placeholders (matches the canonical app name).
    import os
    script_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "nzbget-extension", "Shrinkerr", "Shrinkerr.py",
    )
    with open(script_path, "r") as f:
        script_content = f.read()

    # Bake the user's server URL + API key into the downloaded copy so they
    # don't have to edit the script after drop-in.
    script_content = script_content.replace("__SHRINKERR_URL__", shrinkerr_url)
    script_content = script_content.replace("__SHRINKERR_API_KEY__", api_key)

    return Response(
        content=script_content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="Shrinkerr.py"'},
    )


@router.get("/sabnzbd-script")
async def download_sabnzbd_script(request: Request):
    """Generate and download the SABnzbd post-processing script with baked-in connection."""
    from starlette.responses import Response

    db = await connect_db()
    try:
        settings = {}
        async with db.execute("SELECT key, value FROM settings WHERE key IN ('api_key')") as cur:
            for row in await cur.fetchall():
                settings[row["key"]] = row["value"]
    finally:
        await db.close()

    shrinkerr_url = str(request.base_url).rstrip("/")
    api_key = settings.get("api_key", "")

    import os
    script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "sabnzbd-script", "shrinkerr.py")
    with open(script_path, "r") as f:
        script_content = f.read()

    script_content = script_content.replace("__SHRINKERR_URL__", shrinkerr_url)
    script_content = script_content.replace("__SHRINKERR_API_KEY__", api_key)

    return Response(
        content=script_content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="shrinkerr.py"'},
    )


# --- Backup / Restore ---

MAX_BACKUPS = 4  # Keep this many most-recent backups; older ones are pruned
BACKUP_INTERVAL_SECONDS = 7 * 24 * 60 * 60  # Weekly


def _list_backup_files() -> list[Path]:
    """Return all backup zips (both legacy squeezarr_ and new shrinkerr_), newest first."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files = list(BACKUP_DIR.glob("shrinkerr_backup_*.zip")) + list(BACKUP_DIR.glob("squeezarr_backup_*.zip"))
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files


def _prune_backups(keep: int = MAX_BACKUPS) -> int:
    """Delete oldest backups, keeping only the most recent `keep`. Returns number deleted."""
    files = _list_backup_files()
    removed = 0
    for f in files[keep:]:
        try:
            f.unlink()
            removed += 1
        except Exception:
            pass
    return removed


async def _do_create_backup() -> dict:
    """Create a full backup zip. Used by both the manual endpoint and the scheduler."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y.%m.%d_%H.%M.%S")
    zip_name = f"shrinkerr_backup_{ts}.zip"
    zip_path = BACKUP_DIR / zip_name

    # Safe copy of SQLite DB (handles WAL mode correctly)
    tmp_db = BACKUP_DIR / f"_tmp_backup_{ts}.db"
    try:
        db = await aiosqlite.connect(DB_PATH)
        try:
            await db.execute(f"VACUUM INTO '{tmp_db}'")
        finally:
            await db.close()

        # Build settings JSON
        db = await aiosqlite.connect(str(tmp_db))
        db.row_factory = aiosqlite.Row
        try:
            settings = {}
            async with db.execute("SELECT key, value FROM settings") as cur:
                for row in await cur.fetchall():
                    settings[row["key"]] = row["value"]
            async with db.execute("SELECT path, label FROM media_dirs") as cur:
                dirs = [{"path": r["path"], "label": r["label"]} for r in await cur.fetchall()]
            async with db.execute("SELECT * FROM encoding_rules ORDER BY priority ASC") as cur:
                rules = [dict(r) for r in await cur.fetchall()]
        finally:
            await db.close()

        settings_json = json.dumps({
            "version": "1",
            "settings": settings,
            "media_dirs": dirs,
            "encoding_rules": rules,
        }, indent=2)

        # Create zip — include db under both names so old restore code still works
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp_db, "shrinkerr.db")
            zf.writestr("settings.json", settings_json)
    finally:
        if tmp_db.exists():
            tmp_db.unlink()

    # Prune older backups to keep only MAX_BACKUPS
    _prune_backups(MAX_BACKUPS)

    stat = zip_path.stat()
    return {
        "name": zip_name,
        "size": stat.st_size,
        "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
    }


async def scheduled_backup_loop():
    """Background task: create a backup every week (if none in the last week), pruning to MAX_BACKUPS."""
    import asyncio
    while True:
        try:
            files = _list_backup_files()
            now = datetime.now(timezone.utc).timestamp()
            most_recent_age = (now - files[0].stat().st_mtime) if files else float("inf")
            if most_recent_age >= BACKUP_INTERVAL_SECONDS:
                print(f"[backup] Creating scheduled weekly backup", flush=True)
                result = await _do_create_backup()
                print(f"[backup] Wrote {result['name']} ({result['size']} bytes)", flush=True)
        except Exception as exc:
            print(f"[backup] Scheduled backup failed: {exc}", flush=True)
        # Check again in 1 hour — cheap, and catches cases where the container was down past the deadline
        await asyncio.sleep(3600)


@router.post("/backup")
async def create_backup():
    """Create a full backup zip containing the database and settings export."""
    return await _do_create_backup()


@router.get("/backup/list")
async def list_backups():
    """List all backup zip files."""
    backups = []
    for f in _list_backup_files():
        stat = f.stat()
        backups.append({
            "name": f.name,
            "size": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })
    return backups


@router.get("/backup/download/{name}")
async def download_backup(name: str):
    """Download a backup zip file."""
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "Invalid backup name")
    path = BACKUP_DIR / name
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Backup not found")
    return FileResponse(
        path=str(path),
        media_type="application/zip",
        filename=name,
    )


@router.delete("/backup/{name}")
async def delete_backup(name: str):
    """Delete a specific backup file."""
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "Invalid backup name")
    path = BACKUP_DIR / name
    if not path.exists():
        raise HTTPException(404, "Backup not found")
    path.unlink()
    return {"status": "deleted"}


@router.post("/backup/restore")
async def restore_backup(file: UploadFile = File(...)):
    """Restore from a backup zip. Replaces the current database."""
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(400, "Must upload a .zip file")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    tmp_zip = BACKUP_DIR / f"_restore_upload_{datetime.now().strftime('%H%M%S')}.zip"
    tmp_db = BACKUP_DIR / "_restore_tmp.db"

    try:
        # Save uploaded file
        content = await file.read()
        tmp_zip.write_bytes(content)

        # Validate zip contents — accept either shrinkerr.db (new) or squeezarr.db (legacy)
        with zipfile.ZipFile(tmp_zip, "r") as zf:
            names = zf.namelist()
            db_name = "shrinkerr.db" if "shrinkerr.db" in names else ("squeezarr.db" if "squeezarr.db" in names else None)
            if db_name is None:
                raise HTTPException(400, "Backup zip must contain shrinkerr.db or squeezarr.db")
            zf.extract(db_name, BACKUP_DIR)
            extracted = BACKUP_DIR / db_name
            extracted.rename(tmp_db)

        # Validate the extracted DB is a valid SQLite database
        try:
            test_db = await aiosqlite.connect(str(tmp_db))
            try:
                async with test_db.execute("SELECT count(*) FROM settings") as cur:
                    await cur.fetchone()
            finally:
                await test_db.close()
        except Exception as exc:
            raise HTTPException(400, f"Invalid database in backup: {exc}")

        # Create a safety backup of the current DB before replacing
        safety_name = f"shrinkerr_pre_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        safety_path = BACKUP_DIR / safety_name
        shutil.copy2(DB_PATH, str(safety_path))

        # Replace the database
        shutil.move(str(tmp_db), DB_PATH)

        return {"status": "restored", "message": "Database restored. Restart the container for changes to take full effect."}
    finally:
        if tmp_zip.exists():
            tmp_zip.unlink()
        if tmp_db.exists():
            tmp_db.unlink()
