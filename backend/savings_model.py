"""The savings estimate, learned from your own conversions (v0.10.0).

The estimate starts from a CQ → savings curve measured on NVENC conversions
(encoding_estimates.cq_to_savings_pct): one percentage for every file. Your
finished conversions teach factors on it:

- per CQ: the curve's shape isn't every library's (one saved 46 % at CQ 27,
  where the curve says 75 %, and 70 % at CQ 36, where it says 80 %);
- per file: its bitrate (bits per pixel), then its source (remux, Blu-ray,
  WEB-DL, HDTV, DVD, from the name), then its codec. A 30 Mb/s remux shrinks
  far more than a 4 Mb/s WEB-DL.

Each group's factor is pulled toward its broader group's until it has enough
samples of its own (empirical Bayes shrinkage); the CQ and file factors are
fitted in turn. With no history, the estimate is the curve. On one library
(84K conversions) this cut the typical error from 12 % to 10 %, and remuxes'
from 26 % to 6 %.

Samples are completed jobs that encoded HEVC at a fixed quality (not a VMAF
target) and removed no tracks (removed tracks are estimated on their own). A
job that re-encoded audio counts at its whole saving: a remux's lossless
tracks are part of what converting it saves. A discarded larger output counts
as no savings. A file's resolution: the job's, else (not resized) its
output's scan row, else the tag in its name.

A resize shrinks the output with its pixels, to the power resize_alpha
(RESIZE_ALPHA until your resized conversions teach it, v0.10.0): a 4K remux
resized to 1080p saves 85 % and more, and was estimated as if it stayed 4K.
Resized conversions teach that power, not the files' factors.

Stored per-file estimates (scan_results.video_conv_savings_bytes) are
recomputed when the model learns or the default quality changes.
"""
from __future__ import annotations

import asyncio
import bisect
import json
import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field

from backend.database import connect_db
from backend.encoding_estimates import (
    cq_to_savings_pct, effective_cq, load_effective_cq, load_target_resolution, set_target_resolution,
)
from backend.resolution import tag_height
from backend.scan_filters import release_source

K = 10                                  # pseudo-samples pulling a group toward its parent
BPP_BANDS = (0.05, 0.1, 0.2, 0.4)       # bits per pixel (at 24 fps): 5 bands
MAX_SAMPLES = 200_000                   # the most recent conversions
REBUILD_SECONDS = 600
MAX_PCT = 0.95
ROUNDS = 3                              # fitting the CQ and the file factors in turn
# A resized output's size ∝ its pixels ** RESIZE_ALPHA: fewer pixels need
# fewer bits, though not in proportion (each keeps more detail).
RESIZE_ALPHA = 0.75
# The widths the encode scales to (converter.RESOLUTION_MAP: by width).
_TARGET_WIDTHS = {"1080p": 1920, "720p": 1280, "480p": 854}

_FAMILIES = (("h264", ("h264", "avc")), ("hevc", ("hevc", "h265")), ("mpeg2", ("mpeg2",)),
             ("vc1", ("vc1", "wmv3")), ("mpeg4", ("mpeg4", "msmpeg4", "xvid", "divx")))
_NAME_CODECS = ((re.compile(r"\b(?:x264|h\.?264|avc)\b", re.I), "h264"),
                (re.compile(r"\b(?:x265|h\.?265|hevc)\b", re.I), "hevc"),
                (re.compile(r"\b(?:xvid|divx)\b", re.I), "mpeg4"),
                (re.compile(r"\bvc-?1\b", re.I), "vc1"),
                (re.compile(r"\bmpeg-?2\b", re.I), "mpeg2"))


def codec_family(codec: str | None) -> str | None:
    c = (codec or "").lower().replace("-", "").replace(".", "")
    return next((fam for fam, names in _FAMILIES if any(c.startswith(n) for n in names)), None)


def bits_per_pixel(file_size: int | None, duration: float | None, width: int | None, height: int | None) -> float | None:
    """The file's bits per pixel and frame, taking 24 frames a second."""
    if not file_size or not duration or duration <= 0 or not height:
        return None
    width = width or round(height * 16 / 9)
    return file_size * 8 / duration / (width * height * 24)


def band(bpp: float | None) -> int | None:
    if bpp is None:
        return None
    return next((i for i, edge in enumerate(BPP_BANDS) if bpp < edge), len(BPP_BANDS))


def resize_ratio(width: int | None, height: int | None, target_resolution: str | None,
                 path: str | None = None) -> float | None:
    """The share of its pixels a file keeps when the encode resizes it to
    `target_resolution` (by width, as the encode scales), or None when it
    isn't made smaller. Without a height, its name's resolution tag."""
    target = _TARGET_WIDTHS.get(target_resolution or "")
    if not target:
        return None
    if not height:
        width, height = None, tag_height(path or "")
    if not height:
        return None
    width = width or round(height * 16 / 9)
    return (target / width) ** 2 if width > target else None


def file_groups(file_size: int | None, duration: float | None, width: int | None, height: int | None,
                codec: str | None, path: str | None) -> tuple:
    """A file's groups, broadest first: its bitrate band ("?" not known), its
    source ("other"), and its codec family when known. Without a height, the
    one its name's resolution tag names."""
    if not height:
        width, height = None, tag_height(path or "")
    b = band(bits_per_pixel(file_size, duration, width, height))
    family = codec_family(codec)
    return ("?" if b is None else b, release_source(path) or "other") + ((family,) if family else ())


@dataclass(frozen=True)
class SavingsModel:
    # Factors on the curve: () → every file; then (band,), (band, source),
    # (band, source, codec family).
    factors: dict = field(default_factory=dict)
    samples: int = 0
    # CQ → its own factor, at each CQ with enough conversions.
    by_cq: dict = field(default_factory=dict)
    # A resize's power (RESIZE_ALPHA), as your resized conversions show it.
    resize_alpha: float = RESIZE_ALPHA

    def factor(self, groups: tuple = ()) -> float:
        """The factor of the narrowest of `groups` the model learned."""
        f = self.factors.get((), 1.0)
        for depth in range(1, len(groups) + 1):
            if groups[:depth] not in self.factors:
                break
            f = self.factors[groups[:depth]]
        return f

    def cq_factor(self, cq: int) -> float:
        """Between two learned CQs a straight line; past them the nearest
        one's; none learned: 1."""
        if not self.by_cq:
            return 1.0
        known = sorted(self.by_cq)
        if cq <= known[0]:
            return self.by_cq[known[0]]
        if cq >= known[-1]:
            return self.by_cq[known[-1]]
        i = bisect.bisect_left(known, cq)
        if known[i] == cq:
            return self.by_cq[cq]
        lo, hi = known[i - 1], known[i]
        return self.by_cq[lo] + (self.by_cq[hi] - self.by_cq[lo]) * (cq - lo) / (hi - lo)

    def video_pct(self, cq: int, *, file_size: int = 0, duration: float = 0, width: int = 0, height: int = 0,
                  codec: str | None = None, path: str | None = None, target_resolution: str | None = None) -> float:
        """The share of the file a video conversion at `cq` (NVENC's scale)
        saves, resized to `target_resolution` ("copy"/None: not); with no
        file (no size or path), any file's."""
        groups = file_groups(file_size, duration, width, height, codec, path) if file_size or path else ()
        pct = max(0.0, min(MAX_PCT, cq_to_savings_pct(cq) * self.cq_factor(cq) * self.factor(groups)))
        ratio = resize_ratio(width, height, target_resolution, path)
        if ratio:
            pct = min(MAX_PCT, 1 - (1 - pct) * ratio ** self.resize_alpha)
        return pct


def _shrink(values: list[float], parent: float) -> float:
    return (sum(values) + K * parent) / (len(values) + K)


def _tree(samples: list[tuple[float, tuple]]) -> dict:
    """Each group's factor (every prefix of the samples' groups), shrunk
    toward its parent's."""
    members: dict = defaultdict(list)
    for ratio, groups in samples:
        for depth in range(len(groups) + 1):
            members[groups[:depth]].append(ratio)
    factors: dict = {}
    for key in sorted(members, key=len):  # parents first
        factors[key] = _shrink(members[key], factors[key[:-1]] if key else 1.0)
    return factors


def fit(samples: list[tuple]) -> SavingsModel:
    """samples: (achieved savings ÷ the curve's, CQ, the file's groups[, the
    share of its pixels a resize kept]). The files' factors from the ones
    not resized; the resize's power from the resized, against what the
    files' factors say they'd have saved at their own size."""
    plain = [s[:3] for s in samples if len(s) < 4 or not s[3]]
    resized = [s for s in samples if len(s) > 3 and s[3]]
    model = SavingsModel(_tree([(r, g) for r, _, g in plain]), len(samples))
    for _ in range(ROUNDS):
        residuals: dict = defaultdict(list)
        for ratio, cq, groups in plain:
            residuals[cq].append(ratio / model.factor(groups))
        by_cq = {cq: _shrink(v, 1.0) for cq, v in residuals.items() if len(v) >= K}
        model = SavingsModel(model.factors, len(samples), by_cq)
        model = SavingsModel(_tree([(r / model.cq_factor(cq), g) for r, cq, g in plain]), len(samples), by_cq)
    alphas = []
    for ratio, cq, groups, kept in resized:
        same = min(MAX_PCT, cq_to_savings_pct(cq) * model.cq_factor(cq) * model.factor(groups))
        out = 1 - ratio * cq_to_savings_pct(cq)  # the output's share of the file
        if 0 < out < 1 and 0 < same < 1 and 0 < kept < 1:
            alphas.append(min(1.5, max(0.3, math.log(out / (1 - same)) / math.log(kept))))
    return SavingsModel(model.factors, len(samples), model.by_cq, _shrink(alphas, RESIZE_ALPHA))


_SAMPLE_SQL = f"""
SELECT j.file_path, j.original_file_path, j.audio_tracks_to_remove,
       json_extract(j.encoding_stats, '$.input_size') AS in_size,
       json_extract(j.encoding_stats, '$.output_size') AS out_size,
       json_extract(j.encoding_stats, '$.duration') AS duration,
       json_extract(j.encoding_stats, '$.encoder') AS encoder,
       json_extract(j.encoding_stats, '$.cq') AS cq,
       json_extract(j.encoding_stats, '$.crf') AS crf,
       json_extract(j.encoding_stats, '$.qsv_cq') AS qsv_cq,
       json_extract(j.encoding_stats, '$.vaapi_qp') AS vaapi_qp,
       json_extract(j.encoding_stats, '$.videotoolbox_quality') AS vt_quality,
       json_extract(j.encoding_stats, '$.output_codec') AS output_codec,
       json_extract(j.encoding_stats, '$.vmaf_target') AS vmaf_target,
       json_extract(j.encoding_stats, '$.target_resolution') AS target_resolution,
       json_extract(j.encoding_stats, '$.source_codec') AS source_codec,
       json_extract(j.encoding_stats, '$.source_width') AS source_width,
       json_extract(j.encoding_stats, '$.source_height') AS source_height,
       s.video_width, s.video_height
FROM jobs j LEFT JOIN scan_results s ON s.file_path = j.file_path
WHERE j.status = 'completed' AND j.job_type IN ('convert', 'combined')
  AND j.encoding_stats IS NOT NULL AND json_valid(j.encoding_stats)
ORDER BY j.id DESC LIMIT {MAX_SAMPLES}"""


def sample(row: dict) -> tuple[float, int, tuple, float | None] | None:
    """A finished job as a sample, or None when it doesn't tell the savings."""
    in_size, out_size = row.get("in_size") or 0, row.get("out_size")
    if in_size <= 0 or out_size is None or out_size < 0:
        return None
    if (row.get("output_codec") or "hevc") != "hevc" or row.get("vmaf_target"):
        return None
    try:
        if json.loads(row.get("audio_tracks_to_remove") or "[]"):
            return None
    except (TypeError, ValueError):
        return None
    cq = effective_cq({"default_encoder": row.get("encoder"), "nvenc_cq": row.get("cq"),
                       "libx265_crf": row.get("crf"), "qsv_cq": row.get("qsv_cq"),
                       "vaapi_qp": row.get("vaapi_qp"), "videotoolbox_quality": row.get("vt_quality")})
    ratio = min(2.5, max(0.0, 1 - out_size / in_size) / cq_to_savings_pct(cq))
    width, height = row.get("source_width"), row.get("source_height")
    if not height and (row.get("target_resolution") or "copy") == "copy":
        width, height = row.get("video_width"), row.get("video_height")  # not resized: the output's size
    original = row.get("original_file_path") or row.get("file_path")
    codec = row.get("source_codec")
    if not codec:
        name = (original or "").rsplit("/", 1)[-1]
        codec = next((c for rx, c in _NAME_CODECS if rx.search(name)), None)
    kept = resize_ratio(row.get("source_width"), row.get("source_height"), row.get("target_resolution"), original)
    return ratio, cq, file_groups(in_size, row.get("duration"), width, height, codec, original), kept


_model = SavingsModel()
_state: dict = {"counter": None, "built": 0.0, "cq": None, "target": None}


def current() -> SavingsModel:
    """The model as last learned (the curve alone until then)."""
    return _model


async def rebuild() -> SavingsModel:
    global _model
    db = await connect_db()
    try:
        async with db.execute(_SAMPLE_SQL) as cur:
            rows = await cur.fetchall()
        async with db.execute("SELECT n FROM change_counters WHERE name = 'completed_jobs'") as cur:
            r = await cur.fetchone()
    finally:
        await db.close()
    # ~90K conversions: a second or two, off the event loop
    _model = await asyncio.to_thread(lambda: fit([s for s in (sample(dict(r)) for r in rows) if s is not None]))
    _state.update(counter=r[0] if r else None, built=time.monotonic())
    return _model


async def reestimate_scan_rows() -> int:
    """Recompute the stored video savings of the files that need converting;
    the number of rows that changed."""
    model = _model
    db = await connect_db()
    try:
        cq = await load_effective_cq(db)
        target = await load_target_resolution(db)
        async with db.execute(
            "SELECT file_path, file_size, duration, video_width, video_height, video_codec, "
            "COALESCE(video_conv_savings_bytes, 0) AS old FROM scan_results "
            "WHERE removed_from_list = 0 AND needs_conversion = 1"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    def changed() -> list[tuple[int, str]]:
        out = []
        for r in rows:
            size = r["file_size"] or 0
            new = int(size * model.video_pct(cq, file_size=size, duration=r["duration"] or 0,
                                             width=r["video_width"] or 0, height=r["video_height"] or 0,
                                             codec=r["video_codec"], path=r["file_path"], target_resolution=target))
            if abs(new - r["old"]) > max(1, r["old"] // 100):
                out.append((new, r["file_path"]))
        return out
    changes = await asyncio.to_thread(changed)  # 160K files: off the event loop
    for i in range(0, len(changes), 500):  # short write transactions
        db = await connect_db()
        try:
            await db.executemany("UPDATE scan_results SET video_conv_savings_bytes = ? WHERE file_path = ?",
                                 changes[i:i + 500])
            await db.commit()
        finally:
            await db.close()
        await asyncio.sleep(0)
    _state["cq"], _state["target"] = cq, target
    return len(changes)


async def tick(force: bool = False) -> None:
    """Relearn when conversions finished since (at most every
    REBUILD_SECONDS), and recompute the stored estimates when the model,
    the default quality or the target resolution changed."""
    db = await connect_db()
    try:
        async with db.execute("SELECT n FROM change_counters WHERE name = 'completed_jobs'") as cur:
            r = await cur.fetchone()
        cq = await load_effective_cq(db)
        target = await load_target_resolution(db)
    finally:
        await db.close()
    set_target_resolution(target)
    counter = r[0] if r else None
    learned = False
    if force or (counter != _state["counter"] and time.monotonic() - _state["built"] >= REBUILD_SECONDS):
        before = _model
        await rebuild()
        learned = force or (_model.factors, _model.by_cq, _model.resize_alpha) != (
            before.factors, before.by_cq, before.resize_alpha)
    if learned or cq != _state["cq"] or target != _state["target"]:
        changed = await reestimate_scan_rows()
        if changed:
            print(f"[ESTIMATES] Updated {changed} file estimates (learned from {_model.samples} conversions)",
                  flush=True)


async def savings_model_loop() -> None:
    try:
        await tick(force=True)
    except Exception as exc:
        print(f"[ESTIMATES] Learning from finished jobs failed: {exc}", flush=True)
    while True:
        await asyncio.sleep(60)
        try:
            await tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[ESTIMATES] Update failed: {exc}", flush=True)
