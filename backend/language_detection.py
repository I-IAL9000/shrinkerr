"""Language detection for tracks Shrinkerr can't tag from container/IFO/
mpls/libbluray metadata (they come through as `und`).

detect_subtitle_language(text) — text subs, via langdetect (cheap).
Returns (ISO 639-2 B-form code, confidence 0-1) or (None, 0.0) when
detection fails or confidence is below threshold. Fail-open. v0.8.0+.
"""
from __future__ import annotations

import os

from backend.disc_metadata import _ISO639_1_TO_2, _normalize_iso639_2


def _sub_min_confidence() -> float:
    try:
        return float(os.environ.get("SHRINKERR_LANG_DETECT_SUB_MIN", "0.7"))
    except ValueError:
        return 0.7


def detect_subtitle_language(text: str) -> tuple[str | None, float]:
    """Detect language of subtitle text. langdetect yields ISO 639-1 +
    probability; map to 639-2 B-form to match the codebase."""
    if not text or not text.strip():
        return (None, 0.0)
    try:
        from langdetect import detect_langs, DetectorFactory
        DetectorFactory.seed = 0
        results = detect_langs(text)
    except Exception:
        return (None, 0.0)
    if not results:
        return (None, 0.0)
    best = results[0]
    iso1 = best.lang.lower()
    conf = float(best.prob)
    if conf < _sub_min_confidence():
        return (None, 0.0)
    iso2 = _ISO639_1_TO_2.get(iso1, "")
    if not iso2:
        return (None, 0.0)
    return (_normalize_iso639_2(iso2), conf)
