"""DVD VTS IFO and Blu-ray .mpls language-metadata parsers. v0.6.5+.

The ffmpeg `concat:` (DVD) and `bluray:` (BDMV) probe paths don't surface
per-track language codes for discs — VOBs carry the data only in the IFO
sidecar, and many BD playlists don't tag streams despite libbluray reading
them. This module reads the sidecar metadata directly and returns it in
the same stream order that ffprobe enumerates, so the caller can patch
language fields onto probe results.

Both parsers fail open: any malformed / missing / truncated input returns
empty lists. Callers map empty → "und" at the merge step. Logs a
`[DISC-META]` warning on any failure.
"""

from __future__ import annotations

from pathlib import Path


# ISO 639-1 (2-letter) → ISO 639-2 (3-letter, "B" form matching ffprobe's convention).
# Covers ~50 most common languages. Discs report 2-letter; Shrinkerr's
# keep-language settings use 3-letter. Unknown codes return "".
_ISO639_1_TO_2: dict[str, str] = {
    "en": "eng", "de": "ger", "fr": "fre", "es": "spa", "it": "ita",
    "nl": "dut", "pt": "por", "ru": "rus", "ja": "jpn", "zh": "chi",
    "ko": "kor", "ar": "ara", "hi": "hin", "bn": "ben", "vi": "vie",
    "th": "tha", "tr": "tur", "pl": "pol", "sv": "swe", "no": "nor",
    "da": "dan", "fi": "fin", "is": "ice", "el": "gre", "he": "heb",
    "uk": "ukr", "cs": "cze", "hu": "hun", "ro": "rum", "bg": "bul",
    "hr": "hrv", "sr": "srp", "sk": "slo", "sl": "slv", "lt": "lit",
    "lv": "lav", "et": "est", "id": "ind", "ms": "may", "tl": "tgl",
    "ur": "urd", "fa": "per", "ca": "cat", "ga": "gle", "cy": "wel",
    "mt": "mlt", "eu": "baq", "gl": "glg", "af": "afr", "sw": "swa",
}


def _iso639_1_to_2(code: str) -> str:
    """Map a 2-letter ISO 639-1 code to its 3-letter ISO 639-2 (B-form)
    equivalent. Returns empty string for unknown / malformed input."""
    if not code or len(code) != 2:
        return ""
    return _ISO639_1_TO_2.get(code.lower(), "")


# DVD VTS IFO binary layout (libdvdread ifo_types.h, vtsi_mat_t):
#   bytes [0:12]    : "DVDVIDEO-VTS" magic
#   byte  [0x202]   : nr_of_vts_audio_streams (0-8)
#   bytes [0x204:]  : audio_attrs[8], 8 bytes each
#                     per entry: +2,+3 = lang_code (2-byte ASCII ISO 639-1)
#   byte  [0x254]   : nr_of_vts_subp_streams (0-32)
#   bytes [0x256:]  : subp_attrs[32], 6 bytes each
#                     per entry: +2,+3 = lang_code (2-byte ASCII ISO 639-1)
_DVD_IFO_MAGIC = b"DVDVIDEO-VTS"
_DVD_IFO_HEADER_BYTES = 0x320  # enough to cover both attr arrays
_DVD_AUDIO_COUNT_OFFSET = 0x202
_DVD_AUDIO_ATTR_OFFSET = 0x204
_DVD_AUDIO_ATTR_SIZE = 8
_DVD_AUDIO_MAX = 8
_DVD_SUBP_COUNT_OFFSET = 0x254
_DVD_SUBP_ATTR_OFFSET = 0x256
_DVD_SUBP_ATTR_SIZE = 6
_DVD_SUBP_MAX = 32


def _parse_dvd_ifo(ifo_path: Path) -> dict[str, list[str]]:
    """Parse a DVD VTS IFO file and extract per-stream language codes.

    Returns {"audio": [iso639_2, ...], "subtitle": [iso639_2, ...]} in
    stream order. Empty strings for unknown/unmapped codes. Empty lists
    on any read or parse failure (caller treats as 'no metadata
    available'; tracks stay 'und').
    """
    try:
        data = ifo_path.read_bytes()
    except OSError as exc:
        print(f"[DISC-META] could not read {ifo_path}: {exc}", flush=True)
        return {"audio": [], "subtitle": []}

    if len(data) < _DVD_IFO_HEADER_BYTES or data[:12] != _DVD_IFO_MAGIC:
        print(f"[DISC-META] bad/short IFO at {ifo_path}", flush=True)
        return {"audio": [], "subtitle": []}

    try:
        n_audio = min(data[_DVD_AUDIO_COUNT_OFFSET], _DVD_AUDIO_MAX)
        audio = []
        for i in range(n_audio):
            off = _DVD_AUDIO_ATTR_OFFSET + i * _DVD_AUDIO_ATTR_SIZE + 2
            code = data[off:off + 2].decode("ascii", errors="replace")
            audio.append(_iso639_1_to_2(code))

        n_subp = min(data[_DVD_SUBP_COUNT_OFFSET], _DVD_SUBP_MAX)
        subtitle = []
        for i in range(n_subp):
            off = _DVD_SUBP_ATTR_OFFSET + i * _DVD_SUBP_ATTR_SIZE + 2
            code = data[off:off + 2].decode("ascii", errors="replace")
            subtitle.append(_iso639_1_to_2(code))

        return {"audio": audio, "subtitle": subtitle}
    except Exception as exc:
        print(f"[DISC-META] IFO parse failed for {ifo_path}: {exc}", flush=True)
        return {"audio": [], "subtitle": []}
