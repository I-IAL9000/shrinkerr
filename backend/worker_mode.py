"""Worker mode entry point.

When SHRINKERR_MODE=worker, the container runs this instead of the FastAPI server.
Connects to a remote Shrinkerr server, polls for jobs, and executes them locally.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import signal
import sys
import uuid
from pathlib import Path

import httpx


# ------------------------------------------------------------------
# Config from environment
# ------------------------------------------------------------------
SERVER_URL = os.environ.get("SERVER_URL", "").rstrip("/")
API_KEY = os.environ.get("API_KEY", "")
WORKER_NAME = os.environ.get("WORKER_NAME", platform.node() or "worker")
PATH_MAPPINGS_RAW = os.environ.get("PATH_MAPPINGS", "[]")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "30"))
DATA_DIR = os.environ.get("SHRINKERR_DATA_DIR", "/app/data")
CAPABILITIES_OVERRIDE = os.environ.get("CAPABILITIES", "")  # e.g. "libx265" or "nvenc,libx265"


def _load_or_create_id() -> str:
    """Persist a unique worker ID across container restarts."""
    id_path = Path(DATA_DIR) / "worker_id"
    id_path.parent.mkdir(parents=True, exist_ok=True)
    if id_path.exists():
        return id_path.read_text().strip()
    wid = str(uuid.uuid4())[:12]
    id_path.write_text(wid)
    return wid


async def _detect_capabilities() -> list[str]:
    caps = []
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-encoders",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        out = stdout.decode(errors="replace")
        if "libx265" in out:
            caps.append("libx265")
        # Only claim nvenc if we can actually encode a frame (not just compiled-in)
        if "hevc_nvenc" in out:
            try:
                test = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-hide_banner", "-y",
                    "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.04:r=25",
                    "-frames:v", "1", "-c:v", "hevc_nvenc", "-f", "null", "-",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                _, stderr_out = await asyncio.wait_for(test.communicate(), timeout=10)
                stderr_text = stderr_out.decode(errors="replace").lower()
                # Check both return code AND stderr for CUDA/driver errors
                if test.returncode == 0 and "cannot load" not in stderr_text and "no cuda capable" not in stderr_text and "error" not in stderr_text:
                    caps.append("nvenc")
                    print("[WORKER] NVENC encode test passed", flush=True)
                else:
                    print(f"[WORKER] NVENC test failed (rc={test.returncode}): {stderr_out.decode(errors='replace')[:200]}", flush=True)
            except Exception as exc:
                print(f"[WORKER] NVENC test exception: {exc}", flush=True)
    except Exception:
        caps.append("libx265")
    return caps or ["libx265"]


async def _detect_gpu() -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi", "--query-gpu=name", "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        return stdout.decode(errors="replace").strip().split("\n")[0].strip() or None
    except Exception:
        return None


async def _detect_ffmpeg_version() -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        parts = stdout.decode(errors="replace").split("\n")[0].split()
        return parts[2][:30] if len(parts) >= 3 else None
    except Exception:
        return None


# ------------------------------------------------------------------
# HTTP client
# ------------------------------------------------------------------
class ServerClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.api_key = api_key
        self._client = httpx.AsyncClient(
            timeout=30,
            headers={"X-Api-Key": api_key},
        )

    async def heartbeat(self, node_id: str, name: str, hostname: str,
                        capabilities: list, path_mappings: list,
                        ffmpeg_version: str | None, gpu_name: str | None,
                        os_info: str | None, max_jobs: int) -> dict:
        resp = await self._client.post(f"{self.base_url}/api/nodes/heartbeat", json={
            "node_id": node_id, "name": name, "hostname": hostname,
            "capabilities": capabilities, "path_mappings": path_mappings,
            "ffmpeg_version": ffmpeg_version, "gpu_name": gpu_name,
            "os_info": os_info, "max_jobs": max_jobs,
        })
        resp.raise_for_status()
        return resp.json()

    async def request_job(self, node_id: str) -> tuple[dict | None, dict]:
        """Request next job. Returns (job_dict_or_None, full_response_dict)."""
        resp = await self._client.post(f"{self.base_url}/api/nodes/request-job", json={
            "node_id": node_id,
        })
        resp.raise_for_status()
        data = resp.json()
        return data.get("job"), data

    async def report_progress(self, node_id: str, job_id: int,
                              progress: float, fps: float | None = None,
                              eta_seconds: int | None = None,
                              step: str = "converting") -> bool:
        """Report progress. Returns True if job was cancelled."""
        resp = await self._client.post(f"{self.base_url}/api/nodes/report-progress", json={
            "node_id": node_id, "job_id": job_id,
            "progress": progress, "fps": fps,
            "eta_seconds": eta_seconds, "step": step,
        })
        resp.raise_for_status()
        return resp.json().get("cancelled", False)

    async def report_complete(self, node_id: str, job_id: int, success: bool,
                              output_path: str | None = None,
                              space_saved: int = 0, error: str | None = None,
                              vmaf_score: float | None = None,
                              backup_path: str | None = None,
                              ffmpeg_command: str | None = None,
                              encoding_stats: dict | None = None) -> dict:
        resp = await self._client.post(f"{self.base_url}/api/nodes/report-complete", json={
            "node_id": node_id, "job_id": job_id,
            "success": success, "output_path": output_path,
            "space_saved": space_saved, "error": error,
            "vmaf_score": vmaf_score, "backup_path": backup_path,
            "ffmpeg_command": ffmpeg_command, "encoding_stats": encoding_stats,
        })
        resp.raise_for_status()
        return resp.json()

    async def close(self):
        await self._client.aclose()


# ------------------------------------------------------------------
# Job executor
# ------------------------------------------------------------------
async def execute_job(client: ServerClient, node_id: str, job: dict, worker_capabilities: list[str]) -> None:
    """Execute a transcoding job locally and report results to the server."""
    job_id = job["id"]
    file_path = job["file_path"]
    file_name = os.path.basename(file_path)
    job_type = job.get("job_type", "convert")

    print(f"[WORKER] Starting job {job_id}: {file_name} ({job_type})", flush=True)

    if not os.path.exists(file_path):
        print(f"[WORKER] File not found: {file_path}", flush=True)
        await client.report_complete(node_id, job_id, False, error=f"File not found: {file_path}")
        return

    # Import the converter modules (available because we're using the same Docker image)
    from backend.scanner import probe_file
    from backend.converter import convert_file
    from backend.audio import remux_audio

    # Create a progress callback that reports to the server
    cancel_flag = False

    # Track the ffmpeg process for cancellation
    active_proc = None
    cancel_flag = False

    def on_proc(proc):
        nonlocal active_proc
        active_proc = proc

    async def progress_cb(progress: float = 0, fps=None, eta_seconds=None, step=None):
        nonlocal cancel_flag
        try:
            cancelled = await client.report_progress(
                node_id, job_id, progress, fps, eta_seconds, step or "converting",
            )
            if cancelled and not cancel_flag:
                cancel_flag = True
                print(f"[WORKER] Cancel received for job {job_id}, killing ffmpeg...", flush=True)
                if active_proc and active_proc.returncode is None:
                    try:
                        active_proc.kill()
                    except Exception:
                        pass
        except Exception as exc:
            print(f"[WORKER] Progress report failed: {exc}", flush=True)

    try:
        # Probe
        probe = await probe_file(file_path)
        if not probe:
            await client.report_complete(node_id, job_id, False, error="Failed to probe file")
            return

        duration = probe.get("duration", 0)
        file_size = probe.get("file_size", 0)
        space_saved = 0
        current_file_path = file_path
        result = None

        if job_type in ("convert", "combined"):
            # Use job's encoder if this worker supports it; otherwise fall back
            # to the worker's best capability (if translation is enabled).
            job_encoder = (job.get("encoder") or "").lower()
            translate_allowed = job.get("translate_encoder", True)

            if job_encoder in ("nvenc", "hevc_nvenc") and "nvenc" in worker_capabilities:
                encoder = "nvenc"
            elif job_encoder in ("libx265", "x265", "cpu") and "libx265" in worker_capabilities:
                encoder = "libx265"
            elif translate_allowed:
                encoder = "nvenc" if "nvenc" in worker_capabilities else "libx265"
            else:
                print(f"[WORKER] Refusing job {job_id}: encoder '{job_encoder}' incompatible and translation disabled", flush=True)
                await client.report_complete(
                    node_id, job_id, False,
                    error=f"Worker cannot run encoder '{job_encoder}' and translation is disabled",
                )
                return

            # When falling back from nvenc to libx265, translate quality settings
            nvenc_preset = job.get("nvenc_preset") or "p6"
            nvenc_cq = job.get("nvenc_cq") or 20
            libx265_preset = job.get("libx265_preset")
            libx265_crf = job.get("libx265_crf")

            if encoder == "libx265" and job_encoder in ("nvenc", "hevc_nvenc") and not libx265_preset:
                # Translate NVENC settings to equivalent libx265 settings
                # NVENC is less efficient per-bit, so libx265 CRF is ~3-4 higher
                # for similar visual quality at smaller file sizes
                PRESET_MAP = {
                    "p1": "veryfast", "p2": "faster", "p3": "fast",
                    "p4": "medium", "p5": "slow", "p6": "slower", "p7": "veryslow",
                }
                libx265_preset = PRESET_MAP.get(nvenc_preset, "medium")
                libx265_crf = max(16, min(28, nvenc_cq - 4))
                print(f"[WORKER] Translated nvenc {nvenc_preset}/CQ{nvenc_cq} → libx265 {libx265_preset}/CRF{libx265_crf}", flush=True)

            if job_encoder and job_encoder != encoder:
                print(f"[WORKER] Job requests '{job_encoder}' but using '{encoder}' (capability fallback)", flush=True)

            # Build pre_settings so convert_file() never reads the local DB
            # (worker has no settings table — it gets everything from the job)
            worker_settings = {
                "encoder": encoder,
                "nvenc_preset": nvenc_preset,
                "nvenc_cq": nvenc_cq,
                "libx265_preset": libx265_preset or "medium",
                "libx265_crf": libx265_crf or 20,
                "audio_codec": job.get("audio_codec") or "copy",
                "audio_bitrate": job.get("audio_bitrate") or 128,
                "target_resolution": job.get("target_resolution") or "copy",
                "filename_suffix": "",
                "custom_ffmpeg_flags": "",
                "auto_convert_lossless": False,
                "vmaf_analysis_enabled": False,
                "trash_original_after_conversion": False,
                "backup_original_days": 0,
            }

            # For combined jobs, parse track removal lists and pass them through
            # so ffmpeg applies them in the same conversion pass (single source-of-truth
            # for stream indices).
            _audio_rm = json.loads(job.get("audio_tracks_to_remove") or "[]") if isinstance(job.get("audio_tracks_to_remove"), str) else (job.get("audio_tracks_to_remove") or [])
            _sub_rm = json.loads(job.get("subtitle_tracks_to_remove") or "[]") if isinstance(job.get("subtitle_tracks_to_remove"), str) else (job.get("subtitle_tracks_to_remove") or [])

            result = await convert_file(
                input_path=file_path,
                encoder=encoder,
                duration=duration,
                progress_callback=progress_cb,
                proc_callback=on_proc,
                override_preset=job.get("nvenc_preset") if encoder != "libx265" else None,
                override_cq=job.get("nvenc_cq"),
                override_audio_codec=job.get("audio_codec"),
                override_audio_bitrate=job.get("audio_bitrate"),
                override_crf=job.get("libx265_crf") if encoder == "libx265" else None,
                override_libx265_preset=job.get("libx265_preset") if encoder == "libx265" else None,
                override_target_resolution=job.get("target_resolution"),
                pre_settings=worker_settings,
                audio_tracks_to_remove=_audio_rm if job_type == "combined" else None,
                subtitle_tracks_to_remove=_sub_rm if job_type == "combined" else None,
            )

            if cancel_flag:
                print(f"[WORKER] Job {job_id} cancelled", flush=True)
                await client.report_complete(node_id, job_id, False, error="Cancelled by user")
                return

            if result.get("error"):
                print(f"[WORKER] Job {job_id} failed: {result['error']}", flush=True)
                await client.report_complete(
                    node_id, job_id, False,
                    error=result["error"],
                    ffmpeg_command=result.get("ffmpeg_command"),
                )
                return

            space_saved = result.get("space_saved", 0)
            current_file_path = result.get("output_path", file_path)

        # Audio/subtitle track removal — only "audio" jobs need the separate remux pass.
        # "combined" jobs already applied the removals inline during conversion.
        if job_type == "audio":
            audio_remove = json.loads(job.get("audio_tracks_to_remove") or "[]") if isinstance(job.get("audio_tracks_to_remove"), str) else (job.get("audio_tracks_to_remove") or [])
            sub_remove = json.loads(job.get("subtitle_tracks_to_remove") or "[]") if isinstance(job.get("subtitle_tracks_to_remove"), str) else (job.get("subtitle_tracks_to_remove") or [])

            if audio_remove or sub_remove:
                await progress_cb(progress=95, step="removing tracks")
                try:
                    audio_result = await remux_audio(
                        current_file_path, audio_remove, sub_remove,
                    )
                    if audio_result.get("success"):
                        space_saved += audio_result.get("space_saved", 0)
                        if audio_result.get("output_path"):
                            current_file_path = audio_result["output_path"]
                except Exception as exc:
                    print(f"[WORKER] Audio remux failed (non-fatal): {exc}", flush=True)

        print(f"[WORKER] Job {job_id} completed: saved {space_saved / (1024**3):.2f} GB", flush=True)

        await client.report_complete(
            node_id, job_id, True,
            output_path=current_file_path,
            space_saved=space_saved,
            vmaf_score=result.get("vmaf_score") if result else None,
            backup_path=result.get("backup_path") if result else None,
            ffmpeg_command=result.get("ffmpeg_command") if result else None,
            encoding_stats=result.get("encoding_stats") if result else None,
        )

    except Exception as exc:
        print(f"[WORKER] Job {job_id} exception: {exc}", flush=True)
        import traceback
        traceback.print_exc()
        try:
            await client.report_complete(node_id, job_id, False, error=str(exc)[:500])
        except Exception:
            pass


# ------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------
async def run_worker():
    """Main entry point for worker mode."""
    if not SERVER_URL:
        print("[WORKER] ERROR: SERVER_URL environment variable is required", flush=True)
        sys.exit(1)
    if not API_KEY:
        print("[WORKER] ERROR: API_KEY environment variable is required", flush=True)
        sys.exit(1)

    node_id = _load_or_create_id()
    if CAPABILITIES_OVERRIDE:
        capabilities = [c.strip() for c in CAPABILITIES_OVERRIDE.split(",") if c.strip()]
        print(f"[WORKER] Using CAPABILITIES override: {capabilities}", flush=True)
    else:
        capabilities = await _detect_capabilities()
    gpu_name = await _detect_gpu()
    ffmpeg_version = await _detect_ffmpeg_version()
    os_info = f"{platform.system()} {platform.release()}"

    try:
        path_mappings = json.loads(PATH_MAPPINGS_RAW)
    except Exception:
        path_mappings = []

    print(f"[WORKER] Shrinkerr Worker Node", flush=True)
    print(f"[WORKER]   ID:           {node_id}", flush=True)
    print(f"[WORKER]   Name:         {WORKER_NAME}", flush=True)
    print(f"[WORKER]   Server:       {SERVER_URL}", flush=True)
    print(f"[WORKER]   Capabilities: {capabilities}", flush=True)
    print(f"[WORKER]   GPU:          {gpu_name or 'None'}", flush=True)
    print(f"[WORKER]   ffmpeg:       {ffmpeg_version or 'unknown'}", flush=True)
    if path_mappings:
        print(f"[WORKER]   Path maps:   {path_mappings}", flush=True)
    print(flush=True)

    client = ServerClient(SERVER_URL, API_KEY)
    running = True

    def handle_signal(sig, frame):
        nonlocal running
        print(f"\n[WORKER] Received signal {sig}, shutting down...", flush=True)
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Heartbeat loop
    async def heartbeat_loop():
        backoff = 1
        while running:
            try:
                await client.heartbeat(
                    node_id=node_id,
                    name=WORKER_NAME,
                    hostname=platform.node(),
                    capabilities=capabilities,
                    path_mappings=path_mappings,
                    ffmpeg_version=ffmpeg_version,
                    gpu_name=gpu_name,
                    os_info=os_info,
                    max_jobs=1,
                )
                backoff = 1  # Reset on success
            except Exception as exc:
                print(f"[WORKER] Heartbeat failed (backoff {backoff}s): {exc}", flush=True)
                backoff = min(backoff * 2, 30)
            await asyncio.sleep(HEARTBEAT_INTERVAL if backoff == 1 else backoff)

    # Job poll loop
    async def job_loop():
        backoff = 1
        suspended_logged = False
        while running:
            try:
                job, resp_data = await client.request_job(node_id)
                backoff = 1

                # Circuit breaker: server suspended this node due to repeated failures
                if resp_data.get("suspended"):
                    if not suspended_logged:
                        msg = resp_data.get("message", "Node suspended")
                        print(f"[WORKER] {msg}", flush=True)
                        print(f"[WORKER] Waiting for reset from the Nodes page...", flush=True)
                        suspended_logged = True
                    await asyncio.sleep(30)  # Poll slowly while suspended
                    continue

                suspended_logged = False  # Reset once no longer suspended

                if resp_data.get("queue_paused"):
                    await asyncio.sleep(POLL_INTERVAL)
                    continue

                if job:
                    await execute_job(client, node_id, job, capabilities)
                else:
                    await asyncio.sleep(POLL_INTERVAL)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    # Not registered yet — heartbeat will fix this
                    await asyncio.sleep(5)
                else:
                    print(f"[WORKER] Job request failed: {exc}", flush=True)
                    await asyncio.sleep(min(backoff * 2, 30))
                    backoff = min(backoff * 2, 30)
            except Exception as exc:
                print(f"[WORKER] Job loop error: {exc}", flush=True)
                await asyncio.sleep(min(backoff * 2, 30))
                backoff = min(backoff * 2, 30)

    try:
        # Send initial heartbeat
        try:
            await client.heartbeat(
                node_id=node_id, name=WORKER_NAME, hostname=platform.node(),
                capabilities=capabilities, path_mappings=path_mappings,
                ffmpeg_version=ffmpeg_version, gpu_name=gpu_name,
                os_info=os_info, max_jobs=1,
            )
            print(f"[WORKER] Connected to server at {SERVER_URL}", flush=True)
        except Exception as exc:
            print(f"[WORKER] Initial connection failed: {exc}", flush=True)
            print(f"[WORKER] Will retry in background...", flush=True)

        # Run both loops concurrently
        await asyncio.gather(
            heartbeat_loop(),
            job_loop(),
        )
    except asyncio.CancelledError:
        pass
    finally:
        await client.close()
        print("[WORKER] Shutdown complete", flush=True)
