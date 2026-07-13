"""Image-subtitle (PGS/VobSub) language detection via OCR (v0.9.0).

Pipeline: extract a sample of subtitle bitmaps -> tesseract OSD (detect
script) -> tesseract OCR with the matching language pack -> feed text to
the existing detect_subtitle_language -> confidence gate -> ISO 639-2.
On-demand only. Fail-open: any failure returns (None, 0.0)."""
from __future__ import annotations

import re

# tesseract OSD reports a script name; map it to the OCR language pack(s)
# that read that script. langdetect then identifies the specific language
# from the OCR'd text, so Latin needs only `eng`.
_SCRIPT_TO_LANGS = {
    "Latin": "eng",
    "Han": "chi_sim+chi_tra",
    "HanS": "chi_sim",
    "HanT": "chi_tra",
    "Japanese": "jpn",
    "Korean": "kor",
    "Cyrillic": "rus",
    "Arabic": "ara",
}


def _script_to_ocr_langs(script: str | None) -> str:
    """Map an OSD script name to tesseract -l language(s). Unknown/None
    defaults to Latin (eng)."""
    if not script:
        return "eng"
    return _SCRIPT_TO_LANGS.get(script, "eng")


def _parse_osd_script(osd_output: str) -> str | None:
    """Pull the `Script: X` value out of `tesseract --psm 0` OSD output."""
    m = re.search(r"^Script:\s*(\S+)", osd_output or "", flags=re.MULTILINE)
    return m.group(1) if m else None
