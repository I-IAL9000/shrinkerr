"""VMAF target quality (v0.10.0): instead of one quality setting for every
title, find each title's own — the lowest quality (smallest file) whose
encode still scores the target VMAF — on a few short samples, the way
ab-av1's crf-search does, then encode the whole file at it.

Quality is searched on NVENC's CQ scale (lower = better), which a job's
quality already uses for every encoder (encoding_estimates.quality_settings
maps it to libx265's CRF, QSV, VAAPI and VideoToolbox). Each sample is
encoded with the job's own command (convert_file(command_only=True)) and
scored against itself with the same libvmaf pass as the post-encode check.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from typing import Awaitable, Callable

# The search range, on NVENC's CQ scale.
CQ_BEST, CQ_WORST = 14, 34
# Samples spread through the file; shorter files aren't searched.
SAMPLE_AT = (0.2, 0.5, 0.8)
SAMPLE_SECONDS = 10
MIN_DURATION = 120


async def search(score: Callable[[int], Awaitable[float]], target: float, start: int,
                 best: int = CQ_BEST, worst: int = CQ_WORST) -> dict:
    """Binary search for the highest CQ in [best, worst] scoring at least
    `target`, starting from `start` (the job's own quality, often close).
    Assumes the score falls as CQ rises. {"cq", "vmaf", "reached", "scores"};
    when no CQ reaches the target, the best one is used."""
    scores: dict[int, float] = {}

    async def at(cq: int) -> float:
        if cq not in scores:
            scores[cq] = await score(cq)
        return scores[cq]

    lo, hi, found = best, worst, None
    cq = min(max(start, best), worst)
    while lo <= hi:
        if await at(cq) >= target:
            found, lo = cq, cq + 1
        else:
            hi = cq - 1
        cq = (lo + hi) // 2
    chosen = found if found is not None else best
    return {"cq": chosen, "vmaf": await at(chosen), "reached": found is not None, "scores": scores}


async def _run(cmd: list[str], timeout: int) -> bool:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        print(f"[VMAF-TARGET] {cmd[0]} timed out", flush=True)
        return False
    if proc.returncode != 0:
        print(f"[VMAF-TARGET] {cmd[0]} failed: {(err or b'').decode(errors='replace')[-300:]}", flush=True)
    return proc.returncode == 0


async def _vmaf(reference: str, encoded: str, seconds: float, json_path: str) -> float | None:
    from backend.converter import _probe_vmaf_stream, _run_libvmaf_pass
    fps = (await _probe_vmaf_stream(reference)).get("fps")
    chain = ["scale=in_range=auto:out_range=tv:flags=bicubic"]
    if fps:
        chain.append(f"fps=fps={fps:.6f}")
    chain.append("format=yuv420p")
    result = await _run_libvmaf_pass(
        input_path=reference, temp_path=encoded, seek=0.0, duration=seconds,
        ref_pipeline=",".join(chain), dist_pipeline=",".join(chain), json_path=json_path,
        fps_for_progress=fps or 24.0, progress_callback=None, step_label="")
    return result.get("score")


async def find_quality(input_path: str, encoder: str, duration: float, target: float, start: int,
                       convert_kwargs: dict | None = None) -> dict | None:
    """The CQ to encode `input_path` at for VMAF `target` (search's dict,
    plus "target"), or None when the file is too short, VMAF isn't
    available or a sample fails — the job then keeps its own quality.
    `convert_kwargs`: the job's convert_file overrides (preset, resolution,
    settings) so samples encode exactly as the job will."""
    from backend.converter import convert_file
    from backend.encoding_estimates import quality_settings
    from backend.test_encode import check_vmaf_available
    if duration < MIN_DURATION or not await check_vmaf_available():
        return None
    work = tempfile.mkdtemp(prefix="shrinkerr_vmaf_target_")
    try:
        samples = []
        for n, at in enumerate(SAMPLE_AT):
            sample = os.path.join(work, f"sample{n}.mkv")
            start_s = min(duration * at, duration - SAMPLE_SECONDS)
            if not await _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start_s:.3f}", "-t", str(SAMPLE_SECONDS),
                               "-i", input_path, "-map", "0:V:0", "-c", "copy", sample], 600):
                return None
            samples.append(sample)

        class _Failed(Exception):
            pass

        async def score(cq: int) -> float:
            values = []
            for n, sample in enumerate(samples):
                plan = await convert_file(
                    sample, encoder, SAMPLE_SECONDS, command_only=True, override_cq=cq,
                    override_crf=quality_settings("libx265", cq)["libx265_crf"] if encoder == "libx265" else None,
                    **(convert_kwargs or {}))
                if not plan.get("command") or not await _run(list(plan["command"]), 1800):
                    raise _Failed
                value = await _vmaf(sample, plan["output_path"], SAMPLE_SECONDS,
                                    os.path.join(work, f"vmaf{n}_{cq}.json"))
                try:
                    os.unlink(plan["output_path"])
                except OSError:
                    pass
                if value is None:
                    raise _Failed
                values.append(value)
            mean = round(sum(values) / len(values), 2)
            print(f"[VMAF-TARGET] CQ {cq}: VMAF {mean} on {len(values)} samples", flush=True)
            return mean

        try:
            result = await search(score, target, start)
        except _Failed:
            print("[VMAF-TARGET] A sample couldn't be encoded or scored — keeping the job's quality", flush=True)
            return None
        print(f"[VMAF-TARGET] {os.path.basename(input_path)}: CQ {result['cq']} for VMAF {target} "
              f"({result['vmaf']}{'' if result['reached'] else ', the best in range'})", flush=True)
        return {**result, "target": target}
    finally:
        shutil.rmtree(work, ignore_errors=True)
