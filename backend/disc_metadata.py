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


def _extract_dvd_langs(
    data: bytes,
    n_declared: int,
    start_offset: int,
    attr_size: int,
    max_count: int,
) -> list[str]:
    """Extract ISO 639-2 language codes from a DVD attr array.

    Each attr entry has lang_code at +2 (2 bytes ASCII ISO 639-1).
    Trusts `n_declared` when > 0. When `n_declared == 0` falls back to
    scanning the attr array for non-zero lang_code bytes, stopping at
    the first all-zero entry — handles DVDs where the authoring tool
    populated audio/subp attrs without updating the count byte (a known
    libdvdread compatibility quirk; Fast-Walking 1982 is one such disc).
    """
    if n_declared > 0:
        codes: list[str] = []
        for i in range(n_declared):
            off = start_offset + i * attr_size + 2
            code = data[off:off + 2].decode("ascii", errors="replace")
            codes.append(_iso639_1_to_2(code))
        return codes

    # n_declared == 0 — authoring quirk fallback. Scan attrs until the
    # first all-zero lang_code (sentinel gap), but cap at max_count.
    codes = []
    for i in range(max_count):
        off = start_offset + i * attr_size + 2
        lang_bytes = data[off:off + 2]
        if lang_bytes == b"\x00\x00":
            break
        code = lang_bytes.decode("ascii", errors="replace")
        codes.append(_iso639_1_to_2(code))
    return codes


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
        audio = _extract_dvd_langs(
            data,
            n_declared=n_audio,
            start_offset=_DVD_AUDIO_ATTR_OFFSET,
            attr_size=_DVD_AUDIO_ATTR_SIZE,
            max_count=_DVD_AUDIO_MAX,
        )

        n_subp = min(data[_DVD_SUBP_COUNT_OFFSET], _DVD_SUBP_MAX)
        subtitle = _extract_dvd_langs(
            data,
            n_declared=n_subp,
            start_offset=_DVD_SUBP_ATTR_OFFSET,
            attr_size=_DVD_SUBP_ATTR_SIZE,
            max_count=_DVD_SUBP_MAX,
        )

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


# BDMV .mpls STN_table layout (BD-ROM Part 3):
# After the PlayItem header fields (and optional multi_clip block if
# multi_angle is set, which we don't support here), the STN_table is:
#   uint16 BE  : length (after this field)
#   uint16 BE  : reserved
#   uint8      : n_primary_video
#   uint8      : n_primary_audio
#   uint8      : n_primary_pg (subtitles)
#   uint8      : n_primary_ig (menus)
#   uint8      : n_secondary_audio
#   uint8      : n_secondary_video
#   uint8      : n_pip_pg
#   bytes[5]   : reserved
# Then stream blocks in order: video, audio, pg, ig.
# Each block has a StreamEntry (length-prefixed) + StreamAttributes (length-prefixed).
# StreamEntry: byte[0] = length-of-rest; byte[1] = type; remaining = type-specific (skip)
# StreamAttributes for audio (coding_type 0x80-0x86, 0xA1, 0xA2):
#   byte[0]    = length-of-rest
#   byte[1]    = stream_coding_type
#   byte[2]    = audio_format/sample_rate (packed)
#   bytes[3:6] = lang_code (3-byte ASCII ISO 639-2)
# StreamAttributes for PG (coding_type 0x90):
#   byte[0]    = length-of-rest
#   byte[1]    = stream_coding_type (0x90)
#   bytes[2:5] = lang_code (3-byte ASCII ISO 639-2)
_BDMV_AUDIO_CODING_TYPES = {0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0xA1, 0xA2}
_BDMV_PG_CODING_TYPE = 0x90


def _parse_bdmv_mpls(mpls_path: Path) -> dict[str, list[str]]:
    """Parse a single .mpls file's primary audio + PG stream language
    codes from the first PlayItem's STN_table.

    Returns {"audio": [iso639_2, ...], "subtitle": [iso639_2, ...]} in
    stream order. Empty strings for blank/whitespace codes. Empty lists
    on any read or parse failure.
    """
    try:
        data = mpls_path.read_bytes()
    except OSError as exc:
        print(f"[DISC-META] could not read {mpls_path}: {exc}", flush=True)
        return {"audio": [], "subtitle": []}

    try:
        if len(data) < _MPLS_HEADER_BYTES or data[:4] != _MPLS_MAGIC:
            return {"audio": [], "subtitle": []}
        if data[4:8] not in _MPLS_VERSIONS:
            return {"audio": [], "subtitle": []}

        pl_start = struct.unpack(">I", data[8:12])[0]
        if pl_start + 10 > len(data):
            return {"audio": [], "subtitle": []}

        n_playitems = struct.unpack(">H", data[pl_start + 6:pl_start + 8])[0]
        if n_playitems < 1:
            return {"audio": [], "subtitle": []}

        # First PlayItem starts at pl_start + 10
        pi_off = pl_start + 10
        pi_length = struct.unpack(">H", data[pi_off:pi_off + 2])[0]
        # PlayItem fixed header: length(2) + clip_id(5) + codec_id(4) +
        # flags(2) + ref(1) + IN(4) + OUT(4) + UO(8) + flags(1) +
        # still_mode(1) + still_time(2) = 34 bytes.
        # If is_multi_angle (bit 4 of flags at +11): extra multi_clip data follows.
        # We don't support multi_angle parsing here; if set, skip this playlist.
        flags = struct.unpack(">H", data[pi_off + 11:pi_off + 13])[0]
        is_multi_angle = bool(flags & 0x10)
        if is_multi_angle:
            # Multi-angle playlists are rare; bail out, caller picks next-longest.
            return {"audio": [], "subtitle": []}

        stn_off = pi_off + 34
        if stn_off + 4 > len(data):
            return {"audio": [], "subtitle": []}
        stn_length = struct.unpack(">H", data[stn_off:stn_off + 2])[0]
        stn_end = stn_off + 2 + stn_length
        if stn_end > len(data):
            return {"audio": [], "subtitle": []}

        # Stream counts at stn_off + 4 (skip length+reserved)
        n_video = data[stn_off + 4]
        n_audio = data[stn_off + 5]
        n_pg = data[stn_off + 6]
        # Stream blocks start at stn_off + 4 + 12 (counts) = stn_off + 16
        cursor = stn_off + 16

        def skip_stream(cur: int) -> int:
            """Skip one StreamEntry+StreamAttributes pair, return new cursor."""
            entry_len = data[cur]
            cur += 1 + entry_len
            attr_len = data[cur]
            cur += 1 + attr_len
            return cur

        def read_audio_lang(cur: int) -> tuple[str, int]:
            entry_len = data[cur]
            cur += 1 + entry_len  # skip StreamEntry
            attr_len = data[cur]
            cur += 1
            # attr_len bytes follow: byte0=coding_type, byte1=audio_format,
            # bytes2-4 = lang_code
            if attr_len >= 5 and data[cur] in _BDMV_AUDIO_CODING_TYPES:
                lang = data[cur + 2:cur + 5].decode("ascii", errors="replace").strip()
            else:
                lang = ""
            cur += attr_len
            return lang, cur

        def read_pg_lang(cur: int) -> tuple[str, int]:
            entry_len = data[cur]
            cur += 1 + entry_len
            attr_len = data[cur]
            cur += 1
            if attr_len >= 4 and data[cur] == _BDMV_PG_CODING_TYPE:
                lang = data[cur + 1:cur + 4].decode("ascii", errors="replace").strip()
            else:
                lang = ""
            cur += attr_len
            return lang, cur

        # Skip video streams (we don't need their langs)
        for _ in range(n_video):
            cursor = skip_stream(cursor)

        # Read audio
        audio = []
        for _ in range(n_audio):
            lang, cursor = read_audio_lang(cursor)
            audio.append(lang)

        # Read PG
        subtitle = []
        for _ in range(n_pg):
            lang, cursor = read_pg_lang(cursor)
            subtitle.append(lang)

        return {"audio": audio, "subtitle": subtitle}
    except Exception as exc:
        print(f"[DISC-META] mpls parse failed for {mpls_path}: {exc}", flush=True)
        return {"audio": [], "subtitle": []}


def parse_disc_languages(disc_root: Path, disc_type: str) -> dict[str, list[str]]:
    """Public entry point. Given a disc-root folder and disc_type ('dvd'
    or 'bdmv'), return per-stream language metadata.

    DVD: locates the main title set's VTS_NN_0.IFO and parses it.
    BDMV: locates the longest .mpls in BDMV/PLAYLIST and parses it.

    Returns {"audio": [...], "subtitle": [...]} on success, or
    {"audio": [], "subtitle": []} on any error. Never raises.
    """
    try:
        if disc_type == "dvd":
            # Use the same title-set picker that v0.6.2 uses for the
            # concat: VOB list. Same NN → same IFO.
            # Lazy import — `backend.scanner` imports `parse_disc_languages`
            # at module level (probe_file integration); a top-level
            # `from backend.scanner import ...` here would create a circular
            # import. Importing inside the function defers resolution until
            # call time, by which point both modules are fully loaded.
            from backend.scanner import _dvd_main_title_vobs
            vobs = _dvd_main_title_vobs(disc_root)
            if not vobs:
                return {"audio": [], "subtitle": []}
            # VOB name shape: VTS_NN_M.VOB → IFO is VTS_NN_0.IFO
            first_vob = vobs[0]
            parts = first_vob.stem.split("_")  # ['VTS', '01', '1']
            if len(parts) != 3:
                return {"audio": [], "subtitle": []}
            ts_num = parts[1]
            ifo = disc_root / "VIDEO_TS" / f"VTS_{ts_num}_0.IFO"
            return _parse_dvd_ifo(ifo)
        elif disc_type == "bdmv":
            playlist_dir = disc_root / "BDMV" / "PLAYLIST"
            mpls = _find_main_bdmv_playlist(playlist_dir)
            if mpls is None:
                return {"audio": [], "subtitle": []}
            return _parse_bdmv_mpls(mpls)
        else:
            return {"audio": [], "subtitle": []}
    except Exception as exc:
        print(f"[DISC-META] parse_disc_languages failed for {disc_root} ({disc_type}): {exc}", flush=True)
        return {"audio": [], "subtitle": []}
