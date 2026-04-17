"""Sample-based test encoding with VMAF quality analysis.

Encodes a 30-second sample from a media file and compares quality metrics
(filesize, compression ratio, VMAF score) against the original.
"""

import asyncio
import json
import os
import shutil
import time
import uuid
from pathlib import Path

TEMP_DIR = Path("/tmp/squeezarr_test")
# In-memory state for active test encodes
_tasks: dict[str, dict] = {}

# VMAF availability (checked once at startup)
_vmaf_available: bool | None = None


async def check_vmaf_available() -> bool:
    """Check if the installed ffmpeg supports libvmaf."""
    global _vmaf_available
    if _vmaf_available is not None:
        return _vmaf_available
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-filters",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        # ffmpeg -filters outputs to stdout, but some builds use stderr
        combined = stdout + stderr
        _vmaf_available = b"libvmaf" in combined
    except Exception as exc:
        print(f"[VMAF] Check failed: {exc}", flush=True)
        _vmaf_available = False
    print(f"[VMAF] libvmaf available: {_vmaf_available}", flush=True)
    return _vmaf_available


def cleanup_temp_dir():
    """Clean up old test encode files on startup."""
    if TEMP_DIR.exists():
        # Remove files older than 1 hour
        cutoff = time.time() - 3600
        for f in TEMP_DIR.iterdir():
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                pass
    else:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)


def get_task(task_id: str) -> dict | None:
    """Get test encode task state."""
    return _tasks.get(task_id)


async def run_test_encode(
    file_path: str,
    encoder: str = "nvenc",
    cq: int = 20,
    preset: str = "p6",
    sample_seconds: int = 30,
    ws_manager=None,
) -> dict:
    """Run a test encode on a sample segment and return quality metrics.

    Returns:
        {task_id, status, original_size, encoded_size, ratio, vmaf_score, vmaf_label, encoding_fps, sample_duration}
    """
    task_id = str(uuid.uuid4())[:8]
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    _tasks[task_id] = {
        "task_id": task_id,
        "status": "starting",
        "progress": 0,
        "step": "probing",
        "file_path": file_path,
    }

    async def _send_progress(step: str, progress: float = 0):
        _tasks[task_id]["step"] = step
        _tasks[task_id]["progress"] = progress
        if ws_manager:
            try:
                await ws_manager.broadcast({
                    "type": "test_encode_progress",
                    "task_id": task_id,
                    "step": step,
                    "progress": round(progress, 1),
                    "status": "running",
                })
            except Exception:
                pass

    try:
        # 1. Probe file for duration
        await _send_progress("probing")
        from backend.scanner import probe_file
        probe = await probe_file(file_path)
        if not probe or not probe.get("duration") or probe["duration"] < 10:
            raise ValueError(f"File too short or probe failed (duration={probe.get('duration') if probe else None})")

        duration = probe["duration"]
        # Sample from 33% into the file (deterministic, avoids intros/credits)
        start_time = max(0, duration * 0.33)
        sample_dur = min(sample_seconds, duration - start_time)

        orig_path = TEMP_DIR / f"{task_id}_orig.mkv"
        enc_path = TEMP_DIR / f"{task_id}_enc.mkv"

        # 2. Extract original segment (stream copy — fast)
        await _send_progress("extracting", 10)
        extract_cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", str(start_time), "-t", str(sample_dur),
            "-i", file_path, "-c", "copy",
            "-map", "0:v:0", "-map", "0:a:0?",
            str(orig_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *extract_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"Segment extraction failed: {stderr.decode()[-500:]}")

        original_size = orig_path.stat().st_size

        # 3. Encode the sample
        await _send_progress("encoding", 30)
        enc_cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1"]
        enc_cmd += ["-i", str(orig_path)]

        if encoder == "nvenc":
            enc_cmd += [
                "-c:v", "hevc_nvenc",
                "-preset", preset,
                "-tune", "hq",
                "-rc", "vbr",
                "-cq", str(cq),
                "-profile:v", "main10",
                "-pix_fmt", "p010le",
            ]
        else:
            crf = cq + 2  # CRF offset for libx265
            enc_cmd += [
                "-c:v", "libx265",
                "-preset", "medium",
                "-crf", str(crf),
                "-profile:v", "main10",
                "-pix_fmt", "yuv420p10le",
            ]

        enc_cmd += ["-c:a", "copy", "-map", "0:v:0", "-map", "0:a:0?", str(enc_path)]

        enc_start = time.time()
        proc = await asyncio.create_subprocess_exec(
            *enc_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Parse progress
        encoding_fps = 0.0
        if proc.stdout:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").strip()
                if decoded.startswith("out_time_us="):
                    try:
                        us = int(decoded.split("=")[1])
                        pct = min(95, 30 + (us / (sample_dur * 1_000_000)) * 60)
                        await _send_progress("encoding", pct)
                    except (ValueError, ZeroDivisionError):
                        pass
                elif decoded.startswith("fps="):
                    try:
                        encoding_fps = float(decoded.split("=")[1])
                    except ValueError:
                        pass

        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        enc_time = time.time() - enc_start

        if proc.returncode != 0 or not enc_path.exists():
            raise RuntimeError(f"Encoding failed: {stderr.decode()[-500:]}")

        encoded_size = enc_path.stat().st_size
        ratio = 1 - (encoded_size / original_size) if original_size > 0 else 0

        # 4. VMAF analysis
        vmaf_score = None
        vmaf_label = None

        if await check_vmaf_available():
            await _send_progress("analyzing", 90)
            vmaf_json = TEMP_DIR / f"{task_id}_vmaf.json"
            vmaf_cmd = [
                "ffmpeg", "-y", "-hide_banner", "-progress", "pipe:1",
                "-i", str(enc_path),
                "-i", str(orig_path),
                "-lavfi", f"libvmaf=model=version=vmaf_v0.6.1:log_fmt=json:log_path={vmaf_json}",
                "-f", "null", "-",
            ]
            vmaf_proc = await asyncio.create_subprocess_exec(
                *vmaf_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            # Track VMAF progress (90% → 100%)
            if vmaf_proc.stdout:
                while True:
                    line = await vmaf_proc.stdout.readline()
                    if not line:
                        break
                    decoded = line.decode("utf-8", errors="replace").strip()
                    if decoded.startswith("out_time_us=") and sample_dur > 0:
                        try:
                            us = int(decoded.split("=")[1])
                            pct = min(99, 90 + (us / (sample_dur * 1_000_000)) * 10)
                            await _send_progress("analyzing", pct)
                        except (ValueError, ZeroDivisionError):
                            pass
            await vmaf_proc.wait()

            if vmaf_json.exists():
                try:
                    vmaf_data = json.loads(vmaf_json.read_text())
                    # VMAF JSON format: {"pooled_metrics": {"vmaf": {"mean": 95.2, ...}}, ...}
                    pooled = vmaf_data.get("pooled_metrics", {})
                    vmaf_score = pooled.get("vmaf", {}).get("mean")
                    if vmaf_score is None:
                        # Alternative format
                        vmaf_score = vmaf_data.get("VMAF score", None)
                except (json.JSONDecodeError, KeyError):
                    pass

            if vmaf_score is not None:
                vmaf_score = round(vmaf_score, 1)
                if vmaf_score >= 93:
                    vmaf_label = "Excellent"
                elif vmaf_score >= 87:
                    vmaf_label = "Good"
                elif vmaf_score >= 80:
                    vmaf_label = "Fair"
                else:
                    vmaf_label = "Poor"

        result = {
            "task_id": task_id,
            "status": "complete",
            "original_size": original_size,
            "encoded_size": encoded_size,
            "ratio": round(ratio * 100, 1),
            "vmaf_score": vmaf_score,
            "vmaf_label": vmaf_label,
            "vmaf_available": await check_vmaf_available(),
            "encoding_fps": round(encoding_fps, 1),
            "encoding_time": round(enc_time, 1),
            "sample_duration": round(sample_dur, 1),
            "encoder": encoder,
            "cq": cq,
            "preset": preset,
        }

        _tasks[task_id] = result

        if ws_manager:
            try:
                await ws_manager.broadcast({
                    "type": "test_encode_complete",
                    "task_id": task_id,
                    "result": result,
                })
            except Exception:
                pass

        return result

    except Exception as e:
        error_result = {
            "task_id": task_id,
            "status": "failed",
            "error": str(e),
        }
        _tasks[task_id] = error_result
        if ws_manager:
            try:
                await ws_manager.broadcast({
                    "type": "test_encode_complete",
                    "task_id": task_id,
                    "result": error_result,
                })
            except Exception:
                pass
        return error_result

    finally:
        # Cleanup temp files (keep for 1 hour for debugging)
        # Actual cleanup happens via cleanup_temp_dir() on startup
        pass


async def run_vmaf_analysis(
    original_path: str,
    encoded_path: str,
    sample_seconds: int = 30,
) -> float | None:
    """Run VMAF analysis on two files and return the mean score.

    Used for post-conversion quality tracking (Phase 4).
    Extracts a sample from each file to keep analysis fast.
    """
    if not await check_vmaf_available():
        return None

    task_id = str(uuid.uuid4())[:8]
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # Probe for duration
        from backend.scanner import probe_file
        probe = await probe_file(encoded_path)
        if not probe or not probe.get("duration"):
            return None

        duration = probe["duration"]
        start = max(0, duration * 0.33)
        sample_dur = min(sample_seconds, duration - start)

        # Extract samples
        orig_sample = TEMP_DIR / f"{task_id}_vorig.mkv"
        enc_sample = TEMP_DIR / f"{task_id}_venc.mkv"

        for src, dst in [(original_path, orig_sample), (encoded_path, enc_sample)]:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", str(start), "-t", str(sample_dur),
                "-i", src, "-c", "copy", "-map", "0:v:0",
                str(dst),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=120)

        if not orig_sample.exists() or not enc_sample.exists():
            return None

        # Run VMAF
        vmaf_json = TEMP_DIR / f"{task_id}_vmaf.json"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(enc_sample),
            "-i", str(orig_sample),
            "-lavfi", f"libvmaf=model=version=vmaf_v0.6.1:log_fmt=json:log_path={vmaf_json}",
            "-f", "null", "-",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=300)

        if vmaf_json.exists():
            data = json.loads(vmaf_json.read_text())
            score = data.get("pooled_metrics", {}).get("vmaf", {}).get("mean")
            return round(score, 1) if score is not None else None

        return None

    except Exception as e:
        print(f"[VMAF] Analysis failed: {e}", flush=True)
        return None

    finally:
        # Cleanup
        for f in [
            TEMP_DIR / f"{task_id}_vorig.mkv",
            TEMP_DIR / f"{task_id}_venc.mkv",
            TEMP_DIR / f"{task_id}_vmaf.json",
        ]:
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass
