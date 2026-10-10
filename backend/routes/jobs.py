import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from backend.api_errors import ApiError
from pydantic import BaseModel

from backend.database import connect_db, prefix_clause
from backend.queue import JobQueue, QueueWorker

router = APIRouter(prefix="/api/jobs")

_worker: Optional[QueueWorker] = None
_queue: Optional[JobQueue] = None


def init_job_routes(worker: QueueWorker, queue: JobQueue) -> None:
    global _worker, _queue
    _worker = worker
    _queue = queue


class BulkJobCreate(BaseModel):
    jobs: list[dict]


class ReorderRequest(BaseModel):
    job_ids: list[int]


class BulkUpdateSettingsRequest(BaseModel):
    job_ids: list[int]
    nvenc_preset: Optional[str] = None
    nvenc_cq: Optional[int] = None
    libx265_preset: Optional[str] = None
    libx265_crf: Optional[int] = None
    audio_codec: Optional[str] = None
    audio_bitrate: Optional[int] = None
    priority: Optional[int] = None


class BulkMoveRequest(BaseModel):
    job_ids: list[int]
    position: str  # "top", "bottom", "up", "down"


class BulkIgnoreRequest(BaseModel):
    job_ids: list[int]


@router.post("/add")
async def add_job(
    file_path: str,
    job_type: str,
    encoder: Optional[str] = None,
    audio_tracks_to_remove: Optional[list[int]] = None,
):
    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")
    job_id = await _queue.add_job(
        file_path=file_path,
        job_type=job_type,
        encoder=encoder,
        audio_tracks_to_remove=audio_tracks_to_remove or [],
    )
    return {"job_id": job_id}


@router.post("/add-bulk")
async def add_bulk_jobs(payload: BulkJobCreate):
    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")
    job_ids = []
    for job_data in payload.jobs:
        job_id = await _queue.add_job(
            file_path=job_data["file_path"],
            job_type=job_data["job_type"],
            encoder=job_data.get("encoder"),
            audio_tracks_to_remove=job_data.get("audio_tracks_to_remove", []),
            subtitle_tracks_to_remove=job_data.get("subtitle_tracks_to_remove", []),
            original_size=job_data.get("original_size"),
            nvenc_preset=job_data.get("nvenc_preset"),
            nvenc_cq=job_data.get("nvenc_cq"),
            audio_codec=job_data.get("audio_codec"),
            audio_bitrate=job_data.get("audio_bitrate"),
        )
        job_ids.append(job_id)
    return {"job_ids": job_ids}


class BulkQueueFromScanRequest(BaseModel):
    file_paths: list[str] = []
    priority: int = 0  # 0=Normal, 1=High, 2=Highest
    override_rules: bool = False  # When True, ignore encoding rules
    select_all: bool = False  # When True, resolve matching files server-side
    filter: str = "all"  # Filter to apply when select_all=True
    # Encoding overrides from modal (None = auto)
    encoder_override: str | None = None
    nvenc_preset_override: str | None = None
    nvenc_cq_override: int | None = None
    libx265_crf_override: int | None = None
    libx265_preset_override: str | None = None
    audio_codec_override: str | None = None
    audio_bitrate_override: int | None = None
    target_resolution_override: str | None = None
    force_reencode: bool = False
    # User-chosen "no video conversion" — runs only the audio/sub cleanup
    # pass on the original file. Same effect as a rule with action="ignore",
    # but expressed per-batch from the estimate modal. Wins over
    # force_reencode if both are somehow set. v0.3.80+.
    cleanup_only: bool = False
    # v0.9.37: "apply detected language" — remux to mkv (stream copy, no
    # re-encode) to stamp a language detected for an untaggable source (AVI
    # etc.) that couldn't be written in place. Forces an audio-type (remux)
    # job even when there's no track-removal work.
    language_remux: bool = False


def _classify_job_type(
    *,
    needs_conversion: bool,
    force_reencode: bool,
    cleanup_only: bool,
    language_remux: bool,
    has_audio_work: bool,
    skip_conversion: bool,
) -> str | None:
    """Decide a queued file's job_type, or None if it should be skipped.

    Pure function so the add-from-scan classification can be unit-tested.
    Precedence: language_remux (stream-copy audio) → force_reencode forces a
    video convert even for a source that doesn't otherwise need it (e.g. an
    already-h265 file — it must NOT fall through to a no-op "audio"/remux job)
    → cleanup_only and an ignore rule suppress the video convert. Returns None
    only when there is genuinely nothing to do AND the file was explicitly
    exempted from conversion (ignore rule / cleanup-only) — the caller skips
    those with a log line.
    """
    needs_conv = bool(needs_conversion) or force_reencode
    if skip_conversion and not force_reencode:
        needs_conv = False
    if cleanup_only:
        needs_conv = False

    if language_remux:
        return "audio"
    if needs_conv and has_audio_work:
        return "combined"
    if needs_conv:
        return "convert"
    if has_audio_work:
        return "audio"
    if skip_conversion or cleanup_only:
        return None
    return "audio"


def track_work(row: dict) -> tuple[list[int], list[int], bool]:
    """The audio and subtitle tracks a job for this scanned file removes, and
    whether it has audio work at all (removals, or the original language's
    audio not first). `row`: scan_results columns (audio_tracks_json,
    subtitle_tracks_json, native_language if known)."""
    # v0.9.99: gate on keep alone, NOT `keep and not locked`.
    # Classification never emits keep=False together with locked=True —
    # that pair only arises when a user deliberately unticks a locked
    # (forced / keep-language) track in the detail panel. The old
    # `and not locked` guard silently discarded exactly that override,
    # so an explicitly-unticked forced sub was never removed.
    audio_remove: list[int] = []
    sub_remove: list[int] = []
    try:
        for t in json.loads(row["audio_tracks_json"] or "[]"):
            if not t.get("keep", True):
                audio_remove.append(t["stream_index"])
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        for t in json.loads(row["subtitle_tracks_json"] or "[]"):
            if not t.get("keep", True):
                sub_remove.append(t["stream_index"])
    except (json.JSONDecodeError, ValueError):
        pass

    # Also treat "native language not first" as audio work (reorder-only job)
    has_audio_work = (len(audio_remove) > 0 or len(sub_remove) > 0
                      or native_first(row, audio_remove) is not None)
    return audio_remove, sub_remove, has_audio_work


def native_first(row: dict, audio_remove: list[int]) -> Optional[str]:
    """The original language when a job moves its audio first (the Scanner's
    rule, scanner.needs_native_reorder, over the tracks the job keeps). `row`
    needs native_language (v0.10.0: the queue never loaded it, so this never
    fired)."""
    from backend.scanner import needs_native_reorder
    try:
        tracks = json.loads(row["audio_tracks_json"] or "[]")
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    removed = set(audio_remove)
    kept = [{**t, "keep": t.get("stream_index") not in removed} for t in tracks]
    native = row.get("native_language")
    return native.lower() if needs_native_reorder(kept, native) else None


# The scan_results columns track_work() and the job plans read.
_TRACK_COLS = ("id, file_path, file_size, needs_conversion, audio_tracks_json, subtitle_tracks_json, "
               "native_language, duration, disc_type")


async def _scan_rows_for(db, paths: list[str], cols: str = _TRACK_COLS) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for i in range(0, len(paths), 900):
        chunk = paths[i:i + 900]
        async with db.execute(
            f"SELECT {cols} FROM scan_results WHERE file_path IN ({','.join('?' * len(chunk))})", chunk,
        ) as cur:
            for r in await cur.fetchall():
                rows[r["file_path"]] = dict(r)
    return rows


async def refresh_pending_jobs(paths: Optional[list[str]] = None) -> int:
    """Re-derive pending jobs' track removals from their files' tracks — after
    the keep languages change, or a file's tracks are edited (in the Scanner
    or the queue). A conversion gains or drops its cleanup ("combined" /
    "convert"); a cleanup-only job keeps its type (with nothing left to do it
    finishes without rewriting the file). Returns the jobs changed. v0.10.0."""
    db = await connect_db()
    try:
        where, params = "status = 'pending' AND job_type IN ('convert', 'combined', 'audio')", []
        jobs = []
        scopes = [None] if paths is None else [paths[i:i + 900] for i in range(0, len(paths), 900)]
        for chunk in scopes:
            extra = "" if chunk is None else f" AND file_path IN ({','.join('?' * len(chunk))})"
            async with db.execute(
                f"SELECT id, file_path, job_type, audio_tracks_to_remove, subtitle_tracks_to_remove "
                f"FROM jobs WHERE {where}{extra}", [*params, *(chunk or [])],
            ) as cur:
                jobs += [dict(r) for r in await cur.fetchall()]
        if not jobs:
            return 0
        rows = await _scan_rows_for(db, sorted({j["file_path"] for j in jobs}))
        updates = []
        for job in jobs:
            row = rows.get(job["file_path"])
            if row is None:
                continue
            audio_remove, sub_remove, has_audio_work = track_work(row)
            job_type = job["job_type"] if job["job_type"] == "audio" else ("combined" if has_audio_work else "convert")
            try:
                before = (json.loads(job["audio_tracks_to_remove"] or "[]"), json.loads(job["subtitle_tracks_to_remove"] or "[]"))
            except (ValueError, TypeError):
                before = None
            if before != (audio_remove, sub_remove) or job_type != job["job_type"]:
                updates.append((json.dumps(audio_remove), json.dumps(sub_remove), job_type, job["id"]))
        if updates:
            await db.executemany(
                "UPDATE jobs SET audio_tracks_to_remove = ?, subtitle_tracks_to_remove = ?, job_type = ? "
                "WHERE id = ? AND status = 'pending'", updates)
            await db.commit()
        return len(updates)
    finally:
        await db.close()


async def _originals_setting(db) -> dict:
    async with db.execute(
        "SELECT key, value FROM settings WHERE key IN ('backup_original_days', 'trash_original_after_conversion')"
    ) as cur:
        values = {r["key"]: r["value"] for r in await cur.fetchall()}
    try:
        days = int(values.get("backup_original_days") or 0)
    except ValueError:
        days = 0
    if days > 0:
        return {"action": "keep", "days": days}
    if str(values.get("trash_original_after_conversion", "false")).lower() == "true":
        return {"action": "trash"}
    return {"action": "delete"}


def _job_savings(job: dict, row: dict, audio: list[dict], global_cq: int) -> int:
    """About what a pending job saves: the removed tracks, and the video
    conversion at the job's quality (else the default encoder's)."""
    from backend.encoding_estimates import _CRF_OFFSET, cq_to_savings_pct
    size = row.get("file_size") or job.get("original_size") or 0
    remove = set(json.loads(job.get("audio_tracks_to_remove") or "[]"))
    removed = sum(t.get("size_estimate_bytes") or 0 for t in audio if t.get("stream_index") in remove)
    saved = min(removed, size)
    if job["job_type"] in ("convert", "combined"):
        if (job.get("encoder") or "") == "libx265" and job.get("libx265_crf") is not None:
            cq = job["libx265_crf"] - _CRF_OFFSET
        elif job.get("nvenc_cq") is not None:
            cq = job["nvenc_cq"]
        else:
            cq = global_cq
        saved += int((size - saved) * cq_to_savings_pct(cq))
    return saved


_JOB_PLAN_COLS = ("id, file_path, job_type, encoder, nvenc_cq, libx265_crf, original_size, "
                  "audio_tracks_to_remove, subtitle_tracks_to_remove")


@router.get("/pending-summary")
async def pending_summary():
    """What the pending queue will do (v0.10.0): jobs by type and encoder,
    the tracks it removes by language, the size and about what it saves, and
    what becomes of the originals."""
    from backend.encoding_estimates import load_effective_cq
    db = await connect_db()
    try:
        async with db.execute(f"SELECT {_JOB_PLAN_COLS} FROM jobs WHERE status = 'pending'") as cur:
            jobs = [dict(r) for r in await cur.fetchall()]
        rows = await _scan_rows_for(db, sorted({j["file_path"] for j in jobs}))
        global_cq = await load_effective_cq(db)
        originals = await _originals_setting(db)
        async with db.execute("SELECT value FROM settings WHERE key = 'default_encoder'") as cur:
            r = await cur.fetchone()
        default_encoder = (r["value"] if r else None) or "nvenc"
    finally:
        await db.close()

    def tally() -> dict:
        by_type: dict[str, int] = {}
        by_encoder: dict[str, int] = {}
        removals: dict[str, dict[str, int]] = {"audio": {}, "subtitles": {}}
        total_size = saved = 0
        for job in jobs:
            by_type[job["job_type"]] = by_type.get(job["job_type"], 0) + 1
            if job["job_type"] == "health_check":  # reads the file, changes nothing
                continue
            row = rows.get(job["file_path"], {})
            size = row.get("file_size") or job.get("original_size") or 0
            total_size += size
            if job["job_type"] in ("convert", "combined"):
                enc = job.get("encoder") or default_encoder
                by_encoder[enc] = by_encoder.get(enc, 0) + 1
            try:
                audio = json.loads(row.get("audio_tracks_json") or "[]")
                subs = json.loads(row.get("subtitle_tracks_json") or "[]")
                lists = (json.loads(job["audio_tracks_to_remove"] or "[]"), json.loads(job["subtitle_tracks_to_remove"] or "[]"))
            except (ValueError, TypeError):
                continue
            for kind, tracks, remove in (("audio", audio, lists[0]), ("subtitles", subs, lists[1])):
                for t in tracks:
                    if t.get("stream_index") in remove:
                        lang = (t.get("language") or "und").lower()
                        removals[kind][lang] = removals[kind].get(lang, 0) + 1
            saved += _job_savings(job, row, audio, global_cq) if row else 0
        return {"jobs": len(jobs), "by_type": by_type, "by_encoder": by_encoder, "removals": removals,
                "total_size": total_size, "estimated_savings": saved}

    import asyncio
    out = await asyncio.to_thread(tally)
    out["originals"] = originals
    return out


@router.get("/{job_id}/plan")
async def job_plan(job_id: int):
    """What a pending job will do (v0.10.0): its file's tracks and which it
    removes, about what it saves, and what becomes of the original. The
    tracks are edited on the file (PUT /api/scan/results/{scan_id}/tracks),
    which updates the job."""
    from backend.encoding_estimates import load_effective_cq
    db = await connect_db()
    try:
        async with db.execute(f"SELECT {_JOB_PLAN_COLS} FROM jobs WHERE id = ?", (job_id,)) as cur:
            job = await cur.fetchone()
        if job is None:
            raise ApiError(status_code=404, detail="Job not found", code="jobs.notFound")
        job = dict(job)
        row = (await _scan_rows_for(db, [job["file_path"]])).get(job["file_path"])
        global_cq = await load_effective_cq(db)
        originals = await _originals_setting(db)
    finally:
        await db.close()
    if row is None:
        return {"scan_id": None, "job_type": job["job_type"], "audio": [], "subtitles": [], "native_first": None,
                "file_size": job.get("original_size") or 0, "estimated_savings": 0, "originals": originals}
    audio = json.loads(row["audio_tracks_json"] or "[]")
    subs = json.loads(row["subtitle_tracks_json"] or "[]")
    remove_a = set(json.loads(job["audio_tracks_to_remove"] or "[]"))
    remove_s = set(json.loads(job["subtitle_tracks_to_remove"] or "[]"))
    reorder = native_first(row, list(remove_a))
    for t in audio:
        t["remove"] = t.get("stream_index") in remove_a
    for t in subs:
        t["remove"] = t.get("stream_index") in remove_s
    return {
        "scan_id": row["id"], "job_type": job["job_type"], "audio": audio, "subtitles": subs,
        "native_first": reorder,  # the original language's audio moved first
        "file_size": row["file_size"] or 0, "estimated_savings": _job_savings(job, row, audio, global_cq),
        "originals": originals,
    }


async def _jobs_from_scan(file_paths: list[str], payload: BulkQueueFromScanRequest,
                          new_files: bool = False) -> tuple[list[dict], int]:
    """The jobs Add to Queue creates for scanned files, and how many a rule
    skipped or kept from converting: rules, the conversion filters, the track
    work, the job type and quality. Also the watcher's auto-queue (v0.10.0;
    it decided on its own, without subtitle removals or the reorder), with
    `new_files`: a file with nothing to do is left out there, where an
    explicit Add to Queue still queues it (a remux)."""
    from backend.rule_resolver import resolve_rules_for_batch

    # Resolve encoding rules for all files in batch (unless overridden)
    if payload.override_rules:
        rule_results = {}
    else:
        rule_results = await resolve_rules_for_batch(file_paths)

    db = await connect_db()
    try:
        # Load conversion filter settings + smart encoding settings
        from backend.content_detect import SMART_CQ_KEYS
        smart_keys = (
            'min_bitrate_mbps', 'max_bitrate_mbps', 'min_file_size_mb',
            'default_encoder', *SMART_CQ_KEYS,
        )
        filter_settings = {}
        async with db.execute(
            f"SELECT key, value FROM settings WHERE key IN ({','.join('?' for _ in smart_keys)})",
            smart_keys,
        ) as cur:
            for row in await cur.fetchall():
                filter_settings[row["key"]] = row["value"]
        min_bitrate_bps = int(filter_settings.get("min_bitrate_mbps", "0")) * 1_000_000
        max_bitrate_bps = int(filter_settings.get("max_bitrate_mbps", "0")) * 1_000_000
        min_file_size_bytes = int(filter_settings.get("min_file_size_mb", "0")) * 1024 * 1024
        from backend.content_detect import smart_cq_settings, smart_quality
        smart_settings = smart_cq_settings(filter_settings)
        default_encoder = filter_settings.get("default_encoder", "nvenc")

        # Check if unwatched prioritization is enabled
        plex_prioritize = False
        unwatched_folders: set[str] = set()
        try:
            async with db.execute("SELECT value FROM settings WHERE key = 'plex_prioritize_unwatched'") as cur:
                row = await cur.fetchone()
                plex_prioritize = row and row["value"].lower() == "true"
            if plex_prioritize:
                async with db.execute(
                    "SELECT folder_path FROM plex_metadata_cache WHERE metadata_type = 'watch_status' AND metadata_value = 'unwatched'"
                ) as cur:
                    unwatched_folders = {r["folder_path"] for r in await cur.fetchall()}
        except Exception:
            pass

        # Batch-load all scan_results up front (avoids N+1 queries)
        scan_rows: dict[str, dict] = {}
        CHUNK = 900
        for i in range(0, len(file_paths), CHUNK):
            chunk = file_paths[i:i + CHUNK]
            placeholders = ",".join("?" * len(chunk))
            async with db.execute(
                f"SELECT file_path, file_size, needs_conversion, audio_tracks_json, "
                f"subtitle_tracks_json, native_language, duration, COALESCE(video_height, 0) as video_height, "
                f"COALESCE(video_width, 0) as video_width, disc_type "
                f"FROM scan_results WHERE file_path IN ({placeholders})",
                chunk,
            ) as cur:
                for r in await cur.fetchall():
                    scan_rows[r["file_path"]] = dict(r)

        jobs_to_insert: list[dict] = []
        ignored_by_rule = 0
        for fp in file_paths:
            rule = rule_results.get(fp)

            # "skip" = do nothing at all, skip entirely.
            # language_remux is an explicit per-file request to stamp a
            # detected language via stream-copy — honour it over a skip rule
            # (the rule governs video conversion, not metadata fixes). v0.9.52.
            if rule and rule["action"] == "skip" and not payload.language_remux:
                ignored_by_rule += 1
                continue

            # "ignore" = skip video conversion, still do audio/sub cleanup
            skip_conversion = rule and rule["action"] == "ignore"
            if skip_conversion:
                ignored_by_rule += 1

            row = scan_rows.get(fp)
            if not row:
                if payload.language_remux:
                    print(f"[QUEUE] language_remux: no scan_results row for {fp}", flush=True)
                continue

            # Check conversion filters (bitrate ceiling, min file size).
            # Skip them for a language_remux: a stream-copy tag fix has no
            # video-conversion cost, so size/bitrate gates don't apply and
            # would otherwise drop the file with a "Nothing queued". v0.9.52.
            if not payload.override_rules and not payload.language_remux:
                file_size = row["file_size"] or 0
                duration = row["duration"] or 0

                if min_file_size_bytes > 0 and file_size < min_file_size_bytes:
                    continue
                if max_bitrate_bps > 0 and duration > 0:
                    bitrate = file_size * 8 / duration
                    if bitrate > max_bitrate_bps:
                        continue  # Above ceiling — don't convert (already high quality)
                if min_bitrate_bps > 0 and duration > 0:
                    bitrate = file_size * 8 / duration
                    if bitrate < min_bitrate_bps:
                        continue  # Below minimum — savings too small

            audio_remove, sub_remove, has_audio_work = track_work(row)

            # force_reencode overrides both needs_conversion AND skip rules;
            # cleanup_only (per-batch user choice) wins over force_reencode.
            # A forced re-encode of an already-h265 file must classify as
            # "convert" (not a no-op "audio"/remux job). See _classify_job_type.
            # v0.9.120: a disc row always needs conversion — an audio-only remux
            # can't open a disc image (exit 183). Force it here from the stored
            # disc_type so it applies immediately, without waiting for a re-scan
            # to refresh the needs_conversion column.
            job_type = _classify_job_type(
                needs_conversion=bool(row["needs_conversion"]) or bool(row.get("disc_type")),
                force_reencode=payload.force_reencode,
                cleanup_only=payload.cleanup_only,
                language_remux=payload.language_remux,
                has_audio_work=has_audio_work,
                skip_conversion=skip_conversion,
            )
            if job_type is None:
                # Nothing to do and the file was explicitly exempted from
                # conversion (ignore rule / cleanup-only) — skip with a note.
                if skip_conversion:
                    print(f"[QUEUE] Skipped {fp} (ignore rule: {rule['rule_name']}), no audio/sub work", flush=True)
                elif payload.cleanup_only:
                    print(f"[QUEUE] Skipped {fp} (cleanup_only: no audio/sub work to do)", flush=True)
                continue
            if new_files and job_type == "audio" and not has_audio_work:
                continue  # nothing to do: only an explicit Add to Queue remuxes it

            # Apply encoding rule overrides (if any)
            encoder = (rule.get("encoder") if rule else None) or default_encoder
            nvenc_preset = rule.get("nvenc_preset") if rule else None
            nvenc_cq = rule.get("nvenc_cq") if rule else None
            libx265_crf = rule.get("libx265_crf") if rule else None
            libx265_preset = rule.get("libx265_preset") if rule else None
            target_resolution = rule.get("target_resolution") if rule else None
            audio_codec = rule.get("audio_codec") if rule else None
            audio_bitrate = rule.get("audio_bitrate") if rule else None

            # Content type detection / resolution-aware quality (v0.10.0):
            # the per-file CQ the estimate shows, unless a rule or the Add to
            # Queue dialog sets the quality. (These were estimate-only before,
            # so jobs always ran at the global CQ.)
            if (job_type in ("convert", "combined")
                    and nvenc_cq is None and libx265_crf is None
                    and payload.nvenc_cq_override is None and payload.libx265_crf_override is None):
                nvenc_cq, libx265_crf = smart_quality(
                    fp, row.get("video_width"), row.get("video_height"), smart_settings)

            # Modal encoding overrides take highest precedence
            if payload.encoder_override is not None:
                encoder = payload.encoder_override
            if payload.nvenc_preset_override is not None:
                nvenc_preset = payload.nvenc_preset_override
            if payload.nvenc_cq_override is not None:
                nvenc_cq = payload.nvenc_cq_override
            if payload.libx265_crf_override is not None:
                libx265_crf = payload.libx265_crf_override
            if payload.libx265_preset_override is not None:
                libx265_preset = payload.libx265_preset_override
            if payload.audio_codec_override is not None:
                audio_codec = payload.audio_codec_override
            if payload.audio_bitrate_override is not None:
                audio_bitrate = payload.audio_bitrate_override
            if payload.target_resolution_override is not None:
                target_resolution = payload.target_resolution_override

            jobs_to_insert.append({
                "file_path": fp,
                "job_type": job_type,
                "encoder": encoder,
                "audio_tracks_to_remove": audio_remove,
                "subtitle_tracks_to_remove": sub_remove,
                "original_size": row["file_size"],
                "nvenc_preset": nvenc_preset,
                "nvenc_cq": nvenc_cq,
                "libx265_crf": libx265_crf,
                "libx265_preset": libx265_preset,
                "target_resolution": target_resolution,
                "audio_codec": audio_codec,
                "audio_bitrate": audio_bitrate,
                "priority": max(
                    payload.priority,
                    rule.get("queue_priority") or 0 if rule else 0,
                    1 if plex_prioritize and any(fp.startswith(uf) for uf in unwatched_folders) else 0,
                ),
            })

        if ignored_by_rule > 0:
            await db.commit()
    finally:
        await db.close()
    return jobs_to_insert, ignored_by_rule


async def queue_new_files(file_paths: list[str], priority: int, queue: JobQueue) -> tuple[int, int]:
    """The watcher's auto-queue: new files get the jobs Add to Queue would
    give them, less any with nothing to do. Returns (jobs added, files a rule
    skipped or kept from converting)."""
    payload = BulkQueueFromScanRequest(file_paths=file_paths, priority=priority)
    jobs, by_rule = await _jobs_from_scan(sorted(file_paths), payload, new_files=True)
    ids = await queue.add_jobs_bulk(jobs)
    return sum(1 for jid in ids if jid), by_rule


@router.post("/add-from-scan")
async def add_jobs_from_scan(payload: BulkQueueFromScanRequest):
    """Create jobs from scan results — resolves track data from DB automatically."""
    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")

    file_paths = list(payload.file_paths)

    # Selected folders, or the whole list on "select all", through the
    # active filter (sorted below).
    from backend.routes.scan import _paths_matching
    folder_paths = [p for p in file_paths if p.endswith("/")]
    if folder_paths:
        file_paths = [p for p in file_paths if not p.endswith("/")]
        resolved = await _paths_matching(payload.filter or "all", folder_paths)
        print(f"[QUEUE] Resolved {len(folder_paths)} folder(s) -> {len(resolved)} files "
              f"(filter '{payload.filter or 'all'}')", flush=True)
        file_paths += resolved
    print(f"[QUEUE] Total file paths: {len(file_paths)}", flush=True)
    if payload.select_all and not file_paths:
        file_paths = await _paths_matching(payload.filter or "all")

    if not file_paths:
        return {"job_ids": [], "added": 0}

    # Sort the resolved paths so queue_order follows show/season/episode
    # grouping (e.g. all "Thunder in My Heart S01E01..S02E08" land
    # contiguously, then "Tiffany Haddish Presents..."). Folder-resolution
    # queries above don't ORDER BY, and the select-all path uses
    # scan_results.id which is insertion order — neither is meaningful in
    # the queue. Alphabetical by full path is what users expect when they
    # bulk-add a chunk of TV shows. v0.3.61.
    file_paths.sort()

    jobs_to_insert, ignored_by_rule = await _jobs_from_scan(file_paths, payload)

    # Bulk-insert all queued jobs in a single transaction (huge perf win).
    # `all_ids` parallels `jobs_to_insert` — entries are 0 for files that
    # were skipped because they already had a pending/running job. The
    # frontend uses `skipped_existing` to phrase the toast correctly when
    # everything submitted was already queued (vs. "no actionable items").
    # v0.3.60.
    all_ids = await _queue.add_jobs_bulk(jobs_to_insert)
    job_ids = [jid for jid in all_ids if jid]
    skipped_existing = sum(1 for jid in all_ids if jid == 0)
    return {
        "job_ids": job_ids,
        "added": len(job_ids),
        "ignored_by_rule": ignored_by_rule,
        "skipped_existing": skipped_existing,
    }


@router.get("/")
async def list_jobs(status: Optional[str] = None, limit: int = 0, offset: int = 0, search: str = ""):
    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")
    if status:
        return await _queue.get_jobs_by_status(status, limit=limit, offset=offset, search=search)
    return await _queue.get_all_jobs(limit=limit, offset=offset)


@router.get("/ids")
async def list_job_ids(status: str, search: str = ""):
    """Ordered job ids for one status tab (v0.9.139) — the Queue page polls
    this instead of re-downloading thousands of pending rows."""
    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")
    return await _queue.get_job_ids_by_status(status, search=search)


@router.get("/stats")
async def get_stats():
    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")
    stats = await _queue.get_stats()
    # Surface the stream-aware pause state so the Queue page can render a
    # "Paused — <Server> streaming" banner instead of leaving the user to
    # wonder why the progress bars are frozen. v0.4.6+. Each per-server
    # pause-logged flag on the worker is True iff that server's stream
    # check returned True on the last pause-loop iteration; the
    # `_stream_paused_jobs` set holds the SIGSTOPped job IDs.
    if _worker is not None:
        stream_paused_servers = []
        if getattr(_worker, "_plex_pause_logged", False):
            stream_paused_servers.append("Plex")
        if getattr(_worker, "_jellyfin_pause_logged", False):
            stream_paused_servers.append("Jellyfin")
        if getattr(_worker, "_emby_pause_logged", False):
            stream_paused_servers.append("Emby")
        frozen_count = len(getattr(_worker, "_stream_paused_jobs", set()) or set())
        stats["stream_pause"] = {
            "active": bool(stream_paused_servers) or frozen_count > 0,
            "servers": stream_paused_servers,
            "frozen_jobs": frozen_count,
        }
    return stats


@router.post("/start")
async def start_worker():
    if _worker is None:
        raise ApiError(status_code=503, detail="Worker not initialized", code="queue.workerNotInitialized")
    try:
        print(f"[API] Starting worker, running={_worker._running}, paused={_worker._paused}", flush=True)
        _worker.start()
        print("[API] Worker started", flush=True)
        return {"status": "started"}
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[API] Worker start FAILED: {exc}", flush=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/pause")
async def pause_worker():
    if _worker is None:
        raise ApiError(status_code=503, detail="Worker not initialized", code="queue.workerNotInitialized")
    _worker.pause()
    return {"status": "paused"}


@router.post("/resume")
async def resume_worker():
    if _worker is None:
        raise ApiError(status_code=503, detail="Worker not initialized", code="queue.workerNotInitialized")
    _worker.resume()
    return {"status": "resumed"}


@router.post("/cancel-current")
async def cancel_current_job(request: Request, job_id: Optional[int] = None):
    if _worker is None:
        raise ApiError(status_code=503, detail="Worker not initialized", code="queue.workerNotInitialized")
    if job_id is not None:
        db = await connect_db()
        try:
            async with db.execute(
                "SELECT assigned_node_id FROM jobs WHERE id = ? AND status = 'running'", (job_id,)
            ) as cur:
                row = await cur.fetchone()
            node_id = row["assigned_node_id"] if row else None
            if node_id and node_id != "local":
                # Running on a remote node: its next progress report tells
                # it to stop. This only killed local ffmpeg processes, so a
                # remote job kept encoding (M6, v0.10.0).
                await db.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
                await db.commit()
                nm = getattr(request.app.state, "node_manager", None)
                if nm is not None:
                    nm.request_cancel(job_id)
                return {"status": "cancel_requested", "job_id": job_id}
        finally:
            await db.close()
    cancelled_id = await _worker.cancel_current(job_id)
    if cancelled_id is None:
        return {"status": "no_job_running"}
    return {"status": "cancelled", "job_id": cancelled_id}


@router.post("/reorder")
async def reorder_jobs(payload: ReorderRequest):
    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")
    await _queue.reorder_jobs(payload.job_ids)
    return {"status": "reordered"}


@router.post("/bulk-update-settings")
async def bulk_update_settings(payload: BulkUpdateSettingsRequest):
    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")
    # Build SET clause dynamically from provided fields
    updates = []
    params = []
    if payload.nvenc_preset is not None:
        updates.append("nvenc_preset = ?")
        params.append(payload.nvenc_preset)
    if payload.nvenc_cq is not None:
        updates.append("nvenc_cq = ?")
        params.append(payload.nvenc_cq)
    if payload.libx265_preset is not None:
        updates.append("libx265_preset = ?")
        params.append(payload.libx265_preset)
    if payload.libx265_crf is not None:
        updates.append("libx265_crf = ?")
        params.append(payload.libx265_crf)
    if payload.audio_codec is not None:
        updates.append("audio_codec = ?")
        params.append(payload.audio_codec)
    if payload.audio_bitrate is not None:
        updates.append("audio_bitrate = ?")
        params.append(payload.audio_bitrate)
    if payload.priority is not None:
        updates.append("priority = ?")
        params.append(max(0, min(2, payload.priority)))
    if not updates:
        return {"updated": 0}
    # v0.5.24: chunked the IN clause so a bulk-reorder of 1000+ pending
    # jobs doesn't risk SQLite's variable / expression limits. Each chunk
    # carries its own `params` copy (the SET values + that chunk's IDs).
    CHUNK = 900
    set_clause = ", ".join(updates)
    db = await connect_db()
    try:
        count = 0
        for i in range(0, len(payload.job_ids), CHUNK):
            id_chunk = list(payload.job_ids)[i:i + CHUNK]
            placeholders = ", ".join(["?"] * len(id_chunk))
            sql = (
                f"UPDATE jobs SET {set_clause} "
                f"WHERE id IN ({placeholders}) AND status = 'pending'"
            )
            async with db.execute(sql, params + id_chunk) as cur:
                count += cur.rowcount or 0
        await db.commit()
        return {"updated": count}
    finally:
        await db.close()


@router.post("/bulk-move")
async def bulk_move(payload: BulkMoveRequest):
    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")
    if payload.position not in ("top", "bottom", "up", "down"):
        raise ApiError(status_code=400, detail="position must be top, bottom, up, or down", code="jobs.invalidPosition")
    db = await connect_db()
    try:
        # Get all pending jobs ordered by queue_order
        async with db.execute(
            "SELECT id, queue_order FROM jobs WHERE status = 'pending' ORDER BY queue_order ASC"
        ) as cur:
            rows = await cur.fetchall()
        all_ids = [r["id"] for r in rows]
        selected = set(payload.job_ids)
        # Filter to only pending job ids that exist
        selected_pending = [jid for jid in all_ids if jid in selected]
        rest = [jid for jid in all_ids if jid not in selected]

        if not selected_pending:
            return {"status": "no_pending_jobs_matched"}

        if payload.position == "top":
            new_order = selected_pending + rest
        elif payload.position == "bottom":
            new_order = rest + selected_pending
        elif payload.position == "up":
            # Move each selected job up by 1 position
            new_order = list(all_ids)
            for jid in selected_pending:
                idx = new_order.index(jid)
                if idx > 0 and new_order[idx - 1] not in selected:
                    new_order[idx - 1], new_order[idx] = new_order[idx], new_order[idx - 1]
        elif payload.position == "down":
            # Move each selected job down by 1 position (iterate in reverse)
            new_order = list(all_ids)
            for jid in reversed(selected_pending):
                idx = new_order.index(jid)
                if idx < len(new_order) - 1 and new_order[idx + 1] not in selected:
                    new_order[idx], new_order[idx + 1] = new_order[idx + 1], new_order[idx]

        for order, jid in enumerate(new_order, start=1):
            await db.execute(
                "UPDATE jobs SET queue_order = ? WHERE id = ?", (order, jid)
            )
        await db.commit()
        return {"status": "moved", "new_order": new_order}
    finally:
        await db.close()


@router.post("/bulk-ignore")
async def bulk_ignore(payload: BulkIgnoreRequest):
    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")
    db = await connect_db()
    try:
        ignored = 0
        now = datetime.now(timezone.utc).isoformat()
        for job_id in payload.job_ids:
            # Get the job's file_path
            async with db.execute("SELECT file_path FROM jobs WHERE id = ?", (job_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                continue
            file_path = row["file_path"]
            # Add to ignored_files
            await db.execute(
                "INSERT OR IGNORE INTO ignored_files (file_path, reason, ignored_at) VALUES (?, ?, ?)",
                (file_path, "user_ignored", now),
            )
            # Delete the job
            await db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            ignored += 1
        await db.commit()
        return {"ignored": ignored}
    finally:
        await db.close()


# --- Health check ---

class HealthCheckRequest(BaseModel):
    file_paths: list[str] = []
    mode: str = "quick"  # "quick" | "thorough"
    select_all: bool = False
    filter: str = "all"


@router.post("/health-check")
async def queue_health_checks(payload: HealthCheckRequest):
    """Queue health_check jobs for the given files (or current filter when select_all=True)."""
    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")

    mode = payload.mode.lower()
    if mode not in ("quick", "thorough"):
        raise ApiError(status_code=400, detail="mode must be 'quick' or 'thorough'", code="jobs.invalidHealthCheckMode")

    file_paths = list(payload.file_paths)

    # Selected folders, or the whole list on "select all", through the
    # active filter.
    from backend.routes.scan import _paths_matching
    folder_paths = [p for p in file_paths if p.endswith("/")]
    if folder_paths:
        file_paths = [p for p in file_paths if not p.endswith("/")]
        file_paths += await _paths_matching(payload.filter or "all", folder_paths)
    if payload.select_all and not file_paths:
        file_paths = await _paths_matching(payload.filter or "all")

    if not file_paths:
        return {"added": 0, "job_ids": []}

    # Build bulk-insert payload. Store mode in the 'encoder' column (re-uses existing schema).
    jobs_to_insert = [
        {
            "file_path": fp,
            "job_type": "health_check",
            "encoder": mode,
            "priority": 0,
        }
        for fp in file_paths
    ]
    all_ids = await _queue.add_jobs_bulk(jobs_to_insert)
    job_ids = [jid for jid in all_ids if jid]

    # Auto-start the worker if idle (never over a manual pause)
    if job_ids and _worker is not None:
        _worker.start_if_idle()

    return {"added": len(job_ids), "job_ids": job_ids, "mode": mode}


# --- Add by path (for NZBGet/external integrations) ---

import os as _os

class AddByPathRequest(BaseModel):
    file_paths: list[str]
    priority: int = 1
    force_reencode: bool = False
    skip_arr_rescan: bool = False
    insert_next: bool = False
    nzbget_category: str | None = None


class ResetHealthRequest(BaseModel):
    file_paths: list[str] = []
    reset_all_corrupt: bool = False
    unignore: bool = True


@router.post("/health-check/reset")
async def reset_health_status(payload: ResetHealthRequest):
    """Clear stored health_status so a file gets re-checked on the next pass.

    Two modes:
      * reset_all_corrupt=True  → clear every row currently flagged corrupt.
        Used after shipping a classifier fix to invalidate false positives
        en masse (e.g. the "number of reference frames exceeds max" noise).
      * file_paths supplied     → clear only those specific paths.

    By default we also remove them from ignored_files so they return to the
    normal scan views. Set unignore=False to leave ignored_files alone.
    """
    if not payload.reset_all_corrupt and not payload.file_paths:
        raise ApiError(status_code=400, detail="Provide file_paths or set reset_all_corrupt=true", code="jobs.noFilesOrResetAll")

    db = await connect_db()
    try:
        if payload.reset_all_corrupt:
            async with db.execute(
                "SELECT file_path FROM scan_results WHERE health_status = 'corrupt'"
            ) as cur:
                targets = [r["file_path"] for r in await cur.fetchall()]
        else:
            targets = list(payload.file_paths)

        if not targets:
            return {"reset": 0, "unignored": 0}

        # SQLite has a parameter limit (~999); chunk to be safe.
        reset_count = 0
        unignored_count = 0
        CHUNK = 500
        for i in range(0, len(targets), CHUNK):
            batch = targets[i:i + CHUNK]
            placeholders = ",".join("?" * len(batch))
            cur = await db.execute(
                f"UPDATE scan_results SET health_status = NULL, health_errors_json = NULL, "
                f"health_checked_at = NULL, health_check_type = NULL "
                f"WHERE file_path IN ({placeholders})",
                batch,
            )
            reset_count += cur.rowcount or 0

            if payload.unignore:
                cur2 = await db.execute(
                    f"DELETE FROM ignored_files WHERE file_path IN ({placeholders})",
                    batch,
                )
                unignored_count += cur2.rowcount or 0

        await db.commit()
        return {"reset": reset_count, "unignored": unignored_count, "targeted": len(targets)}
    finally:
        await db.close()


@router.post("/health-check/clear-pending")
async def clear_pending_health_checks():
    """Emergency cleanup: delete all PENDING health_check jobs.

    Useful when auto-queue has flooded the queue. Running jobs and other job
    types are untouched.
    """
    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")
    # In chunks (F14): a flood is tens of thousands of jobs.
    n = await _queue.delete_jobs_where("job_type = 'health_check' AND status = 'pending'")
    db = await connect_db()
    try:
        # Clean up the file_events queued entries too so the Activity feed isn't drowned
        await db.execute(
            "DELETE FROM file_events WHERE event_type = 'queued' AND summary LIKE '%health check%'"
        )
        await db.commit()
        return {"deleted": n}
    finally:
        await db.close()


@router.post("/add-by-path")
async def add_jobs_by_path(payload: AddByPathRequest):
    """Queue files by path — probes files directly without requiring scan_results."""
    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")

    from backend.scanner import probe_file, classify_audio_tracks, classify_subtitle_tracks, detect_native_language, codec_matches_source
    from backend.rule_resolver import resolve_rules_for_batch
    from backend.config import settings
    from backend.media_paths import load_media_dirs, is_in_any, _resolve

    # Load source codecs, default encoder and the per-file CQ settings.
    # (v0.10.0: this used `async with connect_db()`, which always raised and
    # was swallowed, so the saved source codecs and encoder were never read.)
    from backend.scanner import DEFAULT_SOURCE_CODECS
    from backend.content_detect import SMART_CQ_KEYS, smart_cq_settings, smart_quality
    source_codecs = list(DEFAULT_SOURCE_CODECS)
    default_encoder = "nvenc"
    _values: dict = {}
    try:
        _db = await connect_db()
        try:
            async with _db.execute(
                "SELECT key, value FROM settings WHERE key IN ('source_codecs', 'default_encoder', "
                + ",".join(f"'{k}'" for k in SMART_CQ_KEYS) + ")"
            ) as _cur:
                _values = {r["key"]: r["value"] for r in await _cur.fetchall()}
        finally:
            await _db.close()
        if _values.get("source_codecs"):
            source_codecs = json.loads(_values["source_codecs"])
        if _values.get("default_encoder"):
            default_encoder = _values["default_encoder"]
    except Exception:
        pass
    smart_settings = smart_cq_settings(_values)

    # Containment check — stops callers from queuing `/etc/hostname` etc.
    allowed_dirs = await load_media_dirs()
    if not allowed_dirs:
        raise ApiError(
            status_code=400,
            detail="No media directories configured",
            code="media.noMediaDirs",
        )
    safe_file_paths: list[str] = []
    early_errors: list[str] = []
    for raw_fp in payload.file_paths:
        resolved = _resolve(raw_fp)
        if not is_in_any(resolved, allowed_dirs):
            early_errors.append(f"Outside media dirs: {raw_fp}")
            continue
        safe_file_paths.append(resolved)

    # Resolve encoding rules for allowlisted paths only (skip any that
    # failed the containment check so rules aren't evaluated against
    # attacker-supplied paths).
    extra_context = {}
    if payload.nzbget_category:
        extra_context["nzbget_category"] = payload.nzbget_category
    rule_results = await resolve_rules_for_batch(safe_file_paths, extra_context=extra_context)

    added = 0
    errors = list(early_errors)

    for fp in safe_file_paths:
        if not _os.path.exists(fp):
            errors.append(f"File not found: {fp}")
            continue

        probe = await probe_file(fp)
        if not probe:
            errors.append(f"Probe failed: {fp}")
            continue

        video_codec = (probe.get("video_codec") or "").lower()
        needs_conversion = codec_matches_source(video_codec, source_codecs)
        # v0.9.122: discs always need conversion regardless of codec (see scanner).
        if probe.get("disc_type"):
            needs_conversion = True
        if payload.force_reencode:
            needs_conversion = True

        print(f"[API] add-by-path: {_os.path.basename(fp)} codec={video_codec} source_codecs={source_codecs} needs_conversion={needs_conversion}", flush=True)

        native_lang = detect_native_language(probe.get("audio_tracks", []))
        audio_tracks = classify_audio_tracks(probe.get("audio_tracks", []), native_lang, probe.get("duration", 0))
        sub_tracks = classify_subtitle_tracks(probe.get("subtitle_tracks", []), native_lang)

        audio_remove = [t.stream_index for t in audio_tracks if not t.keep and not t.locked]
        sub_remove = [t.stream_index for t in sub_tracks if not t.keep and not t.locked]
        has_audio_work = len(audio_remove) > 0 or len(sub_remove) > 0

        if needs_conversion and has_audio_work:
            job_type = "combined"
        elif needs_conversion:
            job_type = "convert"
        elif has_audio_work:
            job_type = "audio"
        else:
            print(f"[API] add-by-path: SKIPPED {_os.path.basename(fp)} — no conversion or audio work needed", flush=True)
            continue

        # Apply encoding rule overrides
        rule = rule_results.get(fp)
        if rule and rule["action"] == "skip":
            print(f"[API] add-by-path: SKIPPED {_os.path.basename(fp)} — rule '{rule['rule_name']}' says skip", flush=True)
            continue

        encoder = (rule.get("encoder") if rule else None) or default_encoder
        nvenc_preset = rule.get("nvenc_preset") if rule else None
        nvenc_cq = rule.get("nvenc_cq") if rule else None
        libx265_crf = rule.get("libx265_crf") if rule else None
        libx265_preset = rule.get("libx265_preset") if rule else None
        target_resolution = rule.get("target_resolution") if rule else None
        audio_codec = rule.get("audio_codec") if rule else None
        audio_bitrate = rule.get("audio_bitrate") if rule else None
        # v0.10.0: content type detection / resolution-aware quality, unless
        # a rule sets the quality.
        if job_type in ("convert", "combined") and nvenc_cq is None and libx265_crf is None:
            nvenc_cq, libx265_crf = smart_quality(
                fp, probe.get("video_width"), probe.get("video_height"), smart_settings)

        job_id = await _queue.add_job(
            file_path=fp,
            job_type=job_type,
            encoder=encoder,
            audio_tracks_to_remove=audio_remove,
            subtitle_tracks_to_remove=sub_remove,
            original_size=probe.get("file_size", 0),
            nvenc_preset=nvenc_preset,
            nvenc_cq=nvenc_cq,
            libx265_crf=libx265_crf,
            libx265_preset=libx265_preset,
            target_resolution=target_resolution,
            audio_codec=audio_codec,
            audio_bitrate=audio_bitrate,
            priority=max(payload.priority, rule.get("queue_priority") or 0 if rule else 0),
            insert_next=payload.insert_next,
        )
        added += 1
        print(f"[API] Queued by path: {_os.path.basename(fp)} ({job_type}, priority={payload.priority}, insert_next={payload.insert_next})", flush=True)

    # Auto-start queue if items were added and worker is idle (never over a
    # manual pause)
    if added > 0 and _worker is not None and _worker.start_if_idle():
        print(f"[API] Auto-started queue for {added} new job(s) from add-by-path", flush=True)

    return {"added": added, "errors": errors}


# --- Test Encode (must be before /{job_id} routes to avoid path conflicts) ---

class TestEncodeRequest(BaseModel):
    file_path: str
    # None = the configured default encoder (the Estimate modal's "Auto").
    encoder: str | None = None
    # The dialog's quality (CQ for NVENC, CRF for libx265) and preset; None
    # = what the job would get from its rule or the settings.
    cq: int | None = None
    preset: str | None = None
    sample_seconds: int = 30


@router.post("/test-encode")
async def start_test_encode(payload: TestEncodeRequest):
    """Run a test encode on a sample segment. Returns result directly."""
    from backend.test_encode import run_test_encode
    from backend.websocket import ws_manager

    # If file_path is a folder, pick the largest file in it
    test_file = payload.file_path
    if test_file.endswith("/"):
        db_t = await connect_db()
        try:
            under_sql, under_params = prefix_clause([test_file])
            async with db_t.execute(
                f"SELECT file_path FROM scan_results WHERE {under_sql} "
                "AND +removed_from_list = 0 ORDER BY file_size DESC LIMIT 1",
                under_params,
            ) as cur:
                row = await cur.fetchone()
                if row:
                    test_file = row["file_path"]
                else:
                    raise ApiError(status_code=400, detail="No files found in folder", code="jobs.noFilesInFolder")
        finally:
            await db_t.close()

    # The job's settings, as Add to Queue would give them (v0.10.0): the
    # file's rule, then the dialog's encoder / quality / preset, then the
    # defaults — with the encoder swapped for one this host can run, as the
    # local worker does.
    import asyncio
    from backend.converter import get_live_encoding_settings
    from backend.encoder_caps import detect_encoders, resolve_node_encoder
    from backend.rule_resolver import resolve_rules_for_batch
    rule = (await resolve_rules_for_batch([test_file])).get(test_file) or {}
    encoder = payload.encoder or rule.get("encoder") or \
        (await get_live_encoding_settings()).get("default_encoder") or "nvenc"
    caps = (await asyncio.to_thread(detect_encoders)).available
    encoder = resolve_node_encoder(encoder, caps) or encoder
    overrides: dict = {}
    if encoder == "nvenc":
        cq = payload.cq if payload.cq is not None else rule.get("nvenc_cq")
        if cq is not None:
            overrides["override_cq"] = cq
        if payload.preset or rule.get("nvenc_preset"):
            overrides["override_preset"] = payload.preset or rule.get("nvenc_preset")
    elif encoder == "libx265":
        # The dialog's slider is the CRF for libx265.
        crf = payload.cq if payload.cq is not None else rule.get("libx265_crf")
        if crf is not None:
            overrides["override_crf"] = crf
        if payload.preset or rule.get("libx265_preset"):
            overrides["override_libx265_preset"] = payload.preset or rule.get("libx265_preset")
    if rule.get("target_resolution"):
        overrides["override_target_resolution"] = rule["target_resolution"]

    try:
        result = await run_test_encode(
            file_path=test_file,
            encoder=encoder,
            cq=payload.cq or 20,
            preset=payload.preset or "p6",
            sample_seconds=payload.sample_seconds,
            ws_manager=ws_manager,
            job_overrides=overrides,
        )
        return result
    except Exception as exc:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/test-encode/{task_id}")
async def get_test_encode(task_id: str):
    """Get status/result of a test encode task."""
    from backend.test_encode import get_task
    task = get_task(task_id)
    if task is None:
        raise ApiError(status_code=404, detail="Task not found", code="jobs.taskNotFound")
    return task


@router.get("/vmaf-status")
async def vmaf_status():
    """Check if VMAF is available in the installed ffmpeg."""
    from backend.test_encode import check_vmaf_available
    available = await check_vmaf_available()
    return {"vmaf_available": available}


# Module-level state for the remeasure background task. We keep a single
# in-flight task at a time — re-running while one's already going is almost
# always an accident, and the second run wouldn't have anything new to look
# at anyway.
_remeasure_task: dict = {"running": False, "started_at": None}


@router.get("/vmaf-remeasure/status")
async def vmaf_remeasure_status():
    """Report whether a remeasure pass is currently running, plus a count
    of jobs that would be candidates for remeasure right now."""
    from backend.database import connect_db
    db = await connect_db()
    try:
        # Candidates: completed jobs with a score that's either flagged
        # uncertain OR landed below "Excellent" tier (≤92). Also need a
        # post-rename file (file_path) and a separate pre-rename source
        # (original_file_path) — without both, we have nothing to compare.
        async with db.execute(
            "SELECT COUNT(*) AS n FROM jobs "
            "WHERE status='completed' AND vmaf_score IS NOT NULL "
            "  AND (vmaf_uncertain = 1 OR vmaf_score < 93) "
            "  AND original_file_path IS NOT NULL "
            "  AND original_file_path <> file_path"
        ) as cur:
            row = await cur.fetchone()
        candidates = (row["n"] if row else 0)
    finally:
        await db.close()
    return {
        "running": _remeasure_task["running"],
        "started_at": _remeasure_task["started_at"],
        "candidates": candidates,
    }


@router.post("/vmaf-remeasure")
async def start_vmaf_remeasure():
    """Re-run VMAF on completed jobs whose recorded score is suspect.

    Iterates jobs flagged `vmaf_uncertain=1` or scored below 93 (the
    "Excellent" tier cut), provided both the original (pre-rename) file
    and the encoded (post-rename) file still exist on disk. If the user
    deletes originals after conversion (the common default), this skips
    those jobs — there's nothing to compare against without re-encoding.

    Returns immediately with the candidate count; progress events stream
    over the websocket as `{type: "vmaf_remeasure_progress", ...}`.
    """
    if _remeasure_task["running"]:
        raise ApiError(409, "A VMAF re-measure pass is already running.", code="jobs.vmafRemeasureRunning")

    from backend.database import connect_db
    db = await connect_db()
    try:
        async with db.execute(
            "SELECT id, file_path, original_file_path, vmaf_score, vmaf_uncertain "
            "FROM jobs "
            "WHERE status='completed' AND vmaf_score IS NOT NULL "
            "  AND (vmaf_uncertain = 1 OR vmaf_score < 93) "
            "  AND original_file_path IS NOT NULL "
            "  AND original_file_path <> file_path "
            "ORDER BY id DESC"
        ) as cur:
            candidate_rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()

    if not candidate_rows:
        return {"started": False, "total": 0, "message": "No remeasure candidates."}

    import asyncio as _asyncio
    from backend.websocket import ws_manager
    from datetime import datetime, timezone

    async def _run_remeasure_pass(rows: list[dict]) -> None:
        from backend.converter import remeasure_vmaf
        _remeasure_task["running"] = True
        _remeasure_task["started_at"] = datetime.now(timezone.utc).isoformat()
        total = len(rows)
        rescued = 0
        skipped = 0
        unchanged = 0
        try:
            for idx, row in enumerate(rows, start=1):
                job_id = row["id"]
                src = row["original_file_path"]
                dst = row["file_path"]
                file_name = (dst or src or "").rsplit("/", 1)[-1]
                old_score = row["vmaf_score"]
                await ws_manager.broadcast({
                    "type": "vmaf_remeasure_progress",
                    "done": idx - 1, "total": total,
                    "current_file": file_name,
                    "stage": "starting",
                })
                try:
                    res = await remeasure_vmaf(src, dst)
                except Exception as exc:
                    print(f"[REMEASURE] job {job_id} crashed: {exc}", flush=True)
                    skipped += 1
                    continue

                if res.get("error"):
                    print(f"[REMEASURE] job {job_id} skipped — {res['error']}", flush=True)
                    skipped += 1
                else:
                    new_score = res["score"]
                    new_uncertain = res["uncertain"]
                    db2 = await connect_db()
                    try:
                        await db2.execute(
                            "UPDATE jobs SET vmaf_score = ?, vmaf_uncertain = ? WHERE id = ?",
                            (new_score, 1 if new_uncertain else 0, job_id),
                        )
                        await db2.execute(
                            "UPDATE scan_results SET vmaf_score = ?, vmaf_uncertain = ? WHERE file_path = ?",
                            (new_score, 1 if new_uncertain else 0, dst),
                        )
                        await db2.commit()
                    finally:
                        await db2.close()
                    if new_score is not None and old_score is not None and abs(new_score - old_score) >= 5:
                        rescued += 1
                        print(f"[REMEASURE] job {job_id}: {old_score} → {new_score} (rescued)", flush=True)
                    else:
                        unchanged += 1
                        print(f"[REMEASURE] job {job_id}: {old_score} → {new_score}", flush=True)

                await ws_manager.broadcast({
                    "type": "vmaf_remeasure_progress",
                    "done": idx, "total": total,
                    "current_file": file_name,
                    "stage": "done",
                    "rescued": rescued, "skipped": skipped, "unchanged": unchanged,
                })
        finally:
            _remeasure_task["running"] = False
            _remeasure_task["started_at"] = None
            await ws_manager.broadcast({
                "type": "vmaf_remeasure_complete",
                "total": total,
                "rescued": rescued,
                "skipped": skipped,
                "unchanged": unchanged,
            })
            print(
                f"[REMEASURE] Pass complete: {total} candidates, "
                f"{rescued} rescued, {unchanged} unchanged, {skipped} skipped.",
                flush=True,
            )

    _asyncio.create_task(_run_remeasure_pass(candidate_rows))
    return {"started": True, "total": len(candidate_rows)}


# --- Per-job operations (dynamic {job_id} routes must come AFTER static routes) ---

@router.delete("/{job_id}")
async def remove_job(job_id: int):
    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")
    await _queue.remove_job(job_id)
    return {"status": "removed", "job_id": job_id}


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: int):
    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")
    await _queue.update_status(job_id, "cancelled")
    return {"status": "cancelled", "job_id": job_id}


@router.post("/{job_id}/retry")
async def retry_job(job_id: int):
    """Retry a failed job.

    Special handling: if the file on disk has been renamed since the job was
    recorded (a previous partial conversion: x264 → x265 on disk succeeded but
    a later step failed), the conversion is effectively done. Don't re-run it
    — repoint scan_results to the new file, mark the original job completed,
    and return a note so the frontend can tell the user to rescan if they still
    want tracks removed.
    """
    import os as _os
    from backend.converter import rename_x264_to_x265

    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")

    # Fetch job to check its file_path
    db = await connect_db()
    try:
        async with db.execute(
            "SELECT file_path, job_type, audio_tracks_to_remove, subtitle_tracks_to_remove, "
            "status, finalized_at FROM jobs WHERE id = ?", (job_id,),
        ) as cur:
            row = await cur.fetchone()
    finally:
        await db.close()

    # Retrying a running or completed job ran it again — on a conversion, a
    # re-encode of the already-converted file. Same for a job whose output
    # replaced the original before a later step failed (v0.10.0).
    if row and row["status"] not in ("failed", "cancelled"):
        raise ApiError(status_code=400, detail=f"Cannot retry job with status '{row['status']}'",
                       code="jobs.cannotRetryStatus", params={"status": row["status"]})
    if row and row["finalized_at"]:
        raise ApiError(status_code=409,
                       detail="This job's output already replaced the original; queue a new job instead",
                       code="jobs.retryAlreadyReplaced")

    if row:
        fp = row["file_path"]
        if fp and not _os.path.exists(fp):
            # Try common renames: x264 → x265 in the filename
            candidates = []
            try:
                d = _os.path.dirname(fp)
                orig_name = _os.path.basename(fp)
                renamed_name = rename_x264_to_x265(orig_name)
                if renamed_name != orig_name:
                    candidates.append(_os.path.join(d, renamed_name))
            except Exception:
                pass

            for candidate in candidates:
                if _os.path.exists(candidate):
                    # File was already converted in a previous run. Mark this job
                    # completed (conversion part done), update scan_results to the
                    # new path, and return a note so the user can decide whether
                    # to rescan for track-removal.
                    from datetime import datetime, timezone
                    now = datetime.now(timezone.utc).isoformat()
                    try:
                        new_size = _os.path.getsize(candidate)
                    except OSError:
                        new_size = None

                    had_track_removal = False
                    try:
                        import json as _json
                        a = _json.loads(row["audio_tracks_to_remove"] or "[]")
                        s = _json.loads(row["subtitle_tracks_to_remove"] or "[]")
                        had_track_removal = bool(a) or bool(s)
                    except Exception:
                        pass

                    db2 = await connect_db()
                    try:
                        # Mark the failed job as completed (conversion happened)
                        await db2.execute(
                            "UPDATE jobs SET status = 'completed', completed_at = ?, "
                            "file_path = ?, error_log = NULL, error_key = NULL, "
                            "error_params = NULL WHERE id = ?",
                            (now, candidate, job_id),
                        )
                        # Update scan_results to point to the new file
                        if new_size:
                            await db2.execute(
                                "UPDATE scan_results SET file_path = ?, file_size = ?, "
                                "video_codec = 'hevc', needs_conversion = 0, converted = 1 "
                                "WHERE file_path = ?",
                                (candidate, new_size, fp),
                            )
                        else:
                            await db2.execute(
                                "UPDATE scan_results SET file_path = ?, "
                                "video_codec = 'hevc', needs_conversion = 0, converted = 1 "
                                "WHERE file_path = ?",
                                (candidate, fp),
                            )
                        await db2.commit()
                        print(f"[RETRY] Job {job_id}: file was already converted to {candidate} in a prior run — marking completed", flush=True)
                    finally:
                        await db2.close()

                    if had_track_removal:
                        msg = ("The file was already converted in a previous run. "
                               "Marked the job as completed and updated the path. "
                               "To remove the tracks, rescan the folder and queue a new audio-cleanup job.")
                    else:
                        msg = "The file was already converted in a previous run. Marked the job as completed."
                    return {"status": "completed", "job_id": job_id, "message": msg, "new_path": candidate}

    # v0.7.21: escalate a type='audio' retry to 'combined' when the source
    # still needs video conversion.
    #
    # Background: when a combined convert+cleanup job's video re-encode is
    # discarded (e.g. NVENC crash + software-decode retry left a larger
    # output, or the encoded file would have been bigger than the original),
    # queue.py spawns an audio-only follow-up that does just the track
    # cleanup on the unchanged h264 source. If that follow-up fails (e.g.
    # the v0.7.18 mov_text issue) and the user clicks "retry", the old
    # code re-ran the SAME job_type — so retry of an audio-only job did
    # only sub-cleanup and left the file h264, even though the user
    # expected the full h265 convert.
    #
    # Heuristic: if the source file still needs_conversion (per its
    # scan_results row), the user intent on retry is the full convert.
    # Escalate the job in place. Audio-only retries on already-h265
    # sources stay as-is (legitimate track-cleanup use case).
    escalated = False
    # v0.9.41: a pure remux (no track removal) — e.g. an "apply detected
    # language" remux-to-mkv on an AVI — must NOT escalate to a re-encode on
    # retry. Escalating defeats the point and re-encoding old SD AVIs bloats
    # them (that's why they landed in remux, not convert). Only a cleanup
    # audio job (real track removal) escalates.
    import json as _rjson
    def _nonempty_json_list(v) -> bool:
        try:
            return bool(_rjson.loads(v)) if v else False
        except Exception:
            return False
    _had_removal = (_nonempty_json_list(row["audio_tracks_to_remove"])
                    or _nonempty_json_list(row["subtitle_tracks_to_remove"])) if row else False
    if row and row["job_type"] == "audio" and row["file_path"] and _had_removal:
        fp = row["file_path"]
        if _os.path.exists(fp):
            db_check = await connect_db()
            try:
                async with db_check.execute(
                    "SELECT needs_conversion FROM scan_results WHERE file_path = ?",
                    (fp,),
                ) as cur:
                    sr = await cur.fetchone()
                if sr and sr["needs_conversion"]:
                    await db_check.execute(
                        "UPDATE jobs SET job_type = 'combined', status = 'pending', "
                        "error_log = NULL, error_key = NULL, error_params = NULL WHERE id = ?",
                        (job_id,),
                    )
                    await db_check.commit()
                    escalated = True
                    print(
                        f"[RETRY] Escalated job {job_id} from audio → combined "
                        f"(source still needs h265 convert): {fp}",
                        flush=True,
                    )
            finally:
                await db_check.close()

    if not escalated:
        await _queue.update_status(job_id, "pending")
    return {
        "status": "pending",
        "job_id": job_id,
        **({"escalated": "audio→combined"} if escalated else {}),
    }


@router.post("/clear-completed")
async def clear_completed():
    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")
    await _queue.clear_completed()
    return {"status": "cleared"}


@router.post("/clear-pending")
async def clear_pending():
    if _queue is None:
        raise ApiError(status_code=503, detail="Queue not initialized", code="queue.notInitialized")
    await _queue.clear_pending()
    return {"status": "cleared"}


# --- Undo / Recent Conversions ---

@router.get("/recent-conversions")
async def recent_conversions(limit: int = 20):
    """Return last N completed conversions that have backup files available."""
    import os
    db = await connect_db()
    try:
        async with db.execute(
            """SELECT id, file_path, original_file_path, backup_path, space_saved,
                      original_size, completed_at, job_type, encoder, nvenc_cq, nvenc_preset
               FROM jobs
               WHERE status = 'completed' AND backup_path IS NOT NULL
                 AND job_type IN ('convert', 'combined')
               ORDER BY completed_at DESC LIMIT ?""",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        results = []
        for row in rows:
            bp = row["backup_path"]
            results.append({
                **dict(row),
                "file_name": (row["file_path"] or "").rsplit("/", 1)[-1],
                "backup_exists": os.path.exists(bp) if bp else False,
            })
        return results
    finally:
        await db.close()


def _restore_moves(backup, original) -> list:
    """(from, to) renames that put a converted file's original back.

    original_file_path is the file itself or, for a folder disc, the marker
    inside it (.../BDMV/index.bdmv) — whose backup is a folder: BDMV/ with
    the CERTIFICATE/AACS folders backed up beside it
    (converter._dispose_disc_source), or a whole release folder (made by an
    early v0.10.0 development build). Undo used to rename that folder onto
    the marker path (v0.10.0)."""
    if not backup.is_dir():
        return [(backup, original)]
    disc_dir = original.parent
    if backup.name != disc_dir.name:
        return [(backup, disc_dir.parent)]  # the release folder holding the disc
    from backend.converter import _DISC_DIRS
    # Only this disc's own companions: a backup folder can also hold another
    # disc (one made before v0.10.0 kept slots apart), whose folders must stay.
    companions = next((dirs[1:] for dirs in _DISC_DIRS.values() if dirs[0] == disc_dir.name.upper()), ())
    moves = [(backup, disc_dir)]
    for sibling in sorted(backup.parent.iterdir()):
        if (sibling != backup and sibling.is_dir() and sibling.name.upper() in companions
                and not (disc_dir.parent / sibling.name).exists()):
            moves.append((sibling, disc_dir.parent / sibling.name))
    return moves


@router.post("/{job_id}/undo")
async def undo_conversion(job_id: int):
    """Restore original file from backup, reverting a conversion."""
    import os
    from pathlib import Path

    db = await connect_db()
    try:
        async with db.execute(
            "SELECT file_path, original_file_path, backup_path, status, job_type FROM jobs WHERE id = ?",
            (job_id,),
        ) as cur:
            job = await cur.fetchone()
        if not job:
            raise ApiError(status_code=404, detail="Job not found", code="jobs.notFound")
        if job["status"] not in ("completed", "reverted"):
            raise ApiError(status_code=400, detail=f"Cannot undo job with status '{job['status']}'", code="jobs.cannotUndoStatus", params={"status": job["status"]})

        backup_path = job["backup_path"]
        if not backup_path or not os.path.exists(backup_path):
            raise ApiError(status_code=400, detail="Backup file not found on disk", code="jobs.backupNotFound")

        converted_path = job["file_path"]
        original_path = job["original_file_path"] or job["file_path"]

        # Put the original back first and only then remove the converted file
        # — it used to be deleted up front, so a failed restore left neither
        # in the library (v0.10.0).
        moves = _restore_moves(Path(backup_path), Path(original_path))
        target = moves[0][1]
        converted = Path(converted_path) if converted_path else None
        in_the_way = converted is not None and converted == target and converted.exists()
        if os.path.lexists(target) and not in_the_way:
            raise ApiError(status_code=409, detail=f"Something already exists where the original goes: {target}",
                           code="jobs.undoTargetExists", params={"path": str(target)})
        aside = converted.with_name(converted.name + ".undo") if in_the_way else None
        try:
            if aside is not None:
                converted.rename(aside)  # same-name conversion: free the name
            target.parent.mkdir(parents=True, exist_ok=True)
            Path(backup_path).rename(target)
        except OSError as exc:
            if aside is not None and aside.exists() and not converted.exists():
                aside.rename(converted)
            raise ApiError(status_code=500, detail=f"Could not restore the original: {exc}",
                           code="jobs.undoFailed", params={"error": str(exc)})
        for src, dst in moves[1:]:  # CERTIFICATE / AACS / AUDIO_TS / JACKET_P
            try:
                src.rename(dst)
            except OSError as exc:
                print(f"[UNDO] Could not restore {src.name}: {exc}", flush=True)
        print(f"[UNDO] Restored original: {backup_path} → {target}", flush=True)
        if aside is not None:
            aside.unlink()
        elif converted is not None and converted.exists():
            converted.unlink()
        print(f"[UNDO] Deleted converted file: {converted_path}", flush=True)

        # Update job status
        await db.execute(
            "UPDATE jobs SET status = 'reverted' WHERE id = ?", (job_id,)
        )

        # Reset scan_results for this file (a disc row is a disc again).
        disc_type = {"index.bdmv": "bdmv", "video_ts.ifo": "dvd"}.get(Path(original_path).name.lower())
        await db.execute(
            """UPDATE scan_results SET converted = 0, needs_conversion = 1,
                   video_codec = 'h264', file_path = ?, file_size = ?, disc_type = ?
               WHERE file_path = ? OR file_path = ?""",
            (original_path, os.path.getsize(original_path), disc_type, converted_path, original_path),
        )
        await db.commit()

        try:
            from backend.file_events import log_event, EVENT_REVERTED
            await log_event(original_path, EVENT_REVERTED, "Restored original from backup", {"job_id": job_id, "converted_path": converted_path},
                            summary_key="restoredFromBackup")
        except Exception:
            pass

        return {
            "status": "reverted",
            "restored_path": original_path,
            "size": os.path.getsize(original_path),
        }
    finally:
        await db.close()


@router.get("/{job_id}/log")
async def get_job_log(job_id: int):
    """Return detailed conversion log for a job."""
    db = await connect_db()
    try:
        async with db.execute(
            """SELECT ffmpeg_command, ffmpeg_log, error_log, encoding_stats, vmaf_score,
                      space_saved, original_size, started_at, completed_at,
                      encoder, nvenc_preset, nvenc_cq, audio_codec, audio_bitrate,
                      libx265_crf, target_resolution, status, error_key, error_params
               FROM jobs WHERE id = ?""",
            (job_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            raise ApiError(status_code=404, detail="Job not found", code="jobs.notFound")
        result = dict(row)
        if result.get("error_params"):
            try:
                result["error_params"] = json.loads(result["error_params"])
            except (json.JSONDecodeError, ValueError):
                result["error_params"] = None
        # Parse encoding_stats JSON
        if result.get("encoding_stats"):
            try:
                result["encoding_stats"] = json.loads(result["encoding_stats"])
            except (json.JSONDecodeError, ValueError):
                pass
        return result
    finally:
        await db.close()


# --- Estimation ---

class EstimateRequest(BaseModel):
    file_paths: list[str]
    override_rules: bool = False
    filter: str = "all"
    # v0.10.0: the Add to Queue dialog's encoder (None = the default), for
    # the "what will happen" panel.
    encoder: str | None = None
    # Encoding overrides (same as queue request)
    nvenc_cq_override: int | None = None
    libx265_crf_override: int | None = None
    force_reencode: bool = False


# v0.6.7: hoisted into backend.encoding_estimates so the scanner /
# watcher / file-detail panel can share the same curve. Kept the local
# alias so existing call sites in this module need no churn.
from backend.encoding_estimates import cq_to_savings_pct as _cq_to_savings_pct


@router.post("/estimate")
async def estimate_jobs(payload: EstimateRequest):
    """Estimate savings for a batch of files without creating jobs."""
    try:
        return await _estimate_jobs_impl(payload)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        print(f"[ESTIMATE] Fatal error: {exc}", flush=True)
        # Return a minimal valid response so the frontend shows *something*
        return {
            "total_selected": len(payload.file_paths),
            "total_files": 0,
            "total_size": 0,
            "estimated_savings": 0,
            "estimated_time_seconds": 0,
            "by_type": {"convert": 0, "audio": 0, "combined": 0},
            "by_source": {},
            "skipped_by_rules": 0,
            "ignored_files": 0,
            "cq": 20,
            "savings_pct": 0,
            "content_profiles": {},
            "resolution_breakdown": {},
            "smart_encoding": False,
            "error": str(exc),
        }


async def _estimate_jobs_impl(payload: EstimateRequest):
    from backend.rule_resolver import resolve_rules_for_batch
    from backend.content_detect import smart_cq, smart_cq_settings
    from backend.resolution import resolution_tier
    import re

    # Selected folders through the active filter.
    file_paths = list(payload.file_paths)
    folder_paths = [p for p in file_paths if p.endswith("/")]
    if folder_paths:
        from backend.routes.scan import _paths_matching
        file_paths = [p for p in file_paths if not p.endswith("/")]
        file_paths += await _paths_matching(payload.filter or "all", folder_paths)

    if payload.override_rules:
        rule_results = {}
    else:
        rule_results = await resolve_rules_for_batch(file_paths)

    db = await connect_db()
    try:
        # Load settings for smart CQ
        from backend.encoding_estimates import QUALITY_KEYS, effective_cq
        from backend.content_detect import SMART_CQ_KEYS
        est_keys = ('backup_original_days', 'trash_original_after_conversion',
                    *SMART_CQ_KEYS, *QUALITY_KEYS)
        est_settings = {}
        async with db.execute(
            f"SELECT key, value FROM settings WHERE key IN ({','.join('?' for _ in est_keys)})", est_keys
        ) as cur:
            for row in await cur.fetchall():
                est_settings[row["key"]] = row["value"]

        global_cq = int(est_settings.get("nvenc_cq", "20"))
        # Savings at the global setting use the default encoder's quality on
        # the CQ scale (v0.10.0: it was always the NVENC CQ).
        global_savings_cq = effective_cq(est_settings)

        # Read default encoder for per-file time estimate
        async with db.execute("SELECT value FROM settings WHERE key = 'default_encoder'") as cur:
            _enc_row = await cur.fetchone()
        default_encoder = (_enc_row["value"] if _enc_row else "nvenc").lower()
        smart_settings = smart_cq_settings(est_settings)

        # Compute a speed factor: encoding-seconds per content-second, derived from
        # recent completed jobs (last 30 days) so a few slow CPU-encoded outliers
        # in the full history don't skew small-file estimates.
        # Also compute a fallback avg_seconds for files whose duration is unknown.
        async with db.execute(
            "SELECT COALESCE(SUM(total_encode_seconds), 0) as total_secs, "
            "COALESCE(SUM(jobs_completed), 0) as total_jobs FROM daily_stats "
            "WHERE date >= date('now', '-30 days')"
        ) as cur:
            stat_row = await cur.fetchone()
        avg_seconds = (stat_row["total_secs"] / stat_row["total_jobs"]) if stat_row["total_jobs"] > 0 else 600

        # Per-encoder speed factors: encoding-seconds per content-second.
        # Uses jobs.started_at/completed_at directly rather than daily_stats aggregates
        # so we can filter by encoder (GPU vs CPU have very different speeds).
        # Each factor is the MEDIAN ratio across the last ~50 completed jobs of that
        # encoder — more robust than mean against occasional slow outliers.
        def _median(vals: list[float]) -> float:
            if not vals:
                return 0.0
            s = sorted(vals)
            n = len(s)
            return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0

        def _ratio(r) -> float:
            """Encoding-seconds per content-second of one completed job, or 0."""
            # Prefer jobs.encoding_stats JSON which contains the actual ffmpeg
            # encode_seconds + duration (no post-processing overhead in it).
            # Fall back to started_at/completed_at elapsed time if stats are missing.
            enc_secs: float = 0.0
            dur: float = 0.0
            raw_stats = r["encoding_stats"]
            if raw_stats:
                try:
                    st = json.loads(raw_stats)
                    enc_secs = float(st.get("encode_seconds") or 0)
                    dur = float(st.get("duration") or 0)
                except Exception:
                    pass
            if enc_secs <= 0 or dur <= 0:
                try:
                    if r["started_at"] and r["completed_at"]:
                        t0 = datetime.fromisoformat(r["started_at"])
                        t1 = datetime.fromisoformat(r["completed_at"])
                        enc_secs = (t1 - t0).total_seconds()
                        dur = float(r["duration"] or 0)
                except Exception:
                    pass
            return enc_secs / dur if enc_secs > 0 and dur > 0 else 0.0

        # F13 (v0.10.0): one pass over the most recent completed conversions.
        # This ran once per encoder over the whole history, which it walked to
        # the end whenever an encoder had fewer than 50 usable jobs — always
        # the case for the one you don't use: ~115 ms each on 94k jobs.
        _families = {
            "nvenc": ("nvenc", "hevc_nvenc"),
            "libx265": ("libx265", "x265", "cpu"),
        }
        _ratios: dict[str, list[float]] = {f: [] for f in _families}
        try:
            async with db.execute(
                "SELECT LOWER(j.encoder) AS encoder, j.encoding_stats, j.started_at, j.completed_at, sr.duration "
                "FROM (SELECT encoder, encoding_stats, started_at, completed_at, file_path FROM jobs "
                "      WHERE status = 'completed' AND job_type IN ('convert', 'combined') "
                "      ORDER BY completed_at DESC LIMIT 2000) j "
                "JOIN scan_results sr ON sr.file_path = j.file_path "
                "ORDER BY j.completed_at DESC"
            ) as cur:
                for r in await cur.fetchall():
                    family = next((f for f, names in _families.items() if r["encoder"] in names), None)
                    if family is None or len(_ratios[family]) >= 50:  # 50 data points is plenty
                        continue
                    ratio = _ratio(r)
                    if ratio > 0:
                        _ratios[family].append(ratio)
        except Exception as exc:
            print(f"[ESTIMATE] Speed factor query failed (using defaults): {exc}", flush=True)

        def _speed_factor(family: str) -> float:
            med = _median(_ratios[family])
            # Clamp: [0.02 (50x realtime), 3.0 (3x slower than realtime)]
            return max(0.02, min(3.0, med)) if med > 0 else 0.0

        nvenc_speed = _speed_factor("nvenc")
        libx265_speed = _speed_factor("libx265")

        # Fallbacks: typical realistic defaults
        if nvenc_speed == 0.0:
            nvenc_speed = 0.08  # ~12x realtime for NVENC p3 at 1080p
        if libx265_speed == 0.0:
            libx265_speed = 0.4  # ~2.5x realtime for libx265 medium on a decent CPU

        total_files = 0
        total_size = 0
        total_est_time = 0.0  # accumulates per-file encoding-seconds (sequential sum)
        estimated_savings = 0
        by_type = {"convert": 0, "audio": 0, "combined": 0}
        by_source = {}
        content_profiles: dict[str, dict] = {}
        resolution_breakdown = {"4k": 0, "1080p": 0, "720p": 0, "sd": 0}
        removals: dict[str, dict[str, int]] = {"audio": {}, "subtitles": {}}
        skipped = 0
        ignored_count = 0
        # Per-file CQ values actually used in the savings calculation. The
        # response returns the median of these as the "representative" CQ,
        # which the modal's slider initializes to. Pre-v0.3.98 the response
        # returned `payload.nvenc_cq_override or global_cq` instead — for
        # the no-override case this was the user's global default, NOT the
        # smart/content-aware CQ that drove the savings number, so the
        # slider showed e.g. 27 while the savings reflected CQ 20. Once the
        # user touched the slider, the override path produced a number
        # matching the slider — making the initial reading look broken.
        file_cqs: list[int] = []

        # Check which files are in the ignored_files table — batched
        ignored_set = set()
        scan_rows: dict[str, dict] = {}
        if file_paths:
            # Chunk IN clauses to stay under SQLite's 999-variable limit
            CHUNK = 900
            try:
                for i in range(0, len(file_paths), CHUNK):
                    chunk = file_paths[i:i + CHUNK]
                    placeholders = ",".join("?" * len(chunk))
                    async with db.execute(
                        f"SELECT file_path FROM ignored_files WHERE file_path IN ({placeholders})",
                        chunk,
                    ) as cur:
                        for r in await cur.fetchall():
                            ignored_set.add(r["file_path"])
            except Exception:
                pass

            # Batch-load scan_results for all file paths (avoids N+1 queries)
            for i in range(0, len(file_paths), CHUNK):
                chunk = file_paths[i:i + CHUNK]
                placeholders = ",".join("?" * len(chunk))
                async with db.execute(
                    f"SELECT file_path, file_size, needs_conversion, audio_tracks_json, "
                    f"subtitle_tracks_json, COALESCE(video_height, 0) as video_height, "
                    f"COALESCE(video_width, 0) as video_width, "
                    f"COALESCE(duration, 0) as duration, native_language, disc_type "
                    f"FROM scan_results WHERE file_path IN ({placeholders})",
                    chunk,
                ) as cur:
                    for r in await cur.fetchall():
                        scan_rows[r["file_path"]] = dict(r)

        print(f"[ESTIMATE] {len(file_paths)} file(s) to estimate, {len(scan_rows)} found in scan_results, {len(ignored_set)} ignored", flush=True)
        if len(file_paths) > 0 and len(scan_rows) == 0:
            print(f"[ESTIMATE] No scan_results found for paths: {file_paths[:5]}", flush=True)

        for fp in file_paths:
            rule = rule_results.get(fp)
            if rule and rule["action"] == "skip":
                skipped += 1
            if fp in ignored_set:
                ignored_count += 1
            skip_conv = rule and rule["action"] == "ignore"

            row = scan_rows.get(fp)
            if not row:
                continue

            # The decision add-from-scan makes (v0.10.0): removals by keep
            # alone (v0.9.99), and the original language's audio moved first.
            audio_remove, sub_remove, has_work = track_work(row)

            # force_reencode overrides both needs_conversion AND skip rules.
            # v0.9.120: a disc (disc_type set) always needs conversion — force it
            # from the stored disc_type so the estimate is right immediately,
            # without a re-scan to refresh needs_conversion. An ignore rule still
            # wins (a disc under skip stays skipped, like the base column does).
            needs_conv = bool(row["needs_conversion"]) or bool(row.get("disc_type")) or payload.force_reencode
            if skip_conv and not payload.force_reencode:
                needs_conv = False

            if needs_conv and has_work:
                jt = "combined"
            elif needs_conv:
                jt = "convert"
            elif has_work:
                jt = "audio"
            elif skip_conv:
                continue
            else:
                jt = "audio"

            total_files += 1
            total_size += row["file_size"]
            by_type[jt] = by_type.get(jt, 0) + 1
            # Tracks this job removes, by language (the "what will happen" panel).
            for kind, column, remove in (("audio", "audio_tracks_json", audio_remove),
                                         ("subtitles", "subtitle_tracks_json", sub_remove)):
                for t in (json.loads(row[column] or "[]") if remove else []):
                    if t.get("stream_index") in remove:
                        lang = (t.get("language") or "und").lower()
                        removals[kind][lang] = removals[kind].get(lang, 0) + 1

            # Per-file time estimate — pick speed factor based on target encoder
            file_dur = float(row.get("duration") or 0)
            if jt == "audio":
                # Remux only — fast, ~30 seconds regardless of content length
                per_file_seconds = 30
            elif file_dur > 0:
                # Determine which encoder this file will use
                target_enc = (rule.get("encoder") if rule else None) or default_encoder
                is_cpu = (target_enc or "").lower() in ("libx265", "x265", "cpu")
                sf = libx265_speed if is_cpu else nvenc_speed
                per_file_seconds = file_dur * sf
            else:
                per_file_seconds = avg_seconds
            total_est_time += per_file_seconds

            # Smart CQ per file for savings estimation
            if needs_conv:
                # The shared classifier (SC-22): width when known, else the
                # height raised by a resolution tag in the path.
                tier = resolution_tier(row["video_width"], row["video_height"], fp) or "sd"
                resolution_breakdown[tier] = resolution_breakdown.get(tier, 0) + 1

                # Determine effective CQ for this file. Modal override wins
                # over everything. Accept both nvenc_cq_override and
                # libx265_crf_override symmetrically — the savings curve is
                # encoder-agnostic so whichever the modal sent works the
                # same. (Pre-v0.3.98 only nvenc_cq_override was checked, so
                # libx265 users moving the CRF slider had no effect on the
                # estimate.)
                file_cq = (
                    payload.nvenc_cq_override
                    if payload.nvenc_cq_override is not None
                    else payload.libx265_crf_override
                )
                savings_cq = None
                if file_cq is None:
                    rule_cq = None
                    if rule:
                        rule_cq = rule.get("nvenc_cq") if rule.get("nvenc_cq") is not None else rule.get("libx265_crf")
                    if rule_cq is not None:
                        file_cq = rule_cq
                    else:
                        # The same per-file CQ Add to Queue gives the job.
                        smart, ctype = smart_cq(fp, row["video_width"], row["video_height"], smart_settings)
                        file_cq = smart if smart is not None else global_cq
                        if smart is None:
                            savings_cq = global_savings_cq
                        if ctype:
                            # Track content profiles
                            if ctype not in content_profiles:
                                content_profiles[ctype] = {"count": 0, "cq": file_cq}
                            content_profiles[ctype]["count"] += 1

                pct = _cq_to_savings_pct(savings_cq if savings_cq is not None else file_cq)
                estimated_savings += int(row["file_size"] * pct)
                file_cqs.append(file_cq)

            # Source type
            name = fp.rsplit("/", 1)[-1].lower()
            src = "Other"
            if re.search(r"blu[\-\s]?ray|bdremux|bdrip", name): src = "Blu-ray"
            elif "web-dl" in name or "webdl" in name: src = "WEB-DL"
            elif "webrip" in name: src = "WEBRip"
            elif "hdtv" in name: src = "HDTV"
            elif "dvd" in name: src = "DVD"
            elif "remux" in name: src = "Remux"
            by_source[src] = by_source.get(src, 0) + 1

        # Get parallel_jobs for time estimate
        async with db.execute("SELECT value FROM settings WHERE key = 'parallel_jobs'") as cur:
            prow = await cur.fetchone()
            parallel = int(prow["value"]) if prow else 1

        # Per-file estimates summed above; divide by parallel for wall-clock estimate
        est_time_seconds = total_est_time / max(1, parallel)

        # Representative CQ: median of the per-file CQs that actually drove
        # the savings calculation (so the modal's slider initializes to a
        # number that matches the savings figure rather than the user's
        # global default). For an explicit override every entry is the
        # same value, so median == override. For batches with mixed
        # content-aware CQs the median is the honest summary. Falls back
        # to global_cq when no qualifying file was processed (all-skip /
        # all-already-x265 batch). v0.3.98+.
        if file_cqs:
            _sorted_cqs = sorted(file_cqs)
            representative_cq = _sorted_cqs[len(_sorted_cqs) // 2]
        else:
            representative_cq = global_cq

        # "What will happen" (v0.10.0): what becomes of the originals and which
        # encoder this server will actually run (an NVENC default on a host
        # without NVIDIA runs on its best encoder instead).
        import asyncio
        from backend.encoder_caps import detect_encoders, resolve_node_encoder
        requested = (payload.encoder or est_settings.get("default_encoder") or "nvenc").lower()
        try:
            caps = (await asyncio.to_thread(detect_encoders)).available
            runs_here = resolve_node_encoder(requested, caps) or requested
        except Exception:
            runs_here = requested
        try:
            backup_days = int(est_settings.get("backup_original_days") or 0)
        except ValueError:
            backup_days = 0
        if backup_days > 0:
            originals = {"action": "keep", "days": backup_days}
        elif str(est_settings.get("trash_original_after_conversion", "false")).lower() == "true":
            originals = {"action": "trash"}
        else:
            originals = {"action": "delete"}

        return {
            "total_selected": len(file_paths),
            "total_files": total_files,
            "total_size": total_size,
            "estimated_savings": estimated_savings,
            "removals": removals,
            "originals": originals,
            "encoder": {"requested": requested, "runs_here": runs_here},
            "estimated_time_seconds": round(est_time_seconds),
            "by_type": by_type,
            "by_source": by_source,
            "skipped_by_rules": skipped,
            "ignored_files": ignored_count,
            "cq": representative_cq,
            "savings_pct": round((estimated_savings / total_size * 100) if total_size > 0 else 0),
            "content_profiles": content_profiles,
            "resolution_breakdown": resolution_breakdown,
            "smart_encoding": smart_settings["content_detect"] or smart_settings["resolution_aware"],
        }
    finally:
        await db.close()


# --- Failed count (for sidebar badge) ---

@router.get("/failed-count")
async def get_failed_count():
    from backend.database import connect_db
    db = await connect_db()
    try:
        async with db.execute("SELECT COUNT(*) as c FROM jobs WHERE status = 'failed'") as cur:
            row = await cur.fetchone()
            return {"count": row["c"] if row else 0}
    finally:
        await db.close()


# --- Export ---

@router.get("/export/csv")
async def export_csv():
    """Export completed jobs as CSV."""
    from fastapi.responses import StreamingResponse
    import csv, io

    db = await connect_db()
    try:
        async with db.execute(
            """SELECT id, file_path, job_type, encoder, nvenc_preset, nvenc_cq,
                      space_saved, original_size, created_at, started_at, completed_at
               FROM jobs WHERE status = 'completed' ORDER BY completed_at DESC"""
        ) as cur:
            rows = await cur.fetchall()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["id", "file_path", "job_type", "encoder", "preset", "cq",
                         "space_saved_bytes", "original_size_bytes", "created_at", "started_at", "completed_at"])
        for r in rows:
            writer.writerow([r["id"], r["file_path"], r["job_type"], r["encoder"],
                             r["nvenc_preset"], r["nvenc_cq"], r["space_saved"], r["original_size"],
                             r["created_at"], r["started_at"], r["completed_at"]])

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=shrinkerr-jobs-{today}.csv"},
        )
    finally:
        await db.close()


@router.get("/export/json")
async def export_json():
    """Export completed jobs as JSON."""
    from fastapi.responses import StreamingResponse

    db = await connect_db()
    try:
        async with db.execute(
            """SELECT id, file_path, job_type, encoder, nvenc_preset, nvenc_cq,
                      space_saved, original_size, created_at, started_at, completed_at
               FROM jobs WHERE status = 'completed' ORDER BY completed_at DESC"""
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return StreamingResponse(
            iter([json.dumps(rows, indent=2)]),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename=shrinkerr-jobs-{today}.json"},
        )
    finally:
        await db.close()
