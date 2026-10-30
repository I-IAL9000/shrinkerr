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
import time
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

# The search range, on NVENC's CQ scale.
CQ_BEST, CQ_WORST = 14, 42
# The default target: the score the app counts as transparent (the
# Excellent tier; 95 before the setup wizard offered it as Automatic).
DEFAULT_TARGET = 93
# Each sample's length; shorter files aren't searched.
SAMPLE_SECONDS = 10
MIN_DURATION = 120
# One sample per ~8 minutes of file, 3 to 6 of them (sample_points).
SECONDS_PER_SAMPLE, MIN_SAMPLES, MAX_SAMPLES = 480, 3, 6
# A quality needs the mean of its samples at the target and no one sample
# more than this below it, so a hard scene isn't averaged away.
MAX_SAMPLE_DIP = 2.0
# Where a search landed is trusted for this long (remembered_cq).
MEMORY_DAYS = 90
# A disc's samples start on a Blu-ray aligned unit (32 transport packets of
# 192 bytes), which is also a whole number of DVD sectors (2048).
DISC_ALIGN = 6144


def sample_points(duration: float) -> list[float]:
    """Start times (seconds) of the samples for a file of `duration`: each
    SAMPLE_SECONDS sample centred on (i + 0.5) / n of the file."""
    n = min(max(round(duration / SECONDS_PER_SAMPLE), MIN_SAMPLES), MAX_SAMPLES)
    return [max(0.0, min(duration * (i + 0.5) / n - SAMPLE_SECONDS / 2, duration - SAMPLE_SECONDS))
            for i in range(n)]


def disc_sample_offsets(size: int, duration: float) -> list[int]:
    """Where a disc's samples start, in bytes of its main title (`size`
    bytes long): the sample points' share of `duration`, aligned down."""
    return [int(size * start / duration) // DISC_ALIGN * DISC_ALIGN for start in sample_points(duration)]


def passes(values: list[float], target: float) -> bool:
    """Whether a quality's sample scores hold `target`."""
    return sum(values) / len(values) >= target and min(values) >= target - MAX_SAMPLE_DIP


def active_minimum(settings: dict) -> float:
    """The VMAF minimum (the reject threshold) in force for a job's settings:
    0 unless VMAF analysis is on, else the job's minimum (a rule's, or
    Settings') when it's above 0. A target can't be set below it: the
    search would pass qualities the minimum then rejects."""
    enabled = settings.get("vmaf_analysis_enabled", True)
    if not (enabled if isinstance(enabled, bool) else str(enabled).lower() == "true"):
        return 0.0
    try:
        return max(0.0, float(settings.get("vmaf_min_score") or 0))
    except (TypeError, ValueError):
        return 0.0


def effective_target(settings: dict) -> float | None:
    """The VMAF the search holds a job to, from its settings (Settings', with a
    rule's own over them): None when the target is off, else the score, but
    never under the VMAF minimum in force (active_minimum)."""
    if not settings.get("vmaf_target_enabled"):
        return None
    return max(float(settings.get("vmaf_target_score") or DEFAULT_TARGET), active_minimum(settings))


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


async def remembered_cq(db, file_path: str, target: float, encoder: str, output_codec: str) -> float | None:
    """Where the last search for `target` landed in `file_path`'s folder (a
    season, a film's folder) with this encoder and output codec, or None —
    nothing remembered, or nothing in the last MEMORY_DAYS. It's only a
    starting point for the next search."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MEMORY_DAYS)).isoformat()
    async with db.execute(
        "SELECT cq FROM vmaf_target_memory WHERE folder = ? AND target = ? AND encoder = ? "
        "AND output_codec = ? AND updated_at >= ?",
        (os.path.dirname(file_path), target, encoder, output_codec, cutoff),
    ) as cur:
        row = await cur.fetchone()
    return float(row[0]) if row else None


async def remember(db, file_path: str, target: float, encoder: str, output_codec: str, cq: float) -> None:
    """Record that a search for `target` landed at `cq` in `file_path`'s
    folder, replacing the last one; forgets what's older than MEMORY_DAYS."""
    now = datetime.now(timezone.utc)
    await db.execute("DELETE FROM vmaf_target_memory WHERE updated_at < ?",
                     ((now - timedelta(days=MEMORY_DAYS)).isoformat(),))
    await db.execute(
        "INSERT INTO vmaf_target_memory (folder, target, encoder, output_codec, cq, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(folder, target, encoder, output_codec) "
        "DO UPDATE SET cq = excluded.cq, updated_at = excluded.updated_at",
        (os.path.dirname(file_path), target, encoder, output_codec, cq, now.isoformat()))
    await db.commit()


async def remember_landing(db, file_path: str, encoding_stats: dict | None) -> None:
    """remember() where the search an encode reports in its `encoding_stats`
    landed (convert_file's "vmaf_target", under the encoder and output codec
    it encoded with); nothing when it didn't search, or doesn't say which
    encoder it searched with (a cancelled re-encode's result)."""
    landed = (encoding_stats or {}).get("vmaf_target")
    if landed and encoding_stats.get("encoder"):
        await remember(db, file_path, landed["target"], encoding_stats["encoder"],
                       encoding_stats.get("output_codec") or "hevc", landed["cq"])


async def _run(cmd: list[str], timeout: int, on_proc: Callable | None = None) -> bool:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE)
    if on_proc:  # the job's: a Cancel kills it, and the search ends there
        on_proc(proc)
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


async def _input_size(src: str) -> int | None:
    """The size in bytes of what ffmpeg reads from `src` — a disc's main
    title through concat: / bluray: — or None."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=size", "-of", "csv=p=0", "-i", src,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=180)  # a disc over SMB opens slowly
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    try:
        return int(out.decode().strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def _sample_fingerprint(path: str) -> tuple[int, bytes]:
    with open(path, "rb") as f:
        return os.path.getsize(path), f.read(4 << 20)


async def _cut_samples(input_path: str, duration: float, work: str, *, disc_input: str | None = None,
                       on_proc: Callable | None = None) -> list[str] | None:
    """The video of each sample point, copied into `work`; None when one
    can't be cut. A file is cut by time. A disc (`disc_input`, its main
    title as the encode reads it) is cut by position, since a DVD's
    timestamps restart in every VOB; libbluray moves each seek to the nearest
    entry point. Its samples are MPEG-TS, which carries VC-1 as Blu-rays do."""
    if not disc_input:
        samples = []
        for n, start_s in enumerate(sample_points(duration)):
            sample = os.path.join(work, f"sample{n}.mkv")
            if not await _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start_s:.3f}", "-t", str(SAMPLE_SECONDS),
                               "-i", input_path, "-map", "0:V:0", "-c", "copy", sample], 600, on_proc):
                return None
            samples.append(sample)
        return samples
    size = await _input_size(disc_input)
    if not size:
        print("[VMAF-TARGET] The disc's size couldn't be read — keeping the job's quality", flush=True)
        return None
    samples = []
    for n, offset in enumerate(disc_sample_offsets(size, duration)):
        sample = os.path.join(work, f"sample{n}.ts")
        # +genpts: a DVD cut mid-stream can start on a frame with no timestamp.
        if not await _run(["ffmpeg", "-v", "error", "-y", "-fflags", "+genpts", "-skip_initial_bytes", str(offset),
                           "-probesize", "50M", "-analyzeduration", "30M", "-i", disc_input,
                           "-t", str(SAMPLE_SECONDS), "-map", "0:V:0", "-c", "copy", "-f", "mpegts", sample],
                          600, on_proc):
            return None
        samples.append(sample)
    # A seek that fails quietly reads from the start every time.
    prints = [await asyncio.to_thread(_sample_fingerprint, s) for s in samples]
    if len(set(prints)) < len(prints):
        print("[VMAF-TARGET] The disc's samples came out the same — keeping the job's quality", flush=True)
        return None
    return samples


async def _vmaf(reference: str, encoded: str, seconds: float, json_path: str) -> float | None:
    from backend.converter import _probe_vmaf_stream, _run_libvmaf_pass, _vmaf_pipelines
    ref_pipeline, dist_pipeline, fps = _vmaf_pipelines(await _probe_vmaf_stream(reference),
                                                       await _probe_vmaf_stream(encoded))
    result = await _run_libvmaf_pass(
        input_path=reference, temp_path=encoded, seek=0.0, duration=seconds,
        ref_pipeline=ref_pipeline, dist_pipeline=dist_pipeline, json_path=json_path,
        fps_for_progress=fps or 24.0, progress_callback=None, step_label="")
    return result.get("score")


async def find_quality(input_path: str, encoder: str, duration: float, target: float, start: float,
                       convert_kwargs: dict | None = None, *, output_codec: str | None = None,
                       on_round: Callable[[float, int], Awaitable[None]] | None = None,
                       on_proc: Callable | None = None, disc_input: str | None = None) -> dict | None:
    """The CQ to encode `input_path` at for VMAF `target` (search's dict,
    plus "target", "samples" and "seconds", the search's wall time), or None
    when the file is too short, VMAF isn't available or a sample fails — the
    job then keeps its own quality. `start`: where the search begins (the
    job's quality, or where the last search in the folder landed).
    `convert_kwargs`: the job's convert_file overrides (preset, resolution,
    settings) so samples encode exactly as the job will; `output_codec` the
    codec it resolved (AV1 takes whole steps only). `on_round(cq, n)` is
    awaited as each CQ's samples start (n counts the CQs scored).
    `on_proc(proc)` is given each ffmpeg cutting or encoding a sample, as
    convert_file's `proc_callback` is: a Cancel kills it, which fails the
    sample, so the search ends (None). `disc_input`: a disc's main title as
    the encode reads it (concat: / bluray:), to cut the samples from."""
    from backend.converter import convert_file
    from backend.encoding_estimates import quality_settings
    from backend.test_encode import check_vmaf_available
    if duration < MIN_DURATION or not await check_vmaf_available():
        return None
    work = tempfile.mkdtemp(prefix="shrinkerr_vmaf_target_")
    began = time.monotonic()
    try:
        samples = await _cut_samples(input_path, duration, work, disc_input=disc_input, on_proc=on_proc)
        if not samples:
            return None

        class _Failed(Exception):
            pass

        rounds = 0

        async def score(cq: float) -> list[float]:
            nonlocal rounds
            rounds += 1
            if on_round:
                await on_round(cq, rounds)
            values = []
            for n, sample in enumerate(samples):
                plan = await convert_file(
                    sample, encoder, SAMPLE_SECONDS, command_only=True, override_cq=cq,
                    override_crf=quality_settings("libx265", cq)["libx265_crf"] if encoder == "libx265" else None,
                    output_codec=output_codec, **(convert_kwargs or {}))
                if not plan.get("command") or not await _run(list(plan["command"]), 1800, on_proc):
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

        def setting_key(cq: float) -> object:
            # What the encoder is actually given: VideoToolbox maps every CQ
            # from 29 up to one -q:v. A half step is its own setting.
            return tuple(quality_settings(encoder, cq).values()) if cq == int(cq) else cq

        try:
            result = await search(score, target, start, half_steps=fractional_steps(encoder, output_codec),
                                  setting_key=setting_key)
        except _Failed:
            print("[VMAF-TARGET] A sample couldn't be encoded or scored — keeping the job's quality", flush=True)
            return None
        seconds = round(time.monotonic() - began, 1)
        print(f"[VMAF-TARGET] {os.path.basename(input_path)}: CQ {result['cq']} for VMAF {target} "
              f"({result['vmaf']}{'' if result['reached'] else ', the best in range'}) "
              f"in {result['rounds']} rounds, {seconds}s", flush=True)
        return {**result, "target": target, "samples": len(samples), "seconds": seconds}
    finally:
        shutil.rmtree(work, ignore_errors=True)
