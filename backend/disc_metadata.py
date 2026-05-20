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

import struct
from pathlib import Path
from typing import Optional


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


# Blu-ray .mpls header layout (BD-ROM Part 3):
#   bytes [0:4]   : "MPLS" magic
#   bytes [4:8]   : version ("0100" / "0200" / "0300", ASCII)
#   bytes [8:12]  : PlayList_start_address (uint32 BE)
#   bytes [12:16] : PlayListMark_start_address (uint32 BE)
#   bytes [16:20] : ExtensionData_start_address (uint32 BE)
#   bytes [20:40] : reserved
# PlayList:
#   uint32 BE   : length (bytes after this field)
#   uint16 BE   : reserved
#   uint16 BE   : number_of_PlayItems
#   uint16 BE   : number_of_SubPaths
#   PlayItem[]
# Each PlayItem header (first 34 bytes when no multi_angle):
#   uint16 BE   : length (after this field)
#   bytes [2:7] : clip_id (5 ASCII)
#   bytes [7:11]: clip_codec_id (4 ASCII)
#   uint16 BE   : flags (bit 4 = IsMultiAngle)
#   uint8       : ref_to_STC_id
#   uint32 BE   : IN_time (45 kHz ticks)
#   uint32 BE   : OUT_time
#   bytes [22:30]: UO_mask
#   uint8       : flags (PlayItem_random_access_flag)
#   uint8       : still_mode
#   uint16 BE   : still_time
_MPLS_MAGIC = b"MPLS"
_MPLS_VERSIONS = (b"0100", b"0200", b"0300")
_MPLS_HEADER_BYTES = 40
_MPLS_45KHZ = 45000.0  # PlayItem times are in 45 kHz units


def _mpls_total_duration(mpls_path: Path) -> float:
    """Sum all PlayItem durations in a .mpls file. Returns 0.0 on any
    parse failure (skips the file in the longest-playlist picker)."""
    try:
        data = mpls_path.read_bytes()
        if len(data) < _MPLS_HEADER_BYTES or data[:4] != _MPLS_MAGIC:
            return 0.0
        if data[4:8] not in _MPLS_VERSIONS:
            return 0.0
        pl_start = struct.unpack(">I", data[8:12])[0]
        # PlayList: 4 length + 2 reserved + 2 n_playitems + 2 n_subpaths = 10
        if pl_start + 10 > len(data):
            return 0.0
        n_playitems = struct.unpack(">H", data[pl_start + 6:pl_start + 8])[0]
        cursor = pl_start + 10
        total_ticks = 0
        for _ in range(n_playitems):
            if cursor + 22 > len(data):
                break
            pi_length = struct.unpack(">H", data[cursor:cursor + 2])[0]
            # IN_time at offset 14 within PlayItem; OUT_time at 18.
            in_time = struct.unpack(">I", data[cursor + 14:cursor + 18])[0]
            out_time = struct.unpack(">I", data[cursor + 18:cursor + 22])[0]
            total_ticks += max(0, out_time - in_time)
            cursor += 2 + pi_length  # skip the whole PlayItem
        return total_ticks / _MPLS_45KHZ
    except Exception:
        return 0.0


def _find_main_bdmv_playlist(playlist_dir: Path) -> Optional[Path]:
    """Return the .mpls file under `playlist_dir` with the largest total
    PlayItem duration. Replicates libbluray's default 'longest title'
    pick. Returns None if no .mpls file parses successfully."""
    if not playlist_dir.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for mpls in sorted(playlist_dir.glob("*.mpls")):
        dur = _mpls_total_duration(mpls)
        if dur > 0:
            candidates.append((dur, mpls))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]
