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
import statistics
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
# A folder with no landing of its own starts at the others' median once
# there are this many (remembered_cq).
MIN_LANDINGS = 3
# A target no quality reaches (grainy footage levels off below it): the
# search takes the highest CQ scoring within this of the best it found, not
# the best quality, which only bought a bigger file (search).
KNEE_POINTS = 1.0
# A first guess's slope (next_cq): VMAF points per CQ step near the target —
# the maintainer's films lost 0.5 to 0.9 a step around 93.
GUESS_SLOPE = 0.6
# The furthest one round moves from the nearest CQ scored (next_cq).
MAX_JUMP = 8
# A better quality that scores less than this more is no better (next_cq).
PLATEAU_GAIN = 0.2
# A half step is scored unless its whole neighbours, averaged, put it more
# than this under the target: Sgt. Bilko's 29 and 30 averaged 92.82 for a
# target of 93, and 29.5 scored 92.77 — a round for nothing.
HALF_STEP_MARGIN = 0.1
# The search scores every other frame of a sample: 2.7 s instead of 4.1 for a
# 10 s 1080p sample, VMAF 99.09 instead of 99.11 (the maintainer's NUC).
VMAF_SUBSAMPLE = 2
# A sample's score that low is checked by timestamps too (_vmaf): no real
# encode in the search's range scores it, a misaligned compare does.
SUSPECT_SCORE = 50.0
# The best quality scores well over this on any footage its samples are
# measured right on: The Nest's scored 31 at CQ 14 and at CQ 37 alike, and
# the search, taking the target as out of reach, chose 37 (the file scored
# 88.7). Below it the samples aren't trusted, and the job keeps its quality.
MIN_TRUSTED_BEST = 80.0
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


def judged(scores: dict, cq: float) -> float:
    """What a scored CQ is held to the target by (search's `scores`): its
    mean, or its lowest sample's allowance when that's lower — passes()
    as one number."""
    s = scores[cq]
    return min(s["mean"], s["min"] + MAX_SAMPLE_DIP)


def next_cq(scores: dict, target: float, lo: float, hi: float, best: float, worst: float, *,
            bisect: bool = False, value: Callable[[float], float] | None = None) -> int:
    """The next whole CQ for search to score, strictly between `lo` (the
    highest that passed, or best - 1) and `hi` (the lowest that failed, or
    worst + 1): where the line through the scores nearest the target
    crosses it — between a pass and a fail, the two; on one side, the
    nearest two there, or the nearest at GUESS_SLOPE — at most MAX_JUMP
    from the nearest. `bisect`: the middle instead, when the line keeps
    landing on one side (the search counts it). When a better quality
    scored no better (PLATEAU_GAIN), the best quality is next. `value`: what
    a CQ is held to `target` by (judged; the knee's, its mean).

    The steps of 2, 4, 8 … and halving took six rounds on most films; this
    takes three or four (v0.10.0)."""
    value = value or (lambda c: judged(scores, c))

    def slope(a: float, b: float) -> float:  # points lost per CQ step from a to b (> a)
        rise = (value(a) - value(b)) / (b - a)
        return rise if rise > 0.05 else GUESS_SLOPE  # flat or noisy: the usual slope
    whole = sorted(c for c in scores if c == int(c))
    if lo >= best and hi <= worst:
        nearest = lo
        x = (lo + (hi - lo) / 2 if bisect
             else lo + (value(lo) - target) / slope(lo, hi))
    elif lo >= best:  # every CQ scored passed: further up
        nearest = lo
        below = [c for c in whole if c < lo]
        x = lo + (value(lo) - target) / (slope(below[-1], lo) if below else GUESS_SLOPE)
    else:  # every CQ scored failed: further down
        nearest = hi
        above = [c for c in whole if c > hi]
        if above and value(hi) - value(above[0]) < PLATEAU_GAIN:
            # Showgirls' samples scored 92.67 at CQ 25 and 92.68 at 24, 23, 19
            # and 14: whether any quality reaches the target, the best says.
            return int(max(best, lo + 1))
        x = hi - (target - value(hi)) / (slope(hi, above[0]) if above else GUESS_SLOPE)
    n = round(min(max(x, nearest - MAX_JUMP), nearest + MAX_JUMP))
    return int(min(max(n, lo + 1, best), hi - 1, worst))


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
    in the folder landed). Each round scores the CQ where the scores so far
    say the target is crossed (next_cq), until a CQ passes and the next
    whole step fails, or the range ends; with `half_steps` the CQ found
    + 0.5 is tried too, unless its neighbours put it under the target.
    Assumes the score falls as CQ rises. `score(cq)` returns the per-sample
    VMAF values. `setting_key` maps a CQ to the encoder's actual setting:
    CQs sharing a key are scored once.
    {"cq", "vmaf" (mean at cq), "min" (lowest sample at cq), "reached",
    "rounds" (CQs scored), "scores": {cq: {"mean", "min"}}}. When no CQ
    passes, the target is out of reach ("best": the best mean found): the
    highest CQ within KNEE_POINTS of it, narrowed between the CQs scored."""
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
    last, streak = None, 0  # which end the last rounds moved, and how many in a row
    while True:
        passed = await at(cq)
        if passed:
            lo = cq
        else:
            hi = cq
        streak = streak + 1 if passed == last else 1
        last = passed
        if hi - lo <= 1:
            break
        cq = next_cq(scores, target, lo, hi, best, worst, bisect=streak >= 3)
    found = lo >= best
    if (found and half_steps and lo == int(lo) and lo + 0.5 <= worst
            and judged(scores, lo) + judged(scores, lo + 1) >= 2 * (target - HALF_STEP_MARGIN)
            and await at(lo + 0.5)):
        lo += 0.5
    if found:
        return {"cq": lo, "vmaf": scores[lo]["mean"], "min": scores[lo]["min"], "reached": True,
                "rounds": len(scored), "scores": scores}
    # Out of reach (v0.10.0): CQ 14 scored 92.7 where CQ 22 scored 92.5 on
    # grainy footage, and came out larger than the original.
    top = max(v["mean"] for v in scores.values())

    def close(c: float) -> bool:
        return scores[c]["mean"] >= top - KNEE_POINTS
    k_lo = max(c for c in scores if close(c))
    k_hi = min([c for c in scores if c > k_lo and not close(c)], default=worst + 1)
    last, streak = None, 0
    while k_hi - k_lo > 1:
        mid = next_cq(scores, top - KNEE_POINTS, k_lo, k_hi, best, worst, bisect=streak >= 3,
                      value=lambda c: scores[c]["mean"])
        await at(mid)
        near = close(mid)
        if near:
            k_lo = mid
        else:
            k_hi = mid
        streak = streak + 1 if near == last else 1
        last = near
    return {"cq": k_lo, "vmaf": scores[k_lo]["mean"], "min": scores[k_lo]["min"], "reached": False, "best": top,
            "rounds": len(scored), "scores": scores}


async def remembered_cq(db, file_path: str, target: float, encoder: str, output_codec: str) -> float | None:
    """Where the last search for `target` landed in `file_path`'s folder (a
    season, a film's folder) with this encoder and output codec; else where
    the searches in other folders landed, their median, once there are
    MIN_LANDINGS — a film is alone in its folder, and started at the rule's
    CQ every time (25, where the maintainer's films landed at 25 to 33);
    else None. Only the last MEMORY_DAYS count. It's only a starting point
    for the next search."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MEMORY_DAYS)).isoformat()
    async with db.execute(
        "SELECT cq FROM vmaf_target_memory WHERE folder = ? AND target = ? AND encoder = ? "
        "AND output_codec = ? AND updated_at >= ?",
        (os.path.dirname(file_path), target, encoder, output_codec, cutoff),
    ) as cur:
        row = await cur.fetchone()
    if row:
        return float(row[0])
    async with db.execute(
        "SELECT cq FROM vmaf_target_memory WHERE target = ? AND encoder = ? AND output_codec = ? AND updated_at >= ?",
        (target, encoder, output_codec, cutoff),
    ) as cur:
        landings = [float(r[0]) for r in await cur.fetchall()]
    return statistics.median(landings) if len(landings) >= MIN_LANDINGS else None


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
    """A sample's encode scored against the sample, frame by frame by number:
    both hold the same frames from their start. By timestamps, The Nest's
    samples (VC-1 cut from a remux: no start time, and their encodes started
    a frame in) were compared each frame with its neighbour — 31 to 37 at
    every quality, 92 by number — and the search took CQ 37. A score under
    SUSPECT_SCORE is compared by timestamps too, and the better one kept: a
    misaligned compare only ever scores lower."""
    from backend.converter import _probe_vmaf_stream, _run_libvmaf_pass, _vmaf_pipelines
    src, dst = await _probe_vmaf_stream(reference), await _probe_vmaf_stream(encoded)

    async def compare(by_number: bool) -> float | None:
        ref_pipeline, dist_pipeline, fps = _vmaf_pipelines({**src, "start_time": None} if by_number else src, dst)
        result = await _run_libvmaf_pass(
            input_path=reference, temp_path=encoded, seek=0.0, duration=seconds,
            ref_pipeline=ref_pipeline, dist_pipeline=dist_pipeline, json_path=json_path,
            fps_for_progress=fps or 24.0, progress_callback=None, step_label="",
            frames=(0, int(seconds * 120)) if by_number else None,  # every frame, at up to 120 fps
            subsample=VMAF_SUBSAMPLE)
        return result.get("score")
    score = await compare(True)
    if score is None or score < SUSPECT_SCORE:
        by_time = await compare(False)
        if by_time is not None and (score is None or by_time > score):
            score = by_time
    return score


async def find_quality(input_path: str, encoder: str, duration: float, target: float, start: float,
                       convert_kwargs: dict | None = None, *, output_codec: str | None = None,
                       on_round: Callable[[float, int], Awaitable[None]] | None = None,
                       on_sample: Callable[[int, int], Awaitable[None]] | None = None,
                       on_proc: Callable | None = None, disc_input: str | None = None) -> dict | None:
    """The CQ to encode `input_path` at for VMAF `target` (search's dict,
    plus "target", "samples" and "seconds", the search's wall time), or None
    when the file is too short, VMAF isn't available or a sample fails — the
    job then keeps its own quality, as it does when the samples score too
    low at every quality to be trusted ({"untrusted": True, "best", "rounds",
    "seconds"}: MIN_TRUSTED_BEST). `start`: where the search begins (the
    job's quality, or where the last search in the folder landed).
    `convert_kwargs`: the job's convert_file overrides (preset, resolution,
    settings) so samples encode exactly as the job will; `output_codec` the
    codec it resolved (AV1 takes whole steps only). `on_round(cq, n)` is
    awaited as each CQ's samples start (n counts the CQs scored), and
    `on_sample(n, of)` as each of its samples starts.
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
        # The encoded samples' bytes per setting, against the samples' own:
        # whether a quality would make the file larger (the size guard).
        encoded_bytes: dict = {}
        source_bytes = sum(os.path.getsize(s) for s in samples)

        async def encode(n: int, cq: float) -> str:
            """Sample n encoded at `cq`: the encode's path (_Failed when it can't be)."""
            if on_sample:  # the round's progress: sample n + 1 of them
                await on_sample(n + 1, len(samples))
            plan = await convert_file(
                samples[n], encoder, SAMPLE_SECONDS, command_only=True, override_cq=cq,
                override_crf=quality_settings("libx265", cq)["libx265_crf"] if encoder == "libx265" else None,
                output_codec=output_codec, **(convert_kwargs or {}))
            if not plan.get("command") or not await _run(list(plan["command"]), 1800, on_proc):
                raise _Failed
            return plan["output_path"]

        async def score(cq: float) -> list[float]:
            nonlocal rounds
            rounds += 1
            if on_round:
                await on_round(cq, rounds)
            values = []
            # The next sample is encoded (the GPU's work, mostly) while this one
            # is scored (the CPU's): 1.6 s and 2.7 s for a 1080p sample on the
            # maintainer's NUC, one after the other until v0.10.0.
            pending: asyncio.Future | None = asyncio.ensure_future(encode(0, cq))
            try:
                for n, sample in enumerate(samples):
                    output = await pending
                    pending = asyncio.ensure_future(encode(n + 1, cq)) if n + 1 < len(samples) else None
                    value = await _vmaf(sample, output, SAMPLE_SECONDS, os.path.join(work, f"vmaf{n}_{cq}.json"))
                    try:
                        key = setting_key(cq)
                        encoded_bytes[key] = encoded_bytes.get(key, 0) + os.path.getsize(output)
                        os.unlink(output)
                    except OSError:
                        pass
                    if value is None:
                        raise _Failed
                    values.append(value)
            finally:
                if pending is not None:  # a sample failed: the next one's encode isn't left running
                    await asyncio.gather(pending, return_exceptions=True)
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
        if not result["reached"] and result["best"] < MIN_TRUSTED_BEST:
            print(f"[VMAF-TARGET] {os.path.basename(input_path)}: the samples scored {result['best']} at best, "
                  f"in {result['rounds']} rounds — they can't have been measured right; keeping the job's quality",
                  flush=True)
            return {"untrusted": True, "best": result["best"], "rounds": result["rounds"], "seconds": seconds}
        reach = "" if result["reached"] else f", out of reach: the best {result['best']}"
        print(f"[VMAF-TARGET] {os.path.basename(input_path)}: CQ {result['cq']} for VMAF {target} "
              f"({result['vmaf']}{reach}) in {result['rounds']} rounds, {seconds}s", flush=True)
        # The size guard: samples encoded at the quality found larger than
        # the original's mean the file would be too — not worth a full encode.
        chosen_bytes = encoded_bytes.get(setting_key(result["cq"]))
        ratio = round((chosen_bytes / source_bytes - 1) * 100, 1) if chosen_bytes and source_bytes else None
        if ratio is not None and ratio >= 0:
            print(f"[VMAF-TARGET] Its samples came out {ratio}% larger than the original's", flush=True)
        return {**result, "target": target, "samples": len(samples), "seconds": seconds,
                "larger": ratio is not None and ratio >= 0, "size_ratio": ratio}
    finally:
        shutil.rmtree(work, ignore_errors=True)
