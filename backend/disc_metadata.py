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


def _parse_dvd_ifo_bytes(data: bytes) -> dict[str, list[str]]:
    """Parse a DVD VTS IFO from raw bytes (no file I/O).

    Returns {"audio": [iso639_2, ...], "subtitle": [iso639_2, ...]} in
    stream order. Empty strings for unknown/unmapped codes. Empty lists
    on any parse failure. v0.7.0+: extracted from _parse_dvd_ifo to
    allow ISO-side callers to feed bytes from pycdlib.
    """
    if len(data) < _DVD_IFO_HEADER_BYTES or data[:12] != _DVD_IFO_MAGIC:
        print(f"[DISC-META] bad/short IFO bytes (len={len(data)})", flush=True)
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
        print(f"[DISC-META] IFO parse failed: {exc}", flush=True)
        return {"audio": [], "subtitle": []}


def _parse_dvd_ifo(ifo_path: Path) -> dict[str, list[str]]:
    """Parse a DVD VTS IFO file path. Thin wrapper around
    _parse_dvd_ifo_bytes for path-based callers."""
    try:
        data = ifo_path.read_bytes()
    except OSError as exc:
        print(f"[DISC-META] could not read {ifo_path}: {exc}", flush=True)
        return {"audio": [], "subtitle": []}
    return _parse_dvd_ifo_bytes(data)


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


def _mpls_total_duration_bytes(data: bytes) -> float:
    """Sum PlayItem durations from .mpls raw bytes. Returns 0.0 on any
    parse failure. v0.7.0+."""
    try:
        if len(data) < _MPLS_HEADER_BYTES or data[:4] != _MPLS_MAGIC:
            return 0.0
        if data[4:8] not in _MPLS_VERSIONS:
            return 0.0
        pl_start = struct.unpack(">I", data[8:12])[0]
        if pl_start + 10 > len(data):
            return 0.0
        n_playitems = struct.unpack(">H", data[pl_start + 6:pl_start + 8])[0]
        cursor = pl_start + 10
        total_ticks = 0
        for _ in range(n_playitems):
            if cursor + 22 > len(data):
                break
            pi_length = struct.unpack(">H", data[cursor:cursor + 2])[0]
            in_time = struct.unpack(">I", data[cursor + 14:cursor + 18])[0]
            out_time = struct.unpack(">I", data[cursor + 18:cursor + 22])[0]
            total_ticks += max(0, out_time - in_time)
            cursor += 2 + pi_length
        return total_ticks / _MPLS_45KHZ
    except Exception:
        return 0.0


def _mpls_total_duration(mpls_path: Path) -> float:
    """Sum PlayItem durations from a .mpls file path. Thin wrapper."""
    try:
        return _mpls_total_duration_bytes(mpls_path.read_bytes())
    except OSError:
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


def _parse_bdmv_mpls_bytes(data: bytes) -> dict[str, list[str]]:
    """Parse a BDMV .mpls from raw bytes. Reads STN_table from the first
    PlayItem. v0.7.0+: extracted from _parse_bdmv_mpls for ISO-side
    callers."""
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

        pi_off = pl_start + 10
        flags = struct.unpack(">H", data[pi_off + 11:pi_off + 13])[0]
        is_multi_angle = bool(flags & 0x10)
        if is_multi_angle:
            return {"audio": [], "subtitle": []}

        stn_off = pi_off + 34
        if stn_off + 4 > len(data):
            return {"audio": [], "subtitle": []}
        stn_length = struct.unpack(">H", data[stn_off:stn_off + 2])[0]
        stn_end = stn_off + 2 + stn_length
        if stn_end > len(data):
            return {"audio": [], "subtitle": []}

        n_video = data[stn_off + 4]
        n_audio = data[stn_off + 5]
        n_pg = data[stn_off + 6]
        cursor = stn_off + 16

        def skip_stream(cur: int) -> int:
            entry_len = data[cur]
            cur += 1 + entry_len
            attr_len = data[cur]
            cur += 1 + attr_len
            return cur

        def read_audio_lang(cur: int) -> tuple[str, int]:
            entry_len = data[cur]
            cur += 1 + entry_len
            attr_len = data[cur]
            cur += 1
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

        for _ in range(n_video):
            cursor = skip_stream(cursor)

        audio = []
        for _ in range(n_audio):
            lang, cursor = read_audio_lang(cursor)
            audio.append(lang)

        subtitle = []
        for _ in range(n_pg):
            lang, cursor = read_pg_lang(cursor)
            subtitle.append(lang)

        return {"audio": audio, "subtitle": subtitle}
    except Exception as exc:
        print(f"[DISC-META] mpls parse failed: {exc}", flush=True)
        return {"audio": [], "subtitle": []}


def _parse_bdmv_mpls(mpls_path: Path) -> dict[str, list[str]]:
    """Parse a .mpls file path. Thin wrapper around _parse_bdmv_mpls_bytes."""
    try:
        data = mpls_path.read_bytes()
    except OSError as exc:
        print(f"[DISC-META] could not read {mpls_path}: {exc}", flush=True)
        return {"audio": [], "subtitle": []}
    return _parse_bdmv_mpls_bytes(data)


def _iso_has_path(iso, path: str) -> bool:
    """Check existence of a path inside an ISO via UDF facade first,
    then ISO 9660 (with optional ';1' version suffix). Used by the
    classifier and the sidecar extraction helpers. v0.7.0+."""
    try:
        iso.get_record(udf_path=path)
        return True
    except Exception:
        pass
    try:
        iso.get_record(iso_path=path + ";1")
        return True
    except Exception:
        pass
    try:
        iso.get_record(iso_path=path)
        return True
    except Exception:
        return False


def _classify_disc_iso(iso_path: Path) -> Optional[str]:
    """Peek inside an ISO file and return 'dvd', 'bdmv', or None.

    BDMV wins on combo discs (same priority as folder-based
    `_classify_disc` in scanner.py). Uses pycdlib to read UDF + ISO 9660
    directory tables — no payload extraction at this stage. Fail-open:
    any pycdlib error returns None so non-video ISOs are silently
    skipped rather than blocking the scan.

    v0.7.0+: ffmpeg fallback for ISOs pycdlib can't open. UDF-only BD
    ISOs (no ISO 9660 layer) trigger this path — pycdlib requires a
    Primary Volume Descriptor which UDF-only discs lack. ffmpeg's
    `bluray:` protocol opens them via libbluray. Slower (~1-5s per
    probe) but handles real-world BD ISOs that pycdlib rejects.
    """
    if not iso_path.is_file():
        return None

    try:
        import pycdlib
    except ImportError:
        print("[DISC-META] pycdlib not installed; ISO support disabled", flush=True)
        # No pycdlib at all — go straight to ffmpeg fallback
        return _classify_disc_iso_via_ffmpeg(iso_path)

    iso = pycdlib.PyCdlib()
    try:
        iso.open(str(iso_path))
    except Exception as exc:
        # pycdlib couldn't open — likely a UDF-only BD ISO (no ISO 9660 PVD)
        # or some other format pycdlib doesn't support. Try ffmpeg.
        print(f"[DISC-META] pycdlib couldn't open {iso_path} ({exc}); trying ffmpeg fallback", flush=True)
        return _classify_disc_iso_via_ffmpeg(iso_path)

    try:
        # Check BDMV first (combo-disc priority)
        if _iso_has_path(iso, "/BDMV/index.bdmv"):
            return "bdmv"
        if _iso_has_path(iso, "/VIDEO_TS/VIDEO_TS.IFO"):
            return "dvd"
        return None
    finally:
        try:
            iso.close()
        except Exception:
            pass


def _classify_disc_iso_via_ffmpeg(iso_path: Path) -> Optional[str]:
    """Fallback classification using ffmpeg's `bluray:` and `-f dvdvideo`
    input syntaxes. Slower than pycdlib (~1-5 seconds per probe) but
    works on UDF-only ISOs that pycdlib rejects. v0.7.0+.

    Order: BD first (more common for ISOs), then DVD. Returns None if
    neither probe yields streams (non-video ISO or unreadable image).
    """
    import subprocess
    import json as _json

    # Try BD via bluray: protocol
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams",
             "-print_format", "json", "-i", f"bluray:{iso_path}"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            try:
                data = _json.loads(r.stdout)
                if data.get("streams"):
                    return "bdmv"
            except _json.JSONDecodeError:
                pass
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        print(f"[DISC-META] ffmpeg bluray fallback errored for {iso_path}: {exc}", flush=True)

    # Try DVD via -f dvdvideo
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams",
             "-print_format", "json", "-f", "dvdvideo", "-i", str(iso_path)],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            try:
                data = _json.loads(r.stdout)
                if data.get("streams"):
                    return "dvd"
            except _json.JSONDecodeError:
                pass
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        print(f"[DISC-META] ffmpeg dvdvideo fallback errored for {iso_path}: {exc}", flush=True)

    return None


def _extract_iso_file(iso, path: str) -> bytes:
    """Read a file from inside an open pycdlib ISO. Tries UDF facade
    first, then ISO 9660 (with optional ';1' version suffix).
    Raises FileNotFoundError if the path is absent in both facades.
    v0.7.0+."""
    import io as _io_mod
    last_exc = None
    for kwargs in (
        {"udf_path": path},
        {"iso_path": path + ";1"},
        {"iso_path": path},
    ):
        try:
            buf = _io_mod.BytesIO()
            iso.get_file_from_iso_fp(buf, **kwargs)
            return buf.getvalue()
        except Exception as exc:
            last_exc = exc
            continue
    raise FileNotFoundError(f"{path} not found in ISO: {last_exc}")


def _pick_main_vts_in_iso(iso) -> Optional[str]:
    """Enumerate /VIDEO_TS/VTS_NN_*.VOB inside an open ISO, group by NN,
    sum byte sizes (excluding _0 menu chunks), return the NN with the
    largest total. Returns None if no candidate found. v0.7.0+ — mirrors
    folder-based `_dvd_main_title_vobs` logic."""
    import re as _re_mod
    title_sets: dict[str, int] = {}
    # Walk the /VIDEO_TS UDF dir; fall back to ISO 9660 if UDF empty.
    for walker_kw in ("udf_path", "iso_path"):
        try:
            children = list(iso.list_children(**{walker_kw: "/VIDEO_TS"}))
        except Exception:
            continue
        found_any = False
        for child in children:
            if child is None:
                continue
            try:
                name = child.file_identifier().decode("utf-8", errors="replace").strip()
            except Exception:
                continue
            if name in (".", "..", ""):
                continue
            # Strip ;1 version suffix from ISO 9660 paths
            name = name.split(";", 1)[0]
            if name.startswith("."):
                continue  # AppleDouble companions
            m = _re_mod.fullmatch(r"VTS_(\d{2})_(\d+)\.VOB", name, _re_mod.IGNORECASE)
            if not m:
                continue
            found_any = True
            ts_num = m.group(1)
            chunk = int(m.group(2))
            if chunk == 0:
                continue  # skip menu chunk
            # File size via the directory record
            try:
                size = child.get_data_length()
            except Exception:
                size = 0
            title_sets[ts_num] = title_sets.get(ts_num, 0) + size
        if found_any:
            break  # don't double-count from a second walker

    if not title_sets:
        return None
    return max(title_sets, key=title_sets.get)


def _pick_main_mpls_in_iso(iso) -> Optional[bytes]:
    """Enumerate /BDMV/PLAYLIST/*.mpls inside an open ISO, extract each
    (small files, ~1 KB), pick the one with the largest total PlayItem
    duration, return its bytes. Returns None if no playlists found.
    v0.7.0+ — mirrors folder-based `_find_main_bdmv_playlist`."""
    candidates: list[tuple[float, bytes]] = []
    for walker_kw in ("udf_path", "iso_path"):
        try:
            children = list(iso.list_children(**{walker_kw: "/BDMV/PLAYLIST"}))
        except Exception:
            continue
        found_any = False
        for child in children:
            if child is None:
                continue
            try:
                name = child.file_identifier().decode("utf-8", errors="replace").strip()
            except Exception:
                continue
            name = name.split(";", 1)[0]
            if not name.lower().endswith(".mpls"):
                continue
            found_any = True
            try:
                data = _extract_iso_file(iso, f"/BDMV/PLAYLIST/{name}")
            except FileNotFoundError:
                continue
            dur = _mpls_total_duration_bytes(data)
            if dur > 0:
                candidates.append((dur, data))
        if found_any:
            break

    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def _bsdtar_available() -> bool:
    """Check if bsdtar is on PATH. v0.7.1+ uses it as a fallback to
    pycdlib for UDF-only ISOs."""
    import shutil as _shutil
    return _shutil.which("bsdtar") is not None


def _bsdtar_list_iso(iso_path: Path) -> list[str]:
    """List ALL files inside an ISO via bsdtar. Returns full paths with
    leading slash. Empty list on any error. v0.7.1+."""
    import subprocess
    try:
        r = subprocess.run(
            ["bsdtar", "-tf", str(iso_path)],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return []
        # bsdtar emits paths without leading slash; normalize.
        return ["/" + line.rstrip("/") for line in r.stdout.splitlines() if line.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        print(f"[DISC-META] bsdtar list failed for {iso_path}: {exc}", flush=True)
        return []


def _bsdtar_extract_iso_file(iso_path: Path, internal_path: str) -> bytes:
    """Extract a single file from inside an ISO to bytes via bsdtar.
    `internal_path` should match the bsdtar listing (typically no leading
    slash). Returns bytes on success. Raises FileNotFoundError on any
    failure (matches pycdlib's _extract_iso_file contract). v0.7.1+."""
    import subprocess
    # bsdtar -xOf <iso> <path>: extract to stdout. bsdtar paths don't
    # have a leading slash.
    rel = internal_path.lstrip("/")
    try:
        r = subprocess.run(
            ["bsdtar", "-xOf", str(iso_path), rel],
            capture_output=True, timeout=60,
        )
        if r.returncode == 0 and r.stdout:
            return r.stdout
        raise FileNotFoundError(
            f"bsdtar couldn't extract {rel} from {iso_path}: rc={r.returncode}, stderr={r.stderr[:200].decode('utf-8', 'replace')!r}"
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        raise FileNotFoundError(f"bsdtar extract failed for {rel}: {exc}")


def _pick_main_mpls_via_bsdtar(iso_path: Path) -> Optional[bytes]:
    """Enumerate .mpls files inside an ISO via bsdtar, extract each
    (small ~1 KB), parse duration via _mpls_total_duration_bytes, return
    the longest playlist's bytes. None if no playlists. v0.7.1+.

    Used when pycdlib can't open the ISO (UDF-only BD ISO).
    """
    entries = _bsdtar_list_iso(iso_path)
    candidates: list[tuple[float, bytes]] = []
    for entry in entries:
        # bsdtar listings vary in casing; normalize to upper for the
        # check. mpls files live at BDMV/PLAYLIST/*.mpls.
        normalized = entry.upper()
        if not normalized.endswith(".MPLS"):
            continue
        if "/BDMV/PLAYLIST/" not in normalized:
            continue
        try:
            data = _bsdtar_extract_iso_file(iso_path, entry)
        except FileNotFoundError:
            continue
        dur = _mpls_total_duration_bytes(data)
        if dur > 0:
            candidates.append((dur, data))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def _pick_main_vts_via_bsdtar(iso_path: Path) -> Optional[str]:
    """Enumerate VTS_NN_*.VOB inside an ISO via bsdtar, group by NN, sum
    sizes, return the NN with the largest total. None if no candidates.
    v0.7.1+ (parallel path to _pick_main_vts_in_iso for ISOs pycdlib
    can't open). Note: bsdtar listing doesn't give file sizes directly,
    so we use `bsdtar -tvf` for verbose listing.
    """
    import subprocess, re as _re_mod
    try:
        r = subprocess.run(
            ["bsdtar", "-tvf", str(iso_path)],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return None
        lines = r.stdout.splitlines()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        print(f"[DISC-META] bsdtar verbose list failed for {iso_path}: {exc}", flush=True)
        return None

    # bsdtar -tvf output shape (libarchive):
    #   -rwxrwxr-x  0 0      0     1073739776 Jan  1 1970 VIDEO_TS/VTS_01_1.VOB
    # We want size (col 4 after splitting on whitespace) + path (col 8+)
    title_sets: dict[str, int] = {}
    for line in lines:
        parts = line.split(None, 8)
        if len(parts) < 9:
            continue
        try:
            size = int(parts[4])
        except ValueError:
            continue
        path = parts[8]
        # Match VTS_NN_M.VOB pattern in the path
        m = _re_mod.search(r"VTS_(\d{2})_(\d+)\.VOB$", path, _re_mod.IGNORECASE)
        if not m:
            continue
        chunk = int(m.group(2))
        if chunk == 0:
            continue  # skip menu chunk
        ts_num = m.group(1)
        title_sets[ts_num] = title_sets.get(ts_num, 0) + size
    if not title_sets:
        return None
    return max(title_sets, key=title_sets.get)


def parse_disc_languages_iso(iso_path: Path, disc_type: str) -> dict[str, list[str]]:
    """Extract per-stream language codes from an ISO file via pycdlib.

    DVD: find the main title set via VOB-size heuristic, extract that
    NN's VTS_NN_0.IFO bytes, feed to _parse_dvd_ifo_bytes.

    BDMV: enumerate all .mpls in BDMV/PLAYLIST, pick the longest by total
    PlayItem duration, feed its bytes to _parse_bdmv_mpls_bytes.

    Fail-open: any pycdlib / parse error returns {"audio": [], "subtitle": []}.
    v0.7.0+.
    """
    try:
        import pycdlib
    except ImportError:
        print("[DISC-META] pycdlib not installed; ISO language metadata unavailable", flush=True)
        return {"audio": [], "subtitle": []}

    if not iso_path.is_file():
        return {"audio": [], "subtitle": []}

    iso = pycdlib.PyCdlib()
    try:
        iso.open(str(iso_path))
    except Exception as exc:
        # v0.7.1+ bsdtar fallback first (DVD ISOs sometimes parse via libarchive
        # where pycdlib fails)
        print(
            f"[DISC-META] pycdlib couldn't open {iso_path} ({exc}); "
            "trying bsdtar fallback for language metadata",
            flush=True,
        )
        if _bsdtar_available():
            result = _parse_disc_languages_iso_via_bsdtar(iso_path, disc_type)
            # bsdtar fallback returns empty dict on failure (not None) — check
            # for non-empty audio as a "success" signal
            if result["audio"] or result["subtitle"]:
                return result
        # v0.7.4: libbluray fallback for BD ISOs (UDF-only BDs where libarchive
        # also fails). Only applies to BDMV inputs — DVDs use libdvdread paths.
        if disc_type == "bdmv":
            print(
                f"[DISC-META] bsdtar fallback didn't produce results; "
                f"trying libbluray ctypes for {iso_path}",
                flush=True,
            )
            result = _parse_disc_languages_iso_via_libbluray(iso_path)
            if result is not None:
                return result
        return {"audio": [], "subtitle": []}

    try:
        if disc_type == "dvd":
            ts_num = _pick_main_vts_in_iso(iso)
            if not ts_num:
                return {"audio": [], "subtitle": []}
            try:
                ifo_bytes = _extract_iso_file(iso, f"/VIDEO_TS/VTS_{ts_num}_0.IFO")
            except FileNotFoundError as exc:
                print(f"[DISC-META] IFO not found in ISO {iso_path}: {exc}", flush=True)
                return {"audio": [], "subtitle": []}
            return _parse_dvd_ifo_bytes(ifo_bytes)
        elif disc_type == "bdmv":
            mpls_bytes = _pick_main_mpls_in_iso(iso)
            if mpls_bytes is None:
                return {"audio": [], "subtitle": []}
            return _parse_bdmv_mpls_bytes(mpls_bytes)
        else:
            return {"audio": [], "subtitle": []}
    except Exception as exc:
        print(f"[DISC-META] parse_disc_languages_iso failed for {iso_path} ({disc_type}): {exc}", flush=True)
        return {"audio": [], "subtitle": []}
    finally:
        try:
            iso.close()
        except Exception:
            pass


def _parse_disc_languages_iso_via_bsdtar(iso_path: Path, disc_type: str) -> dict[str, list[str]]:
    """Fallback to bsdtar (libarchive) for ISO sidecar extraction when
    pycdlib can't open the image. Mirrors the pycdlib-based flow:

      DVD: find main title set via VOB-size heuristic, extract that
           NN's VTS_NN_0.IFO bytes, feed to _parse_dvd_ifo_bytes.
      BDMV: enumerate .mpls files, pick the longest, feed bytes to
            _parse_bdmv_mpls_bytes.

    Fail-open: returns empty on any extraction or parse failure.
    v0.7.1+.
    """
    try:
        if disc_type == "dvd":
            ts_num = _pick_main_vts_via_bsdtar(iso_path)
            if not ts_num:
                return {"audio": [], "subtitle": []}
            try:
                ifo_bytes = _bsdtar_extract_iso_file(
                    iso_path, f"VIDEO_TS/VTS_{ts_num}_0.IFO"
                )
            except FileNotFoundError as exc:
                print(
                    f"[DISC-META] bsdtar: IFO not found in {iso_path}: {exc}",
                    flush=True,
                )
                return {"audio": [], "subtitle": []}
            return _parse_dvd_ifo_bytes(ifo_bytes)
        elif disc_type == "bdmv":
            mpls_bytes = _pick_main_mpls_via_bsdtar(iso_path)
            if mpls_bytes is None:
                return {"audio": [], "subtitle": []}
            return _parse_bdmv_mpls_bytes(mpls_bytes)
        return {"audio": [], "subtitle": []}
    except Exception as exc:
        print(
            f"[DISC-META] bsdtar-based parse failed for {iso_path} "
            f"({disc_type}): {exc}",
            flush=True,
        )
        return {"audio": [], "subtitle": []}


def _parse_disc_languages_iso_via_libbluray(iso_path: Path) -> Optional[dict[str, list[str]]]:
    """v0.7.4+: extract per-stream language codes via libbluray's C API.

    Used as a third-tier fallback when pycdlib can't open the ISO (no
    ISO 9660 PVD) AND bsdtar/libarchive can't list it (UDF revision
    incompatible). libbluray inside ffmpeg already reads these ISOs for
    encoding; we use the same library via Python ctypes against the
    system's libbluray.so.2 (installed via the libbluray-bin apt package
    in v0.7.4).

    Returns {"audio": [iso639_2, ...], "subtitle": [iso639_2, ...]} on
    success. Returns None if libbluray isn't loadable or fails to open
    the ISO. Callers should fall back to fail-open empty on None.

    Implementation: bd_open(iso) → bd_get_main_title() → bd_get_title_info()
    → read clips[0].{audio_streams, pg_streams}[i].lang. lang is 4 bytes:
    3-byte ISO 639-2 code + null terminator. Properly frees via
    bd_free_title_info() and bd_close().
    """
    import ctypes
    from ctypes import (
        c_uint8, c_uint16, c_uint32, c_uint64, c_int,
        c_char, c_void_p, c_char_p, POINTER, Structure,
    )

    # Try to load libbluray.so.2 (Debian's libbluray-bin's dep). Fall back
    # to .so.1 (older Debian) or full path. Return None if not available.
    libbluray = None
    for soname in ("libbluray.so.2", "libbluray.so.1", "libbluray.so"):
        try:
            libbluray = ctypes.CDLL(soname)
            break
        except OSError:
            continue
    if libbluray is None:
        print("[DISC-META] libbluray.so not found; can't use libbluray fallback", flush=True)
        return None

    # --- ctypes struct definitions ---
    # Match libbluray 1.3.x ABI on x86_64 Linux. Order MUST match
    # libbluray/bluray.h. ctypes handles padding automatically.

    class BLURAY_STREAM_INFO(Structure):
        _fields_ = [
            ("coding_type", c_uint8),
            ("format", c_uint8),
            ("rate", c_uint8),
            ("char_code", c_uint8),
            ("lang", c_char * 4),       # ISO 639-2 (3 chars + null)
            ("pid", c_uint16),
            ("aspect", c_uint8),
            ("subpath_id", c_uint8),
        ]

    class BLURAY_CLIP_INFO(Structure):
        _fields_ = [
            ("pkt_count", c_uint32),
            ("still_mode", c_uint8),
            ("still_time", c_uint16),
            ("video_stream_count", c_uint8),
            ("audio_stream_count", c_uint8),
            ("pg_stream_count", c_uint8),
            ("ig_stream_count", c_uint8),
            ("sec_audio_stream_count", c_uint8),
            ("sec_video_stream_count", c_uint8),
            ("video_streams", POINTER(BLURAY_STREAM_INFO)),
            ("audio_streams", POINTER(BLURAY_STREAM_INFO)),
            ("pg_streams", POINTER(BLURAY_STREAM_INFO)),
            ("ig_streams", POINTER(BLURAY_STREAM_INFO)),
            ("sec_audio_streams", POINTER(BLURAY_STREAM_INFO)),
            ("sec_video_streams", POINTER(BLURAY_STREAM_INFO)),
            ("start_time", c_uint64),
            ("in_time", c_uint64),
            ("out_time", c_uint64),
            ("clip_id", c_char * 6),
        ]

    class BLURAY_TITLE_INFO(Structure):
        _fields_ = [
            ("idx", c_uint32),
            ("playlist", c_uint32),
            ("duration", c_uint64),
            ("angle_count", c_uint8),
            ("chapter_count", c_uint32),
            ("clip_count", c_uint32),
            ("mark_count", c_uint32),
            ("chapters", c_void_p),     # BLURAY_TITLE_CHAPTER* (unused here)
            ("marks", c_void_p),        # BLURAY_TITLE_MARK* (unused here)
            ("clips", POINTER(BLURAY_CLIP_INFO)),
        ]

    # --- libbluray function signatures ---
    # bd_open(const char *device_path, const char *keyfile_path) -> BLURAY*
    libbluray.bd_open.argtypes = [c_char_p, c_char_p]
    libbluray.bd_open.restype = c_void_p
    # bd_close(BLURAY *bd) -> void
    libbluray.bd_close.argtypes = [c_void_p]
    libbluray.bd_close.restype = None
    # bd_get_main_title(BLURAY *bd) -> int (returns -1 on failure)
    libbluray.bd_get_main_title.argtypes = [c_void_p]
    libbluray.bd_get_main_title.restype = c_int
    # bd_get_title_info(BLURAY *bd, uint32_t title_idx, unsigned int angle) -> BLURAY_TITLE_INFO*
    libbluray.bd_get_title_info.argtypes = [c_void_p, c_uint32, c_uint32]
    libbluray.bd_get_title_info.restype = POINTER(BLURAY_TITLE_INFO)
    # bd_free_title_info(BLURAY_TITLE_INFO *info) -> void
    libbluray.bd_free_title_info.argtypes = [POINTER(BLURAY_TITLE_INFO)]
    libbluray.bd_free_title_info.restype = None

    # --- Open the disc ---
    bd = libbluray.bd_open(str(iso_path).encode("utf-8"), None)
    if not bd:
        print(f"[DISC-META] libbluray bd_open failed for {iso_path}", flush=True)
        return None

    try:
        main_title_idx = libbluray.bd_get_main_title(bd)
        if main_title_idx < 0:
            print(f"[DISC-META] libbluray bd_get_main_title failed for {iso_path}", flush=True)
            return None

        ti_ptr = libbluray.bd_get_title_info(bd, c_uint32(main_title_idx), c_uint32(0))
        if not ti_ptr:
            print(f"[DISC-META] libbluray bd_get_title_info failed for {iso_path}", flush=True)
            return None

        try:
            ti = ti_ptr.contents
            if ti.clip_count == 0:
                return {"audio": [], "subtitle": []}
            clip = ti.clips[0]

            audio_langs: list[str] = []
            for i in range(clip.audio_stream_count):
                stream = clip.audio_streams[i]
                # lang is a 4-byte ASCII field with a null terminator
                lang_bytes = bytes(stream.lang).rstrip(b"\x00")
                lang = lang_bytes.decode("ascii", errors="replace").strip()
                audio_langs.append(lang)

            pg_langs: list[str] = []
            for i in range(clip.pg_stream_count):
                stream = clip.pg_streams[i]
                lang_bytes = bytes(stream.lang).rstrip(b"\x00")
                lang = lang_bytes.decode("ascii", errors="replace").strip()
                pg_langs.append(lang)

            return {"audio": audio_langs, "subtitle": pg_langs}
        finally:
            libbluray.bd_free_title_info(ti_ptr)
    except Exception as exc:
        print(f"[DISC-META] libbluray ctypes call failed for {iso_path}: {exc}", flush=True)
        return None
    finally:
        try:
            libbluray.bd_close(bd)
        except Exception:
            pass


def parse_disc_languages(path: Path, disc_type: str) -> dict[str, list[str]]:
    """Public entry point. Dispatches on path shape:

      • folder         → existing v0.6.5+ folder logic
      • .iso file      → new v0.7.0 ISO logic via pycdlib
      • anything else  → empty result

    Returns {"audio": [...], "subtitle": [...]} on success, empty lists
    on any failure (parser, ISO read, pycdlib missing, etc.). Never raises.
    """
    try:
        if path.is_dir():
            return _parse_disc_languages_folder(path, disc_type)
        if path.suffix.lower() == ".iso" and path.is_file():
            return parse_disc_languages_iso(path, disc_type)
        return {"audio": [], "subtitle": []}
    except Exception as exc:
        print(f"[DISC-META] parse_disc_languages failed for {path} ({disc_type}): {exc}", flush=True)
        return {"audio": [], "subtitle": []}


def _parse_disc_languages_folder(disc_root: Path, disc_type: str) -> dict[str, list[str]]:
    """v0.6.5+ folder-based language extraction. Extracted from the old
    parse_disc_languages body when the dispatcher was introduced in
    v0.7.0."""
    if disc_type == "dvd":
        # Lazy import — `backend.scanner` imports `parse_disc_languages`
        # at module level (probe_file integration); a top-level
        # `from backend.scanner import ...` here would create a circular
        # import. Importing inside the function defers resolution until
        # call time, by which point both modules are fully loaded.
        from backend.scanner import _dvd_main_title_vobs
        vobs = _dvd_main_title_vobs(disc_root)
        if not vobs:
            return {"audio": [], "subtitle": []}
        first_vob = vobs[0]
        parts = first_vob.stem.split("_")
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
    return {"audio": [], "subtitle": []}
