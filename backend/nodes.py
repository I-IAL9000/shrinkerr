"""Distributed worker node management.

Tracks registered worker nodes, handles job assignment to remote workers,
manages heartbeats, and releases stale assignments.
"""
from __future__ import annotations

import asyncio
import json
import platform
import shutil
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite

from backend.database import DB_PATH


class NodeManager:
    """Manages the worker_nodes table and job assignment for distributed transcoding."""

    def __init__(self):
        self._cancel_flags: set[int] = set()  # job_ids with cancel requested
        self._requeue_on_cancel: set[int] = set()  # job_ids that should return to pending (not failed)
        # Per-node live metrics. Workers POST these ~every 5s. Purely in
        # memory — metrics are volatile, no reason to persist each sample to
        # disk. Shape: { node_id: { "metrics": <all-metrics dict>, "received_at": epoch } }
        self._node_metrics: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Live metrics (CPU / RAM / GPU / disk / network) — volatile
    # ------------------------------------------------------------------

    def update_metrics(self, node_id: str, metrics: dict) -> None:
        """Store the latest metrics sample for a node."""
        import time as _time
        self._node_metrics[node_id] = {
            "metrics": metrics,
            "received_at": _time.time(),
        }

    def get_metrics(self, node_id: str, max_age_seconds: float = 60.0) -> dict | None:
        """Return the latest metrics for a node, or None if missing/stale."""
        import time as _time
        entry = self._node_metrics.get(node_id)
        if not entry:
            return None
        age = _time.time() - entry.get("received_at", 0)
        if age > max_age_seconds:
            return None
        return {
            "metrics": entry["metrics"],
            "age_seconds": round(age, 2),
        }

    def get_all_metrics(self, max_age_seconds: float = 60.0) -> dict[str, dict]:
        """Return a {node_id: {metrics, age_seconds}} map of all fresh nodes."""
        result: dict[str, dict] = {}
        for node_id in list(self._node_metrics.keys()):
            sample = self.get_metrics(node_id, max_age_seconds=max_age_seconds)
            if sample is not None:
                result[node_id] = sample
        return result

    async def _db(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=30000")
        return db

    # ------------------------------------------------------------------
    # Per-node auth tokens (v0.3.30+)
    # ------------------------------------------------------------------
    # Each registered worker gets a random token on first successful
    # heartbeat. Subsequent calls against `/api/nodes/*` must present that
    # token in `X-Node-Token` or the server rejects the call with 401.
    # The shared API key still gates the HTTP surface (via the auth
    # middleware in backend/main.py); the per-node token prevents anyone
    # *who holds that API key* from impersonating another registered node.
    #
    # Stored plaintext in the `worker_nodes.token` column — it's a shared
    # secret between the server and a single worker, same threat model as
    # `session_secret`. Compared with `hmac.compare_digest` on every call
    # (fast, constant-time; a per-request bcrypt verify would be too slow
    # for heartbeat/progress endpoints hit every few seconds during encodes).

    async def issue_token(self, node_id: str) -> str:
        """Generate a new per-node token, persist it, and return the plain
        value. Caller (the heartbeat handler) ships it back to the worker
        in the response body so the worker can persist it locally."""
        import secrets
        from datetime import datetime, timezone
        token = secrets.token_hex(24)
        now = datetime.now(timezone.utc).isoformat()
        db = await self._db()
        try:
            await db.execute(
                "UPDATE worker_nodes SET token = ?, token_issued_at = ? WHERE id = ?",
                (token, now, node_id),
            )
            await db.commit()
        finally:
            await db.close()
        return token

    async def get_stored_token(self, node_id: str) -> Optional[str]:
        """Return the token recorded for this node, or None if never issued
        (e.g. pre-v0.3.30 upgrade, or just rotated and awaiting bootstrap)."""
        db = await self._db()
        try:
            async with db.execute(
                "SELECT token FROM worker_nodes WHERE id = ?", (node_id,)
            ) as cur:
                row = await cur.fetchone()
                if row is None:
                    return None
                return row["token"] or None
        finally:
            await db.close()

    async def validate_token(self, node_id: str, supplied_token: str) -> bool:
        """Constant-time compare `supplied_token` against the stored value.
        Returns False when no token is on file (caller should treat that as
        bootstrap-not-complete rather than auth failure — see route logic)."""
        import hmac
        stored = await self.get_stored_token(node_id)
        if not stored or not supplied_token:
            return False
        return hmac.compare_digest(stored, supplied_token)

    async def clear_token(self, node_id: str) -> None:
        """Admin-triggered token rotation. Next heartbeat from the node
        runs the bootstrap path and receives a fresh token. Any worker
        still holding the old token gets 401 on its next call and drops
        its local copy, triggering re-bootstrap."""
        db = await self._db()
        try:
            await db.execute(
                "UPDATE worker_nodes SET token = NULL, token_issued_at = NULL WHERE id = ?",
                (node_id,),
            )
            await db.commit()
        finally:
            await db.close()

    # ------------------------------------------------------------------
    # Node registration & heartbeat
    # ------------------------------------------------------------------

    async def register_or_update(
        self,
        node_id: str,
        name: str,
        hostname: str = "",
        capabilities: list[str] | None = None,
        path_mappings: list[dict] | None = None,
        ffmpeg_version: str | None = None,
        gpu_name: str | None = None,
        os_info: str | None = None,
        max_jobs: int = 1,
        driver_version: str | None = None,
        nvenc_unavailable_reason: str | None = None,
    ) -> dict:
        """Register a new node or update an existing one. Called on heartbeat."""
        now = datetime.now(timezone.utc).isoformat()
        caps_json = json.dumps(capabilities or [])
        mappings_json = json.dumps(path_mappings or [])

        db = await self._db()
        try:
            # Check if node exists
            async with db.execute("SELECT id, status FROM worker_nodes WHERE id = ?", (node_id,)) as cur:
                exists = await cur.fetchone()

            if exists:
                # Don't reset status from 'error' back to 'online' on heartbeat —
                # error state requires manual reset from the UI
                current_status = exists["status"]
                if current_status == "error":
                    status_expr = "'error'"
                else:
                    status_expr = "CASE WHEN current_job_id IS NOT NULL THEN 'working' ELSE 'online' END"
                # Do NOT update max_jobs on heartbeat — that's a user-controlled
                # setting. Only update hardware/capability info + liveness.
                await db.execute(
                    f"UPDATE worker_nodes SET name = ?, hostname = ?, capabilities = ?, "
                    f"last_heartbeat = ?, path_mappings = ?, ffmpeg_version = ?, "
                    f"gpu_name = ?, os_info = ?, driver_version = ?, "
                    f"nvenc_unavailable_reason = ?, "
                    f"status = {status_expr} "
                    f"WHERE id = ?",
                    (name, hostname, caps_json, now, mappings_json,
                     ffmpeg_version, gpu_name, os_info,
                     driver_version, nvenc_unavailable_reason, node_id),
                )
            else:
                await db.execute(
                    "INSERT INTO worker_nodes (id, name, hostname, capabilities, status, "
                    "last_heartbeat, registered_at, path_mappings, ffmpeg_version, "
                    "gpu_name, os_info, max_jobs, driver_version, nvenc_unavailable_reason) "
                    "VALUES (?, ?, ?, ?, 'online', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (node_id, name, hostname, caps_json, now, now,
                     mappings_json, ffmpeg_version, gpu_name, os_info, max_jobs,
                     driver_version, nvenc_unavailable_reason),
                )
            await db.commit()

            async with db.execute("SELECT * FROM worker_nodes WHERE id = ?", (node_id,)) as cur:
                row = await cur.fetchone()
            return self._row_to_dict(row) if row else {}
        finally:
            await db.close()

    async def touch_local_heartbeat(self) -> None:
        """Bump last_heartbeat + status for the built-in local worker.

        Called periodically so the Nodes page doesn't show the server
        going stale the way a crashed remote worker would. Distinct from
        register_local_node which also re-detects capabilities (expensive).
        """
        now = datetime.now(timezone.utc).isoformat()
        db = await self._db()
        try:
            # Don't clobber 'error' state on a routine heartbeat — same rule
            # we apply for remote worker heartbeats.
            await db.execute(
                "UPDATE worker_nodes SET last_heartbeat = ?, "
                "status = CASE "
                "    WHEN status = 'error' THEN 'error' "
                "    WHEN current_job_id IS NOT NULL THEN 'working' "
                "    ELSE 'online' "
                "END "
                "WHERE id = 'local'",
                (now,),
            )
            await db.commit()
        finally:
            await db.close()

    async def register_local_node(self) -> None:
        """Register the built-in local worker on server startup."""
        # Auto-detect capabilities. Detect the GPU FIRST so capability
        # detection can refuse to claim NVENC when no NVIDIA device is
        # present (the Mac false-positive fix).
        gpu_name = await self._detect_gpu()
        driver_version = await self._detect_driver_version()
        capabilities, nvenc_reason = await self._detect_capabilities(gpu_name=gpu_name)
        ffmpeg_ver = await self._detect_ffmpeg_version()

        # Default max_jobs = server's parallel_jobs setting (on first registration only)
        db = await self._db()
        try:
            async with db.execute("SELECT value FROM settings WHERE key = 'parallel_jobs'") as cur:
                row = await cur.fetchone()
                try:
                    default_max = int(row["value"]) if row else 2
                except (ValueError, TypeError):
                    default_max = 2
            # Seed default_encoder based on detected local capabilities when no
            # value has ever been stored — so CPU-only boxes don't land on
            # NVENC as the default. INSERT OR IGNORE preserves any prior user choice.
            seed_encoder = "nvenc" if "nvenc" in capabilities else "libx265"
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES ('default_encoder', ?)",
                (seed_encoder,),
            )
            await db.commit()
        finally:
            await db.close()

        await self.register_or_update(
            node_id="local",
            name="Local",
            hostname=platform.node(),
            capabilities=capabilities,
            ffmpeg_version=ffmpeg_ver,
            gpu_name=gpu_name,
            os_info=f"{platform.system()} {platform.release()}",
            max_jobs=default_max,  # only used on first registration — user controls after that
            driver_version=driver_version,
            nvenc_unavailable_reason=nvenc_reason,
        )
        print(
            f"[NODES] Local node registered: capabilities={capabilities}, gpu={gpu_name}, "
            f"driver={driver_version}, nvenc_reason={nvenc_reason!r}, "
            f"default_max_jobs={default_max}",
            flush=True,
        )

        # Backfill historical stats from the jobs table. Jobs completed by
        # the local worker have assigned_node_id NULL (the remote-worker
        # request-job flow is the only thing that sets it). Idempotent:
        # each run SETs the stats to the authoritative count rather than
        # incrementing, so re-running never double-counts.
        db = await self._db()
        try:
            async with db.execute(
                "SELECT COUNT(*) AS cnt, "
                "COALESCE(SUM(CASE WHEN space_saved > 0 THEN space_saved ELSE 0 END), 0) AS saved "
                "FROM jobs "
                "WHERE status = 'completed' "
                "AND (assigned_node_id IS NULL OR assigned_node_id = 'local') "
                "AND COALESCE(job_type, 'convert') <> 'health_check'"
            ) as cur:
                row = await cur.fetchone()
            if row:
                cnt = int(row["cnt"] or 0)
                saved = int(row["saved"] or 0)
                await db.execute(
                    "UPDATE worker_nodes SET jobs_completed = ?, total_space_saved = ? "
                    "WHERE id = 'local'",
                    (cnt, saved),
                )
                await db.commit()
                print(f"[NODES] Local node stats backfilled: {cnt} completed, {saved / (1024**3):.2f} GB saved", flush=True)
        except Exception as exc:
            print(f"[NODES] Local stats backfill failed (non-fatal): {exc}", flush=True)
        finally:
            await db.close()

    # ------------------------------------------------------------------
    # Node queries
    # ------------------------------------------------------------------

    async def get_all_nodes(self) -> list[dict]:
        db = await self._db()
        try:
            async with db.execute(
                "SELECT * FROM worker_nodes ORDER BY CASE WHEN id = 'local' THEN 0 ELSE 1 END, name"
            ) as cur:
                rows = await cur.fetchall()
            nodes = []
            for row in rows:
                d = self._row_to_dict(row)
                # Attach current job filename if working
                if d.get("current_job_id"):
                    async with db.execute(
                        "SELECT file_path, progress FROM jobs WHERE id = ?",
                        (d["current_job_id"],),
                    ) as jcur:
                        jrow = await jcur.fetchone()
                    if jrow:
                        d["current_job_file"] = jrow["file_path"].rsplit("/", 1)[-1]
                        d["current_job_progress"] = jrow["progress"] or 0
                nodes.append(d)
            return nodes
        finally:
            await db.close()

    async def get_node(self, node_id: str) -> Optional[dict]:
        db = await self._db()
        try:
            async with db.execute("SELECT * FROM worker_nodes WHERE id = ?", (node_id,)) as cur:
                row = await cur.fetchone()
            return self._row_to_dict(row) if row else None
        finally:
            await db.close()

    # ------------------------------------------------------------------
    # Job assignment
    # ------------------------------------------------------------------

    async def assign_job_to_node(self, node_id: str, job: dict) -> dict:
        """Mark a job as running and assigned to a specific node. Returns translated job dict."""
        now = datetime.now(timezone.utc).isoformat()
        job_id = job["id"]

        db = await self._db()
        try:
            await db.execute(
                "UPDATE jobs SET status = 'running', assigned_node_id = ?, assigned_at = ?, "
                "started_at = ? WHERE id = ? AND status = 'pending'",
                (node_id, now, now, job_id),
            )
            await db.execute(
                "UPDATE worker_nodes SET current_job_id = ?, status = 'working' WHERE id = ?",
                (job_id, node_id),
            )
            await db.commit()
        finally:
            await db.close()

        # Translate paths for the worker
        result = dict(job)
        result["file_path"] = await self.translate_path(job["file_path"], node_id, "to_worker")
        return result

    async def release_job(self, job_id: int, node_id: str) -> None:
        """Release a job back to pending (e.g. worker crashed)."""
        db = await self._db()
        try:
            await db.execute(
                "UPDATE jobs SET status = 'pending', assigned_node_id = NULL, "
                "assigned_at = NULL, started_at = NULL, progress = 0, "
                "fps = NULL, eta_seconds = NULL WHERE id = ?",
                (job_id,),
            )
            await db.execute(
                "UPDATE worker_nodes SET current_job_id = NULL, "
                "status = CASE WHEN last_heartbeat > datetime('now', '-5 minutes') THEN 'online' ELSE 'offline' END "
                "WHERE id = ?",
                (node_id,),
            )
            await db.commit()
        finally:
            await db.close()

    # Maximum consecutive failures before auto-suspending a node
    MAX_CONSECUTIVE_FAILURES = 5

    async def complete_job_on_node(
        self, node_id: str, job_id: int, success: bool = True, space_saved: int = 0,
    ) -> None:
        """Update node stats after a job completes.

        On success: increment jobs_completed, reset consecutive_failures.
        On failure: increment consecutive_failures. If threshold hit, set status='error'.
        """
        db = await self._db()
        try:
            if success:
                await db.execute(
                    "UPDATE worker_nodes SET current_job_id = NULL, status = 'online', "
                    "jobs_completed = jobs_completed + 1, consecutive_failures = 0, "
                    "total_space_saved = total_space_saved + ? WHERE id = ?",
                    (max(0, space_saved), node_id),
                )
            else:
                # Increment failure count, auto-suspend if threshold reached
                await db.execute(
                    "UPDATE worker_nodes SET current_job_id = NULL, "
                    "consecutive_failures = consecutive_failures + 1, "
                    "status = CASE WHEN consecutive_failures + 1 >= ? THEN 'error' ELSE 'online' END "
                    "WHERE id = ?",
                    (self.MAX_CONSECUTIVE_FAILURES, node_id),
                )
            await db.commit()

            # Check if node was just suspended
            if not success:
                async with db.execute(
                    "SELECT consecutive_failures, name FROM worker_nodes WHERE id = ?", (node_id,)
                ) as cur:
                    row = await cur.fetchone()
                if row and row["consecutive_failures"] >= self.MAX_CONSECUTIVE_FAILURES:
                    print(
                        f"[NODES] Node '{row['name']}' ({node_id}) suspended after "
                        f"{row['consecutive_failures']} consecutive failures",
                        flush=True,
                    )
        finally:
            await db.close()

    async def reset_node(self, node_id: str) -> bool:
        """Reset a node's error state — clears consecutive failures, sets back to online."""
        db = await self._db()
        try:
            async with db.execute(
                "UPDATE worker_nodes SET consecutive_failures = 0, "
                "status = CASE WHEN current_job_id IS NOT NULL THEN 'working' ELSE 'online' END "
                "WHERE id = ? AND status = 'error'",
                (node_id,),
            ) as cur:
                changed = cur.rowcount > 0
            await db.commit()
            if changed:
                async with db.execute("SELECT name FROM worker_nodes WHERE id = ?", (node_id,)) as cur:
                    row = await cur.fetchone()
                print(f"[NODES] Node '{row['name'] if row else node_id}' reset from error state", flush=True)
            return changed
        finally:
            await db.close()

    # ------------------------------------------------------------------
    # Path translation
    # ------------------------------------------------------------------

    async def translate_path(self, path: str, node_id: str, direction: str = "to_worker") -> str:
        """Translate a path using the node's path_mappings.

        direction: "to_worker" = server→worker, "to_server" = worker→server

        Source priority (v0.3.31+):
          1. `path_mappings_override` — admin-edited via the Node Settings
             modal. Non-null means "UI has taken over", ignore env-var.
          2. `path_mappings` — worker-reported (from its PATH_MAPPINGS env
             var on heartbeat). Legacy / bootstrap default.
        """
        if node_id == "local":
            return path  # Local node uses same paths

        node = await self.get_node(node_id)
        if not node:
            return path

        override = node.get("path_mappings_override")
        if override is not None:
            mappings = override
        else:
            mappings = node.get("path_mappings") or []
        for m in mappings:
            src = m.get("server", "").rstrip("/")
            dst = m.get("worker", "").rstrip("/")
            if not src or not dst:
                continue
            if direction == "to_worker" and path.startswith(src + "/"):
                return dst + path[len(src):]
            elif direction == "to_server" and path.startswith(dst + "/"):
                return src + path[len(dst):]
        return path

    # ------------------------------------------------------------------
    # Stale detection & cleanup
    # ------------------------------------------------------------------

    async def mark_offline(self, node_id: str) -> None:
        db = await self._db()
        try:
            await db.execute(
                "UPDATE worker_nodes SET status = 'offline', current_job_id = NULL WHERE id = ?",
                (node_id,),
            )
            await db.commit()
        finally:
            await db.close()

    async def release_stale_assignments(self, stale_timeout_seconds: int = 300) -> int:
        """Find nodes whose heartbeat is older than timeout, mark offline, release their jobs."""
        released = 0
        db = await self._db()
        try:
            # Find stale nodes that are still "online" or "working"
            async with db.execute(
                "SELECT id, current_job_id FROM worker_nodes "
                "WHERE id != 'local' AND status IN ('online', 'working') "
                "AND last_heartbeat < datetime('now', ?)",
                (f"-{stale_timeout_seconds} seconds",),
            ) as cur:
                stale_nodes = await cur.fetchall()

            for node in stale_nodes:
                nid = node["id"]
                jid = node["current_job_id"]
                # Mark node offline
                await db.execute(
                    "UPDATE worker_nodes SET status = 'offline', current_job_id = NULL WHERE id = ?",
                    (nid,),
                )
                # Release assigned job back to pending
                if jid:
                    await db.execute(
                        "UPDATE jobs SET status = 'pending', assigned_node_id = NULL, "
                        "assigned_at = NULL, started_at = NULL, progress = 0 WHERE id = ? AND status = 'running'",
                        (jid,),
                    )
                    released += 1
                    print(f"[NODES] Released job {jid} from stale node '{nid}'", flush=True)

            # Also release any jobs assigned to nodes that are already offline
            async with db.execute(
                "SELECT j.id, j.assigned_node_id FROM jobs j "
                "LEFT JOIN worker_nodes w ON j.assigned_node_id = w.id "
                "WHERE j.status = 'running' AND j.assigned_node_id IS NOT NULL "
                "AND j.assigned_node_id != 'local' "
                "AND (w.id IS NULL OR w.status = 'offline')"
            ) as cur:
                orphaned = await cur.fetchall()
            for row in orphaned:
                await db.execute(
                    "UPDATE jobs SET status = 'pending', assigned_node_id = NULL, "
                    "assigned_at = NULL, started_at = NULL, progress = 0 WHERE id = ?",
                    (row["id"],),
                )
                released += 1

            if released or stale_nodes:
                await db.commit()
            if released:
                print(f"[NODES] Released {released} stale job(s) back to pending", flush=True)
        finally:
            await db.close()
        return released

    async def remove_node(self, node_id: str) -> bool:
        """Remove a remote node (any status, but never the local node)."""
        if node_id == "local":
            return False
        db = await self._db()
        try:
            # Release any assigned job back to pending first
            async with db.execute(
                "SELECT current_job_id FROM worker_nodes WHERE id = ?", (node_id,)
            ) as cur:
                row = await cur.fetchone()
            if row and row["current_job_id"]:
                await db.execute(
                    "UPDATE jobs SET status = 'pending', assigned_node_id = NULL, "
                    "assigned_at = NULL, started_at = NULL, progress = 0 "
                    "WHERE id = ? AND status = 'running'",
                    (row["current_job_id"],),
                )
            async with db.execute(
                "DELETE FROM worker_nodes WHERE id = ?", (node_id,)
            ) as cur:
                deleted = cur.rowcount > 0
            await db.commit()
            return deleted
        finally:
            await db.close()

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def request_cancel(self, job_id: int, requeue: bool = False) -> None:
        """Flag a job for cancellation. If requeue=True, return to pending instead of failing."""
        self._cancel_flags.add(job_id)
        if requeue:
            self._requeue_on_cancel.add(job_id)

    def should_requeue(self, job_id: int) -> bool:
        return job_id in self._requeue_on_cancel

    def is_cancel_requested(self, job_id: int) -> bool:
        return job_id in self._cancel_flags

    def clear_cancel(self, job_id: int) -> None:
        self._cancel_flags.discard(job_id)
        self._requeue_on_cancel.discard(job_id)

    # ------------------------------------------------------------------
    # Auto-detection helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _detect_capabilities(gpu_name: str | None = None) -> tuple[list[str], str | None]:
        """Return (capabilities, nvenc_unavailable_reason).

        Fixes the Mac false-positive: we only try the NVENC test if an NVIDIA
        GPU is actually present (nvidia-smi succeeded upstream). The ffmpeg
        binary reports `hevc_nvenc` in `-encoders` whether or not CUDA is
        wired up — that's a compile-time feature, not a runtime one — so the
        presence check is where we were getting burned.

        Returns a human-readable failure reason for the UI whenever NVENC
        isn't claimed. Callers may pass the reason through to the Monitor
        page so users understand *why* they're stuck on CPU encoding.
        """
        caps: list[str] = []
        nvenc_reason: str | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-encoders",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            out = stdout.decode(errors="replace")
            if "libx265" in out:
                caps.append("libx265")

            if "hevc_nvenc" not in out:
                nvenc_reason = "ffmpeg build has no hevc_nvenc encoder"
                print(f"[NODES] {nvenc_reason}", flush=True)
            elif not gpu_name:
                # No NVIDIA GPU visible to this container/host. Don't even
                # try the test encode — claiming nvenc here is the Mac bug.
                nvenc_reason = "no NVIDIA GPU detected (nvidia-smi unavailable)"
                print(f"[NODES] NVENC not advertised: {nvenc_reason}", flush=True)
            else:
                try:
                    test = await asyncio.create_subprocess_exec(
                        "ffmpeg", "-hide_banner", "-y",
                        "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.04:r=25",
                        "-frames:v", "1", "-c:v", "hevc_nvenc", "-f", "null", "-",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr_out = await asyncio.wait_for(test.communicate(), timeout=10)
                    # rc==0 is the truth — if ffmpeg couldn't actually encode
                    # a frame it would exit non-zero. Trusting rc alone avoids
                    # the fragile stderr-substring heuristic that let Mac
                    # containers through before.
                    if test.returncode == 0:
                        caps.append("nvenc")
                        print(f"[NODES] NVENC encode test passed", flush=True)
                    else:
                        stderr_full = stderr_out.decode(errors="replace").strip()
                        # Keep the last meaningful line — that's where ffmpeg
                        # typically drops the actionable error (driver too
                        # old, device busy, etc.). Surface it to the UI.
                        tail = [ln.strip() for ln in stderr_full.splitlines() if ln.strip()]
                        nvenc_reason = tail[-1] if tail else f"ffmpeg exited {test.returncode}"
                        print(f"[NODES] NVENC test failed (rc={test.returncode}): {nvenc_reason}", flush=True)
                except Exception as exc:
                    nvenc_reason = f"NVENC test crashed: {exc}"
                    print(f"[NODES] {nvenc_reason}", flush=True)
        except Exception as exc:
            nvenc_reason = f"ffmpeg not runnable: {exc}"

        return (caps or ["libx265"], nvenc_reason)

    @staticmethod
    async def _detect_gpu() -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi", "--query-gpu=name", "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            name = stdout.decode(errors="replace").strip().split("\n")[0].strip()
            return name if name else None
        except Exception:
            return None

    @staticmethod
    async def _detect_driver_version() -> str | None:
        """Return the NVIDIA driver version string (e.g. "535.183.01") or
        None if nvidia-smi isn't available / reports nothing.

        Used alongside the compiled-in minimum driver (SHRINKERR_NVENC_MIN_DRIVER
        from the Dockerfile) so the UI can tell the user specifically *which*
        version they need to upgrade to when NVENC fails.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            ver = stdout.decode(errors="replace").strip().split("\n")[0].strip()
            return ver if ver else None
        except Exception:
            return None

    @staticmethod
    async def _detect_ffmpeg_version() -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-version",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            line = stdout.decode(errors="replace").split("\n")[0]
            # "ffmpeg version N-123888-g25e187f849..." -> extract version
            parts = line.split()
            if len(parts) >= 3:
                return parts[2][:30]
            return None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row) -> dict:
        d = dict(row)
        # Parse JSON fields
        for field in ("capabilities", "path_mappings", "schedule_hours"):
            if isinstance(d.get(field), str):
                try:
                    d[field] = json.loads(d[field])
                except Exception:
                    d[field] = []
        # path_mappings_override is nullable — distinguish "not set" (None,
        # fall back to worker-reported) from "set to empty list" (explicit
        # "no mappings at all, ignore env-var").
        pmo = d.get("path_mappings_override")
        if pmo is None:
            d["path_mappings_override"] = None
        elif isinstance(pmo, str):
            try:
                d["path_mappings_override"] = json.loads(pmo)
            except Exception:
                d["path_mappings_override"] = None
        # Coerce integer bools to actual booleans for the frontend
        for field in ("paused", "translate_encoder", "schedule_enabled"):
            if field in d:
                d[field] = bool(d[field])
        # Surface only a boolean indicator of whether a token is set, and
        # the issue timestamp — never the token value itself. The token is
        # a shared secret and must not round-trip through the read API or
        # the websocket broadcast. `token_issued_at` is safe to expose so
        # the UI can show "Token bootstrapped <date>".
        has_token = bool(d.pop("token", None))
        d["has_token"] = has_token
        return d


# Singleton background loop for stale detection
async def stale_release_loop(node_manager: NodeManager, interval: int = 60):
    """Periodically check for stale nodes and release their jobs."""
    while True:
        try:
            await node_manager.release_stale_assignments()
        except Exception as exc:
            print(f"[NODES] Stale release check failed: {exc}", flush=True)
        await asyncio.sleep(interval)
