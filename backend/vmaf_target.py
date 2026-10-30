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
CQ_BEST, CQ_WORST = 14, 42
# The default target: the score the app counts as transparent (the
# Excellent tier; 95 before the setup wizard offered it as Automatic).
DEFAULT_TARGET = 93
# Samples spread through the file; shorter files aren't searched.
SAMPLE_AT = (0.2, 0.5, 0.8)
SAMPLE_SECONDS = 10
MIN_DURATION = 120
# One sample per ~8 minutes of file, 3 to 6 of them (sample_points).
SECONDS_PER_SAMPLE, MIN_SAMPLES, MAX_SAMPLES = 480, 3, 6
# A quality needs the mean of its samples at the target and no one sample
# more than this below it, so a hard scene isn't averaged away.
MAX_SAMPLE_DIP = 2.0


def sample_points(duration: float) -> list[float]:
    """Start times (seconds) of the samples for a file of `duration`: each
    SAMPLE_SECONDS sample centred on (i + 0.5) / n of the file."""
    n = min(max(round(duration / SECONDS_PER_SAMPLE), MIN_SAMPLES), MAX_SAMPLES)
    return [max(0.0, min(duration * (i + 0.5) / n - SAMPLE_SECONDS / 2, duration - SAMPLE_SECONDS))
            for i in range(n)]


def passes(values: list[float], target: float) -> bool:
    """Whether a quality's sample scores hold `target`."""
    return sum(values) / len(values) >= target and min(values) >= target - MAX_SAMPLE_DIP


def fractional_steps(encoder: str, output_codec: str | None) -> bool:
    """Whether the encoder's quality setting takes half steps: NVENC's -cq
    and libx265's CRF are floats. QSV, VAAPI, VideoToolbox and AV1 output
    use whole steps."""
    return (encoder or "nvenc").lower() in ("nvenc", "libx265") and output_codec != "av1"


async def search(score: Callable[[float], Awaitable[list[float]]], target: float, start: float, *,
                 best: float = CQ_BEST, worst: float = CQ_WORST, half_steps: bool = False,
                 setting_key: Callable[[float], object] = lambda cq: cq) -> dict:
    """Find the highest CQ in [best, worst] whose samples pass `target`,
    starting from `start` (the job's own quality, or where the last search
    in the folder landed). Steps from `start` towards the answer, by 2, then
    4, 8, 16 … (clamped to the range), until a CQ passes and the next fails
    or the range ends, so a far start still takes few rounds; then bisects
    between the last pass and the first fail; with `half_steps` tries the CQ
    found + 0.5. Assumes the score falls as CQ rises. `score(cq)` returns
    the per-sample VMAF values. `setting_key` maps a CQ to the encoder's
    actual setting: CQs sharing a key are scored once.
    {"cq", "vmaf" (mean at cq), "min" (lowest sample at cq), "reached",
    "rounds" (CQs scored), "scores": {cq: {"mean", "min"}}}; when no CQ
    passes, the best one is used."""
    scored: dict = {}  # setting key -> (mean, lowest sample, passes)
    scores: dict = {}

    async def at(cq: float) -> bool:
        key = setting_key(cq)
        if key not in scored:
            values = await score(cq)
            scored[key] = (round(sum(values) / len(values), 2), round(min(values), 2), passes(values, target))
        scores[cq] = {"mean": scored[key][0], "min": scored[key][1]}
        return scored[key][2]

    lo, hi = best - 1, worst + 1  # the last CQ that passed, and that failed; the ends are untried
    cq = min(max(round(start), best), worst)  # a remembered start can be a half step
    if await at(cq):
        lo, step = cq, 2
        while lo < worst:
            nxt = min(lo + step, worst)
            if not await at(nxt):
                hi = nxt
                break
            lo, step = nxt, step * 2
    else:
        hi, step = cq, 2
        while hi > best:
            nxt = max(hi - step, best)
            if await at(nxt):
                lo = nxt
                break
            hi, step = nxt, step * 2
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if await at(mid):
            lo = mid
        else:
            hi = mid
    found = lo >= best
    if found and half_steps and lo + 0.5 <= worst and await at(lo + 0.5):
        lo += 0.5
    chosen = lo if found else best
    return {"cq": chosen, "vmaf": scores[chosen]["mean"], "min": scores[chosen]["min"], "reached": found,
            "rounds": len(scored), "scores": scores}


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
    from backend.converter import _probe_vmaf_stream, _run_libvmaf_pass, _vmaf_pipelines
    ref_pipeline, dist_pipeline, fps = _vmaf_pipelines(await _probe_vmaf_stream(reference),
                                                       await _probe_vmaf_stream(encoded))
    result = await _run_libvmaf_pass(
        input_path=reference, temp_path=encoded, seek=0.0, duration=seconds,
        ref_pipeline=ref_pipeline, dist_pipeline=dist_pipeline, json_path=json_path,
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

        async def score(cq: int) -> list[float]:
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
            print(f"[VMAF-TARGET] CQ {cq}: VMAF {mean} (lowest {round(min(values), 2)}) on {len(values)} samples",
                  flush=True)
            return values

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
