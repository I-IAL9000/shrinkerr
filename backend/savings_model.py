"""The savings estimate, learned from your own conversions (v0.10.0).

The estimate starts from a CQ → savings curve measured on NVENC conversions
(encoding_estimates.cq_to_savings_pct): one percentage for every file. But
a file's bitrate decides much of what an encode saves: a 30 Mb/s remux
shrinks far more than a 4 Mb/s WEB-DL. So finished conversions teach a
factor on that curve for each bits-per-pixel band, and for each source codec
within a band. Each factor is pulled toward its broader group until it has
enough samples of its own (empirical Bayes shrinkage). With no history, the
estimate is the curve.

Samples are completed jobs that encoded HEVC at a fixed quality (not a VMAF
target), and didn't convert or remove audio, so the size change is the
video's. A discarded larger output counts as no savings.

Stored per-file estimates (scan_results.video_conv_savings_bytes) are
recomputed when the model learns or the default quality changes.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field

from backend.database import connect_db
from backend.encoding_estimates import cq_to_savings_pct, effective_cq, load_effective_cq

K = 10                                  # pseudo-samples pulling a group toward its parent
BPP_BANDS = (0.05, 0.1, 0.2, 0.4)       # bits per pixel (at 24 fps): 5 bands
MAX_SAMPLES = 20_000                    # the most recent conversions
REBUILD_SECONDS = 600
MAX_PCT = 0.95

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


@dataclass(frozen=True)
class SavingsModel:
    # () → the global factor; (band,) and (band, codec family) → theirs.
    factors: dict = field(default_factory=dict)
    samples: int = 0

    def factor(self, band_: int | None, family: str | None) -> float:
        f = self.factors.get((), 1.0)
        if band_ is None:
            return f
        f = self.factors.get((band_,), f)
        return f if family is None else self.factors.get((band_, family), f)

    def video_pct(self, cq: int, *, file_size: int = 0, duration: float = 0, width: int = 0, height: int = 0,
                  codec: str | None = None) -> float:
        """The share of the file a video conversion at `cq` (NVENC's scale) saves."""
        b = band(bits_per_pixel(file_size, duration, width, height))
        return max(0.0, min(MAX_PCT, cq_to_savings_pct(cq) * self.factor(b, codec_family(codec))))


def fit(samples: list[tuple[float, int | None, str | None]]) -> SavingsModel:
    """samples: (achieved savings / the curve's, band, codec family)."""
    def shrink(values: list[float], parent: float) -> float:
        return (sum(values) + K * parent) / (len(values) + K)
    factors: dict = {(): shrink([r for r, _, _ in samples], 1.0)}
    by_band: dict = defaultdict(list)
    by_family: dict = defaultdict(list)
    for r, b, fam in samples:
        if b is None:
            continue
        by_band[b].append(r)
        if fam:
            by_family[(b, fam)].append(r)
    for b, values in by_band.items():
        factors[(b,)] = shrink(values, factors[()])
    for (b, fam), values in by_family.items():
        factors[(b, fam)] = shrink(values, factors[(b,)])
    return SavingsModel(factors, len(samples))


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
       json_array_length(json_extract(j.encoding_stats, '$.audio_converted_from')) AS audio_converted,
       json_extract(j.encoding_stats, '$.lossless_target_codec') AS lossless,
       json_extract(j.encoding_stats, '$.source_codec') AS source_codec,
       json_extract(j.encoding_stats, '$.source_width') AS source_width,
       json_extract(j.encoding_stats, '$.source_height') AS source_height,
       s.video_width, s.video_height
FROM jobs j LEFT JOIN scan_results s ON s.file_path = j.file_path
WHERE j.status = 'completed' AND j.job_type IN ('convert', 'combined')
  AND j.encoding_stats IS NOT NULL AND json_valid(j.encoding_stats)
ORDER BY j.id DESC LIMIT {MAX_SAMPLES}"""


def sample(row: dict) -> tuple[float, int | None, str | None] | None:
    """A finished job as a sample, or None when it doesn't tell the video's savings."""
    in_size, out_size = row.get("in_size") or 0, row.get("out_size")
    if in_size <= 0 or out_size is None or out_size < 0:
        return None
    if (row.get("output_codec") or "hevc") != "hevc" or row.get("vmaf_target"):
        return None
    if row.get("audio_converted") or row.get("lossless"):
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
    if not height and not row.get("target_resolution"):
        width, height = row.get("video_width"), row.get("video_height")  # the output's: the same size
    codec = row.get("source_codec")
    if not codec:
        name = (row.get("original_file_path") or "").rsplit("/", 1)[-1]
        codec = next((c for rx, c in _NAME_CODECS if rx.search(name)), None)
    return ratio, band(bits_per_pixel(in_size, row.get("duration"), width, height)), codec_family(codec)


_model = SavingsModel()
_state: dict = {"counter": None, "built": 0.0, "cq": None}


def current() -> SavingsModel:
    """The model as last learned (the curve alone until then)."""
    return _model


async def rebuild() -> SavingsModel:
    global _model
    db = await connect_db()
    try:
        async with db.execute(_SAMPLE_SQL) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        async with db.execute("SELECT n FROM change_counters WHERE name = 'completed_jobs'") as cur:
            r = await cur.fetchone()
    finally:
        await db.close()
    _model = fit([s for s in map(sample, rows) if s is not None])
    _state.update(counter=r[0] if r else None, built=time.monotonic())
    return _model


async def reestimate_scan_rows() -> int:
    """Recompute the stored video savings of the files that need converting;
    the number of rows that changed."""
    model = _model
    db = await connect_db()
    try:
        cq = await load_effective_cq(db)
        async with db.execute(
            "SELECT file_path, file_size, duration, video_width, video_height, video_codec, "
            "COALESCE(video_conv_savings_bytes, 0) AS old FROM scan_results "
            "WHERE removed_from_list = 0 AND needs_conversion = 1"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()
    changes = []
    for r in rows:
        size = r["file_size"] or 0
        new = int(size * model.video_pct(cq, file_size=size, duration=r["duration"] or 0, width=r["video_width"] or 0,
                                         height=r["video_height"] or 0, codec=r["video_codec"]))
        if abs(new - r["old"]) > max(1, r["old"] // 100):
            changes.append((new, r["file_path"]))
    for i in range(0, len(changes), 500):  # short write transactions
        db = await connect_db()
        try:
            await db.executemany("UPDATE scan_results SET video_conv_savings_bytes = ? WHERE file_path = ?",
                                 changes[i:i + 500])
            await db.commit()
        finally:
            await db.close()
        await asyncio.sleep(0)
    _state["cq"] = cq
    return len(changes)


async def tick(force: bool = False) -> None:
    """Relearn when conversions finished since (at most every
    REBUILD_SECONDS), and recompute the stored estimates when the model or
    the default quality changed."""
    db = await connect_db()
    try:
        async with db.execute("SELECT n FROM change_counters WHERE name = 'completed_jobs'") as cur:
            r = await cur.fetchone()
        cq = await load_effective_cq(db)
    finally:
        await db.close()
    counter = r[0] if r else None
    learned = False
    if force or (counter != _state["counter"] and time.monotonic() - _state["built"] >= REBUILD_SECONDS):
        before = _model
        await rebuild()
        learned = force or _model.factors != before.factors
    if learned or cq != _state["cq"]:
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
