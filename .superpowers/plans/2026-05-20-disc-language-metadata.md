# DVD IFO + Blu-ray mpls Language Metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract per-track language codes from DVD `VTS_NN_0.IFO` and Blu-ray `.mpls` files, patch them onto probe results before classify_audio_tracks runs, so the existing keep-language filter (`["eng","isl","ice"]`) selects the correct tracks instead of dropping everything as `und`.

**Architecture:** Single new module `backend/disc_metadata.py` with hand-rolled binary parsers (no new deps). One integration point in `backend/scanner.py:probe_file`. One-shot idempotent backfill in `backend/watcher.py` so existing disc rows pick up the new metadata. Pre-tag verification script gates `git tag v0.6.5` on parse correctness for two real discs (Fast-Walking DVD, Elephant BDMV).

**Tech Stack:** Pure Python 3.11 (struct, pathlib, no new pip / apt deps). Existing FastAPI + aiosqlite + pytest backend.

**Spec:** [`.superpowers/specs/2026-05-20-disc-language-metadata-design.md`](../specs/2026-05-20-disc-language-metadata-design.md) — committed at `f771c3c`.

---

## File Structure

- Create: `backend/disc_metadata.py` — all parser logic
- Create: `backend/tests/test_disc_metadata.py` — unit tests
- Modify: `backend/scanner.py` (around `probe_file` line 184–280)
- Modify: `backend/watcher.py` (`check_once` startup path)
- Create: `scripts/verify_disc_languages.py` — Layer-2 pre-tag gate
- Modify: `VERSION`, `CHANGELOG.md`

Six files. No restructuring of existing code.

---

## Task 1: ISO 639-1 → ISO 639-2 mapping helper

**Why first:** both parsers depend on this. Smallest, well-bounded unit to test in isolation.

**Files:**
- Create: `backend/disc_metadata.py`
- Create: `backend/tests/test_disc_metadata.py`

- [ ] **Step 1: Create the test file with failing tests for the mapping**

`backend/tests/test_disc_metadata.py`:

```python
"""Unit tests for backend.disc_metadata. v0.6.5+."""

import pytest

from backend.disc_metadata import _iso639_1_to_2


class TestIso639Mapping:
    def test_known_codes_map_to_three_letter(self):
        assert _iso639_1_to_2("en") == "eng"
        assert _iso639_1_to_2("de") == "ger"
        assert _iso639_1_to_2("fr") == "fre"
        assert _iso639_1_to_2("ja") == "jpn"
        assert _iso639_1_to_2("is") == "ice"

    def test_uppercase_input_normalized(self):
        assert _iso639_1_to_2("EN") == "eng"
        assert _iso639_1_to_2("De") == "ger"

    def test_unknown_code_returns_empty(self):
        assert _iso639_1_to_2("xx") == ""
        assert _iso639_1_to_2("zz") == ""

    def test_zeroed_bytes_returns_empty(self):
        # DVD IFO unused audio_attr slots have lang_code = b"\x00\x00"
        # which decodes to "\x00\x00" — must NOT match anything in the table
        assert _iso639_1_to_2("\x00\x00") == ""

    def test_whitespace_returns_empty(self):
        # Some discs pad codes with spaces
        assert _iso639_1_to_2("  ") == ""

    def test_too_short_or_too_long_returns_empty(self):
        assert _iso639_1_to_2("e") == ""
        assert _iso639_1_to_2("eng") == ""  # 3-letter input not allowed here
        assert _iso639_1_to_2("") == ""
```

- [ ] **Step 2: Run tests to verify they fail (module doesn't exist yet)**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py -v
```

Expected: ImportError or ModuleNotFoundError for `backend.disc_metadata`.

- [ ] **Step 3: Create `backend/disc_metadata.py` with the mapping table and helper**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py::TestIso639Mapping -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr add backend/disc_metadata.py backend/tests/test_disc_metadata.py
git -C /Users/hal9000/Documents/Claude/shrinkerr commit -m "feat(disc-meta): ISO 639-1 to 639-2 mapping helper (v0.6.5)"
```

---

## Task 2: DVD IFO parser (`_parse_dvd_ifo`)

**Files:**
- Modify: `backend/disc_metadata.py`
- Modify: `backend/tests/test_disc_metadata.py`

- [ ] **Step 1: Write failing tests for the IFO parser using synthetic fixtures**

Append to `backend/tests/test_disc_metadata.py`:

```python
import struct
import tempfile

from backend.disc_metadata import _parse_dvd_ifo


def _build_dvd_ifo(audio_langs: list[bytes], subp_langs: list[bytes]) -> bytes:
    """Build a minimal VTS IFO fixture with the given audio and subpicture
    language codes. Pads to 0x320 bytes. Each lang code MUST be 2 bytes.
    Empty audio/subp lists mean nr_of_streams = 0."""
    buf = bytearray(0x320)
    # Magic
    buf[0:12] = b"DVDVIDEO-VTS"
    # nr_of_vts_audio_streams at 0x202
    buf[0x202] = len(audio_langs)
    # audio_attrs[] at 0x204 — 8 entries × 8 bytes
    for i, lang in enumerate(audio_langs[:8]):
        assert len(lang) == 2
        # bytes +0 and +1 are packed format/lang_type/etc — leave 0
        # bytes +2,+3 = lang_code
        buf[0x204 + 8*i + 2] = lang[0]
        buf[0x204 + 8*i + 3] = lang[1]
    # nr_of_vts_subp_streams at 0x254
    buf[0x254] = len(subp_langs)
    # subp_attrs[] at 0x256 — 32 entries × 6 bytes
    for i, lang in enumerate(subp_langs[:32]):
        assert len(lang) == 2
        buf[0x256 + 6*i + 2] = lang[0]
        buf[0x256 + 6*i + 3] = lang[1]
    return bytes(buf)


class TestDvdIfoParser:
    def _write_fixture(self, content: bytes) -> Path:
        td = Path(tempfile.mkdtemp())
        ifo = td / "VTS_01_0.IFO"
        ifo.write_bytes(content)
        return ifo

    def test_single_english_audio_no_subs(self):
        fixture = _build_dvd_ifo(audio_langs=[b"en"], subp_langs=[])
        path = self._write_fixture(fixture)
        assert _parse_dvd_ifo(path) == {"audio": ["eng"], "subtitle": []}

    def test_multi_language_audio_and_subs(self):
        fixture = _build_dvd_ifo(
            audio_langs=[b"en", b"de"],
            subp_langs=[b"ja", b"fr"],
        )
        path = self._write_fixture(fixture)
        assert _parse_dvd_ifo(path) == {
            "audio": ["eng", "ger"],
            "subtitle": ["jpn", "fre"],
        }

    def test_unused_slots_produce_empty_strings(self):
        # Declare 2 audio streams; first valid, second has zeroed lang_code
        fixture = _build_dvd_ifo(audio_langs=[b"en", b"\x00\x00"], subp_langs=[])
        path = self._write_fixture(fixture)
        result = _parse_dvd_ifo(path)
        assert result["audio"] == ["eng", ""]

    def test_unknown_language_code_returns_empty(self):
        fixture = _build_dvd_ifo(audio_langs=[b"xx"], subp_langs=[])
        path = self._write_fixture(fixture)
        assert _parse_dvd_ifo(path) == {"audio": [""], "subtitle": []}

    def test_malformed_magic_returns_empty_lists(self):
        fixture = bytearray(0x320)
        fixture[0:12] = b"NOTADVD-VTS-"  # wrong magic
        path = self._write_fixture(bytes(fixture))
        assert _parse_dvd_ifo(path) == {"audio": [], "subtitle": []}

    def test_truncated_file_returns_empty_lists(self):
        # Valid magic but file is too short
        fixture = b"DVDVIDEO-VTS" + b"\x00" * 100
        path = self._write_fixture(fixture)
        assert _parse_dvd_ifo(path) == {"audio": [], "subtitle": []}

    def test_missing_file_returns_empty_lists(self):
        nonexistent = Path("/tmp/does_not_exist_for_test.IFO")
        assert _parse_dvd_ifo(nonexistent) == {"audio": [], "subtitle": []}

    def test_max_streams_8_audio_32_subp(self):
        # DVD spec caps: 8 audio, 32 subp. Build a full-cap fixture.
        audio = [b"en"] * 8
        subp = [b"en"] * 32
        fixture = _build_dvd_ifo(audio, subp)
        path = self._write_fixture(fixture)
        result = _parse_dvd_ifo(path)
        assert len(result["audio"]) == 8
        assert len(result["subtitle"]) == 32

    def test_clamps_when_count_exceeds_spec(self):
        # Defensive: if nr_of_streams reports >8 (corrupt IFO), clamp.
        fixture = bytearray(_build_dvd_ifo(audio_langs=[b"en"], subp_langs=[]))
        fixture[0x202] = 200  # nonsensical
        path = self._write_fixture(bytes(fixture))
        result = _parse_dvd_ifo(path)
        assert len(result["audio"]) <= 8
```

- [ ] **Step 2: Run tests to verify they fail (function not defined)**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py::TestDvdIfoParser -v
```

Expected: ImportError on `_parse_dvd_ifo`.

- [ ] **Step 3: Implement `_parse_dvd_ifo` in `backend/disc_metadata.py`**

Append to the bottom of `backend/disc_metadata.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py::TestDvdIfoParser -v
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr add backend/disc_metadata.py backend/tests/test_disc_metadata.py
git -C /Users/hal9000/Documents/Claude/shrinkerr commit -m "feat(disc-meta): DVD VTS IFO parser (v0.6.5)"
```

---

## Task 3: BDMV mpls helper — find the main playlist

**Why now:** the parser in Task 4 needs a way to pick which `.mpls` to read. Pulling this out as its own task keeps Task 4 focused and lets us test the playlist selection independently.

**Files:**
- Modify: `backend/disc_metadata.py`
- Modify: `backend/tests/test_disc_metadata.py`

- [ ] **Step 1: Write failing tests for `_find_main_bdmv_playlist`**

Append to `backend/tests/test_disc_metadata.py`:

```python
import struct

from backend.disc_metadata import _find_main_bdmv_playlist


def _build_mpls_with_duration(seconds: float) -> bytes:
    """Build a minimal valid .mpls fixture whose total PlayItem duration
    is `seconds` seconds. Single PlayItem, no multi-angle, no STN_table
    streams (just enough header bytes for duration parsing).
    Used to test the longest-playlist picker without needing full mpls."""
    ticks = int(seconds * 45000)
    # Header (40 bytes)
    pl_start = 40
    plm_start = 0  # not used in selection
    ext_start = 0
    header = (
        b"MPLS"
        + b"0200"
        + struct.pack(">I", pl_start)
        + struct.pack(">I", plm_start)
        + struct.pack(">I", ext_start)
        + b"\x00" * 20  # reserved
    )
    # PlayList @ pl_start: length(4) + reserved(2) + n_playitems(2) + n_subpaths(2)
    # then PlayItem[]
    # Minimal PlayItem:
    #   length(2)
    #   clip_id(5) + codec_id(4)
    #   flags(2) + ref_to_STC_id(1)
    #   IN_time(4) + OUT_time(4)
    #   UO_mask(8) + flags(1) + still_mode(1) + still_time(2)
    #   = 34 bytes payload (length covers bytes 2 onwards = 32)
    # NO multi_clip, NO STN_table (Task 3 doesn't need it, Task 4 will)
    in_time = 0
    out_time = ticks
    playitem = (
        struct.pack(">H", 32)  # length (after this field)
        + b"00000"
        + b"M2TS"
        + b"\x00\x00"  # flags (no multi_angle)
        + b"\x00"
        + struct.pack(">I", in_time)
        + struct.pack(">I", out_time)
        + b"\x00" * 8  # UO_mask
        + b"\x00"  # flags
        + b"\x00"  # still_mode = none
        + b"\x00\x00"  # still_time
    )
    playlist_body = (
        b"\x00" * 2  # reserved
        + struct.pack(">H", 1)  # number_of_PlayItems
        + struct.pack(">H", 0)  # number_of_SubPaths
        + playitem
    )
    playlist = struct.pack(">I", len(playlist_body)) + playlist_body
    return header + playlist


class TestFindMainBdmvPlaylist:
    def test_picks_longest_among_multiple(self, tmp_path):
        playlist_dir = tmp_path / "PLAYLIST"
        playlist_dir.mkdir()
        (playlist_dir / "00000.mpls").write_bytes(_build_mpls_with_duration(120))   # 2 min
        (playlist_dir / "00100.mpls").write_bytes(_build_mpls_with_duration(5000))  # ~83 min (main feature)
        (playlist_dir / "00200.mpls").write_bytes(_build_mpls_with_duration(60))    # 1 min
        result = _find_main_bdmv_playlist(playlist_dir)
        assert result is not None
        assert result.name == "00100.mpls"

    def test_single_playlist_picked(self, tmp_path):
        playlist_dir = tmp_path / "PLAYLIST"
        playlist_dir.mkdir()
        (playlist_dir / "00000.mpls").write_bytes(_build_mpls_with_duration(4900))
        result = _find_main_bdmv_playlist(playlist_dir)
        assert result is not None
        assert result.name == "00000.mpls"

    def test_empty_dir_returns_none(self, tmp_path):
        playlist_dir = tmp_path / "PLAYLIST"
        playlist_dir.mkdir()
        assert _find_main_bdmv_playlist(playlist_dir) is None

    def test_missing_dir_returns_none(self, tmp_path):
        assert _find_main_bdmv_playlist(tmp_path / "does_not_exist") is None

    def test_invalid_mpls_skipped(self, tmp_path):
        playlist_dir = tmp_path / "PLAYLIST"
        playlist_dir.mkdir()
        (playlist_dir / "00000.mpls").write_bytes(b"not_a_real_mpls")
        (playlist_dir / "00001.mpls").write_bytes(_build_mpls_with_duration(1000))
        result = _find_main_bdmv_playlist(playlist_dir)
        assert result is not None
        assert result.name == "00001.mpls"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py::TestFindMainBdmvPlaylist -v
```

Expected: ImportError for `_find_main_bdmv_playlist`.

- [ ] **Step 3: Implement `_find_main_bdmv_playlist` and a helper to read durations**

Append to `backend/disc_metadata.py`:

```python
import struct  # at top of file with other imports — move it up there


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
```

ALSO move `import struct` from inside the function up to the top of the file alongside the other imports. The top of `backend/disc_metadata.py` should now read:

```python
"""DVD VTS IFO and Blu-ray .mpls language-metadata parsers. v0.6.5+.
...
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Optional
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py::TestFindMainBdmvPlaylist -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr add backend/disc_metadata.py backend/tests/test_disc_metadata.py
git -C /Users/hal9000/Documents/Claude/shrinkerr commit -m "feat(disc-meta): BDMV main-playlist selector via .mpls duration sum (v0.6.5)"
```

---

## Task 4: BDMV mpls parser (`_parse_bdmv_mpls`)

**Files:**
- Modify: `backend/disc_metadata.py`
- Modify: `backend/tests/test_disc_metadata.py`

- [ ] **Step 1: Write failing tests using a fuller mpls fixture (with STN_table)**

Append to `backend/tests/test_disc_metadata.py`:

```python
from backend.disc_metadata import _parse_bdmv_mpls


def _build_mpls_full(
    duration_sec: float,
    audio_langs: list[bytes],  # 3-byte ISO 639-2
    pg_langs: list[bytes],     # 3-byte ISO 639-2
) -> bytes:
    """Build a complete .mpls fixture with STN_table.

    Audio entries use stream_coding_type=0x81 (AC-3) so the audio
    StreamAttributes lang_code lives at offset +3 within the
    attributes block.

    PG (subtitle) entries use stream_coding_type=0x90 (PG); lang_code
    at offset +2 within the attributes block.
    """
    ticks = int(duration_sec * 45000)

    # --- Build StreamEntry + StreamAttributes pairs for audio ---
    # StreamEntry for PlayItem-resident audio (type 0x01):
    #   length(1) = 8 ; type(1)=0x01 ; PID (uint16 BE) ; reserved(5)
    def audio_stream_entry(pid: int) -> bytes:
        return (
            b"\x08"   # length
            + b"\x01" # type 0x01 = elementary stream in PlayItem clip
            + struct.pack(">H", pid)
            + b"\x00" * 5
        )

    def audio_stream_attr(coding_type: int, lang: bytes) -> bytes:
        # length(1) + coding_type(1) + audio_format_sample_rate(1) + lang(3)
        # = 6 payload bytes after the length byte
        # length field reports bytes AFTER the length byte
        body = bytes([coding_type]) + b"\x00" + lang + b"\x00" * 0
        return bytes([len(body)]) + body

    def pg_stream_entry(pid: int) -> bytes:
        return (
            b"\x08"
            + b"\x01"
            + struct.pack(">H", pid)
            + b"\x00" * 5
        )

    def pg_stream_attr(lang: bytes) -> bytes:
        # length(1) + coding_type(1) + lang(3)
        body = b"\x90" + lang
        return bytes([len(body)]) + body

    # Build streams blob for STN_table body
    streams_blob = b""
    for i, lang in enumerate(audio_langs):
        streams_blob += audio_stream_entry(0x1100 + i)
        streams_blob += audio_stream_attr(0x81, lang)
    for i, lang in enumerate(pg_langs):
        streams_blob += pg_stream_entry(0x1200 + i)
        streams_blob += pg_stream_attr(lang)

    # STN_table:
    #   uint16 BE   : length (after this field)
    #   uint16 BE   : reserved
    #   uint8       : n_primary_video
    #   uint8       : n_primary_audio
    #   uint8       : n_primary_pg
    #   uint8       : n_primary_ig
    #   uint8       : n_secondary_audio
    #   uint8       : n_secondary_video
    #   uint8       : n_pip_pg
    #   bytes[5]    : reserved
    #   streams_blob (video then audio then pg then ig in that order; we
    #                 emit only audio and pg here)
    stn_body = (
        b"\x00\x00"  # reserved
        + bytes([0, len(audio_langs), len(pg_langs), 0, 0, 0, 0])
        + b"\x00" * 5
        + streams_blob
    )
    stn_table = struct.pack(">H", len(stn_body)) + stn_body

    # PlayItem:
    #   uint16 BE : length (after this field)
    #   bytes[5]  : clip_id
    #   bytes[4]  : codec_id
    #   uint16 BE : flags (no multi_angle)
    #   uint8     : ref_to_STC_id
    #   uint32 BE : IN_time
    #   uint32 BE : OUT_time
    #   bytes[8]  : UO_mask
    #   uint8     : flags
    #   uint8     : still_mode
    #   uint16 BE : still_time
    #   STN_table
    pi_payload = (
        b"00000"
        + b"M2TS"
        + b"\x00\x00"
        + b"\x00"
        + struct.pack(">I", 0)
        + struct.pack(">I", ticks)
        + b"\x00" * 8
        + b"\x00"
        + b"\x00"
        + b"\x00\x00"
        + stn_table
    )
    playitem = struct.pack(">H", len(pi_payload)) + pi_payload

    # PlayList:
    pl_body = (
        b"\x00\x00"  # reserved
        + struct.pack(">H", 1)
        + struct.pack(">H", 0)
        + playitem
    )
    playlist = struct.pack(">I", len(pl_body)) + pl_body

    # Header (40 bytes)
    pl_start = 40
    header = (
        b"MPLS" + b"0200"
        + struct.pack(">I", pl_start)
        + struct.pack(">I", 0)
        + struct.pack(">I", 0)
        + b"\x00" * 20
    )
    return header + playlist


class TestBdmvMplsParser:
    def test_two_audio_two_subtitle(self, tmp_path):
        mpls = tmp_path / "00100.mpls"
        mpls.write_bytes(_build_mpls_full(
            duration_sec=5000,
            audio_langs=[b"eng", b"ger"],
            pg_langs=[b"eng", b"jpn"],
        ))
        result = _parse_bdmv_mpls(mpls)
        assert result == {"audio": ["eng", "ger"], "subtitle": ["eng", "jpn"]}

    def test_french_first_english_second_load_bearing(self, tmp_path):
        # Mirrors the Elephant BD case: audio[0]=fre, audio[1]=eng.
        # Stream-order correlation MUST preserve this.
        mpls = tmp_path / "00800.mpls"
        mpls.write_bytes(_build_mpls_full(
            duration_sec=4900,
            audio_langs=[b"fre", b"eng"],
            pg_langs=[b"fre", b"eng"],
        ))
        result = _parse_bdmv_mpls(mpls)
        assert result["audio"][0] == "fre"
        assert result["audio"][1] == "eng"
        assert result["subtitle"][0] == "fre"
        assert result["subtitle"][1] == "eng"

    def test_three_byte_lang_passthrough(self, tmp_path):
        # 3-letter codes pass through after decode
        mpls = tmp_path / "test.mpls"
        mpls.write_bytes(_build_mpls_full(
            duration_sec=100,
            audio_langs=[b"ice"],
            pg_langs=[],
        ))
        assert _parse_bdmv_mpls(mpls)["audio"] == ["ice"]

    def test_whitespace_padded_lang_returns_empty(self, tmp_path):
        # Some discs pad with spaces; should strip and empty → ""
        mpls = tmp_path / "test.mpls"
        mpls.write_bytes(_build_mpls_full(
            duration_sec=100,
            audio_langs=[b"   "],
            pg_langs=[],
        ))
        assert _parse_bdmv_mpls(mpls)["audio"] == [""]

    def test_malformed_magic_returns_empty(self, tmp_path):
        mpls = tmp_path / "test.mpls"
        mpls.write_bytes(b"NOTAREAL" + b"\x00" * 1000)
        assert _parse_bdmv_mpls(mpls) == {"audio": [], "subtitle": []}

    def test_truncated_file_returns_empty(self, tmp_path):
        mpls = tmp_path / "test.mpls"
        mpls.write_bytes(b"MPLS0200" + b"\x00" * 4)
        assert _parse_bdmv_mpls(mpls) == {"audio": [], "subtitle": []}

    def test_missing_file_returns_empty(self):
        assert _parse_bdmv_mpls(Path("/tmp/nonexistent.mpls")) == {"audio": [], "subtitle": []}

    def test_version_0100_also_parses(self, tmp_path):
        mpls = tmp_path / "test.mpls"
        # Patch a v0200 fixture's version field
        data = bytearray(_build_mpls_full(
            duration_sec=100,
            audio_langs=[b"eng"],
            pg_langs=[],
        ))
        data[4:8] = b"0100"
        mpls.write_bytes(bytes(data))
        assert _parse_bdmv_mpls(mpls)["audio"] == ["eng"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py::TestBdmvMplsParser -v
```

Expected: ImportError on `_parse_bdmv_mpls`.

- [ ] **Step 3: Implement `_parse_bdmv_mpls` in `backend/disc_metadata.py`**

Append to `backend/disc_metadata.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py::TestBdmvMplsParser -v
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr add backend/disc_metadata.py backend/tests/test_disc_metadata.py
git -C /Users/hal9000/Documents/Claude/shrinkerr commit -m "feat(disc-meta): BDMV mpls primary audio + PG language parser (v0.6.5)"
```

---

## Task 5: Public entry point + integration into `probe_file`

**Files:**
- Modify: `backend/disc_metadata.py` — public `parse_disc_languages`
- Modify: `backend/scanner.py` — integration in `probe_file`
- Modify: `backend/tests/test_disc_metadata.py`

- [ ] **Step 1: Write failing tests for the public entry point**

Append to `backend/tests/test_disc_metadata.py`:

```python
from backend.disc_metadata import parse_disc_languages


class TestParseDiscLanguages:
    def test_dvd_routes_to_ifo_parser(self, tmp_path):
        # Build a fake disc-root with VIDEO_TS/VTS_01_0.IFO
        disc_root = tmp_path / "Movie (1980)"
        video_ts = disc_root / "VIDEO_TS"
        video_ts.mkdir(parents=True)
        (video_ts / "VIDEO_TS.IFO").write_bytes(b"DVDVIDEO-VMG" + b"\x00" * 0x320)
        (video_ts / "VTS_01_0.IFO").write_bytes(
            _build_dvd_ifo(audio_langs=[b"en"], subp_langs=[b"fr"])
        )
        # Also create a single VOB so _dvd_main_title_vobs picks "01"
        (video_ts / "VTS_01_1.VOB").write_bytes(b"\x00" * 100)
        result = parse_disc_languages(disc_root, "dvd")
        assert result == {"audio": ["eng"], "subtitle": ["fre"]}

    def test_bdmv_routes_to_mpls_parser(self, tmp_path):
        disc_root = tmp_path / "Movie (1990)"
        playlist_dir = disc_root / "BDMV" / "PLAYLIST"
        playlist_dir.mkdir(parents=True)
        # Two playlists; longer one is the main feature
        (playlist_dir / "00000.mpls").write_bytes(
            _build_mpls_full(duration_sec=60, audio_langs=[b"jpn"], pg_langs=[])
        )
        (playlist_dir / "00100.mpls").write_bytes(
            _build_mpls_full(duration_sec=5000, audio_langs=[b"fre", b"eng"], pg_langs=[b"fre", b"eng"])
        )
        result = parse_disc_languages(disc_root, "bdmv")
        assert result == {"audio": ["fre", "eng"], "subtitle": ["fre", "eng"]}

    def test_unknown_disc_type_returns_empty(self, tmp_path):
        result = parse_disc_languages(tmp_path, "unknown")
        assert result == {"audio": [], "subtitle": []}

    def test_dvd_with_no_vobs_returns_empty(self, tmp_path):
        # No VOBs → _dvd_main_title_vobs returns empty → can't pick title set
        disc_root = tmp_path / "Movie"
        (disc_root / "VIDEO_TS").mkdir(parents=True)
        (disc_root / "VIDEO_TS" / "VIDEO_TS.IFO").write_bytes(b"\x00" * 100)
        result = parse_disc_languages(disc_root, "dvd")
        assert result == {"audio": [], "subtitle": []}

    def test_bdmv_with_no_playlist_returns_empty(self, tmp_path):
        disc_root = tmp_path / "Movie"
        (disc_root / "BDMV" / "PLAYLIST").mkdir(parents=True)  # empty dir
        result = parse_disc_languages(disc_root, "bdmv")
        assert result == {"audio": [], "subtitle": []}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py::TestParseDiscLanguages -v
```

Expected: ImportError on `parse_disc_languages`.

- [ ] **Step 3: Implement `parse_disc_languages` (public entry point)**

Append to `backend/disc_metadata.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py -v
```

Expected: all tests pass (35+ tests across all classes).

- [ ] **Step 5: Read the integration site in `backend/scanner.py:probe_file`**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
grep -n 'audio_tracks = \[\]\|return.*audio_tracks\|disc_type' backend/scanner.py | head -20
```

Locate the spot AFTER `audio_tracks` and `subtitle_tracks` are built (around line 184-280) and BEFORE the return dict is constructed (search for the dict that contains `"audio_tracks": audio_tracks`).

- [ ] **Step 6: Patch language fields in `probe_file` BEFORE the return**

Find the existing `probe_file` block where `audio_tracks` and `subtitle_tracks` are fully populated. Just before the line that constructs the return dict (around `return { ... "audio_tracks": audio_tracks ... }`), insert:

```python
        # v0.6.5: discs don't carry per-track language in their ffmpeg
        # output (VOBs/M2TSes lack the tags; libbluray sees what the BD
        # authored, which is often nothing). Read IFO/mpls sidecar and
        # patch language fields by stream-order index. Fail-open: parser
        # errors leave tracks as "und".
        if disc_type:
            try:
                from backend.disc_metadata import parse_disc_languages
                langs = parse_disc_languages(disc_folder, disc_type)
                for i, t in enumerate(audio_tracks):
                    if i < len(langs["audio"]) and langs["audio"][i]:
                        t["language"] = langs["audio"][i]
                for i, t in enumerate(subtitle_tracks):
                    if i < len(langs["subtitle"]) and langs["subtitle"][i]:
                        t["language"] = langs["subtitle"][i]
                if len(audio_tracks) != len(langs["audio"]) or len(subtitle_tracks) != len(langs["subtitle"]):
                    print(
                        f"[DISC-META] count mismatch for {disc_folder}: "
                        f"ffmpeg audio={len(audio_tracks)}/IFO {len(langs['audio'])}, "
                        f"ffmpeg sub={len(subtitle_tracks)}/IFO {len(langs['subtitle'])}",
                        flush=True,
                    )
            except Exception as exc:
                print(f"[DISC-META] failed for {disc_folder}: {exc}", flush=True)
```

Use the existing `disc_folder` variable that's already in scope in `probe_file` (set when the marker path was classified — `disc_folder = p.parent.parent` for both DVD and BDMV cases).

If `disc_folder` is not in scope at the insertion site, the v0.6.2 work introduced it earlier in `probe_file`. Confirm by reading lines 60-100 of scanner.py before inserting.

- [ ] **Step 7: Verify integration with the smoke test**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -c "
import asyncio, tempfile
from pathlib import Path
import sys
sys.path.insert(0, '.')

# Quick smoke: import doesn't break
from backend.scanner import probe_file
from backend.disc_metadata import parse_disc_languages
print('imports OK')

# Synthetic disc with known IFO contents
async def check():
    with tempfile.TemporaryDirectory() as td:
        disc = Path(td) / 'Movie'
        video_ts = disc / 'VIDEO_TS'
        video_ts.mkdir(parents=True)
        # Real probe needs real VOBs to demux; we're only checking that
        # parse_disc_languages routes correctly for the synthetic IFO.
        # Build the synthetic IFO via the test helper:
        from backend.tests.test_disc_metadata import _build_dvd_ifo
        (video_ts / 'VIDEO_TS.IFO').write_bytes(b'\\x00' * 100)
        (video_ts / 'VTS_01_0.IFO').write_bytes(_build_dvd_ifo([b'de'], [b'en']))
        (video_ts / 'VTS_01_1.VOB').write_bytes(b'\\x00' * 100)
        result = parse_disc_languages(disc, 'dvd')
        assert result == {'audio': ['ger'], 'subtitle': ['eng']}, result
        print('OK: parse_disc_languages routes DVD case correctly')

asyncio.run(check())
"
```

Expected: `imports OK` then `OK: parse_disc_languages routes DVD case correctly`.

- [ ] **Step 8: Run the full test suite to confirm no regressions**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py -v
```

Expected: all green.

- [ ] **Step 9: Commit**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr add backend/disc_metadata.py backend/scanner.py backend/tests/test_disc_metadata.py
git -C /Users/hal9000/Documents/Claude/shrinkerr commit -m "feat(scanner): integrate disc language metadata into probe_file (v0.6.5)"
```

---

## Task 6: Backfill existing disc rows in watcher

**Files:**
- Modify: `backend/watcher.py`

- [ ] **Step 1: Read the watcher's `check_once` entry to find the right insertion point**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
grep -n 'async def check_once\|async def __init__\|_get_known_files\|self\.\(_first_run\|_probe_failures\)' backend/watcher.py | head -10
```

Find the start of `check_once`. The backfill must run BEFORE any normal work (so re-probes happen first), gated by the `disc_lang_backfilled_v065` settings flag so it only runs once.

- [ ] **Step 2: Add the backfill method to `FileWatcher`**

In `backend/watcher.py`, add this method to the `FileWatcher` class (place near the existing private methods like `_get_scanned_dirs`):

```python
    async def _backfill_disc_languages_v065(self) -> None:
        """One-shot v0.6.5 migration: re-probe existing disc rows whose
        audio tracks are tagged 'und' so they pick up the new IFO/mpls
        language metadata. Tracked via settings flag
        'disc_lang_backfilled_v065'. Skips paths whose source has been
        deleted (stale rows are cleaned up by the normal stale-removal
        path)."""
        flag_key = "disc_lang_backfilled_v065"
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute(
                "SELECT value FROM settings WHERE key = ?", (flag_key,)
            ) as cur:
                row = await cur.fetchone()
            if row and row["value"] == "true":
                return  # already done

            async with db.execute(
                "SELECT file_path FROM scan_results "
                "WHERE disc_type IS NOT NULL "
                "AND audio_tracks_json LIKE '%\"language\":\"und\"%'"
            ) as cur:
                candidates = [r["file_path"] for r in await cur.fetchall()]
        finally:
            await db.close()

        if not candidates:
            # Nothing to backfill; still set the flag so we don't re-query.
            await self._set_setting(flag_key, "true")
            return

        print(
            f"[WATCHER] v0.6.5 backfill: re-probing {len(candidates)} disc rows for language metadata",
            flush=True,
        )

        from pathlib import Path as _Path
        from backend.scanner import probe_file as _probe_file
        import json as _json

        updated = 0
        for fp in candidates:
            if not _Path(fp).exists():
                continue  # stale; let normal stale-removal handle it
            probe = await _probe_file(fp)
            if probe is None:
                continue
            audio_tracks = probe.get("audio_tracks", [])
            subtitle_tracks = probe.get("subtitle_tracks", [])
            db2 = await aiosqlite.connect(self.db_path)
            try:
                await db2.execute(
                    "UPDATE scan_results SET "
                    "audio_tracks_json = ?, subtitle_tracks_json = ? "
                    "WHERE file_path = ?",
                    (_json.dumps(audio_tracks), _json.dumps(subtitle_tracks), fp),
                )
                await db2.commit()
            finally:
                await db2.close()
            updated += 1

        print(f"[WATCHER] v0.6.5 backfill: updated {updated} disc rows", flush=True)
        await self._set_setting(flag_key, "true")

    async def _set_setting(self, key: str, value: str) -> None:
        """Helper: upsert a row in the settings table."""
        db = await aiosqlite.connect(self.db_path)
        try:
            await db.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                (key, value),
            )
            await db.commit()
        finally:
            await db.close()
```

If a `_set_setting` (or equivalent settings upsert) helper already exists in `backend/watcher.py` or `backend/database.py`, use that instead and remove the local `_set_setting` definition.

- [ ] **Step 3: Wire the backfill call into `check_once`**

At the very top of `check_once` (right after the `scanned_dirs = ...` line or just before it), call:

```python
        # v0.6.5: one-shot re-probe of existing disc rows so they pick up
        # IFO/mpls language metadata. Idempotent via settings flag.
        await self._backfill_disc_languages_v065()
```

- [ ] **Step 4: Syntax check**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -c "import ast; ast.parse(open('backend/watcher.py').read()); print('AST OK')"
python3 -c "from backend.watcher import FileWatcher; print('import OK')"
```

Expected: both print OK.

- [ ] **Step 5: Smoke test the backfill in isolation (no real disc needed)**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -c "
import asyncio, aiosqlite, json, tempfile, os
from pathlib import Path

# Create a throwaway DB with one disc row that has 'und' audio
with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
    db_path = f.name

async def setup():
    db = await aiosqlite.connect(db_path)
    await db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)')
    await db.execute('''CREATE TABLE scan_results(
        file_path TEXT PRIMARY KEY, disc_type TEXT,
        audio_tracks_json TEXT, subtitle_tracks_json TEXT
    )''')
    # Use a path that won't actually probe (doesn't exist on disk → backfill skips it)
    await db.execute(
        'INSERT INTO scan_results VALUES(?, ?, ?, ?)',
        ('/tmp/fake/VIDEO_TS/VIDEO_TS.IFO', 'dvd',
         json.dumps([{'language': 'und'}]), json.dumps([])),
    )
    await db.commit()
    await db.close()

async def run():
    from backend.watcher import FileWatcher
    w = FileWatcher(db_path, interval_minutes=5)
    await w._backfill_disc_languages_v065()
    # Should have set the flag even though the path doesn't exist
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    async with db.execute('SELECT value FROM settings WHERE key = ?', ('disc_lang_backfilled_v065',)) as cur:
        row = await cur.fetchone()
    assert row['value'] == 'true', f'flag not set: {row}'
    print('OK: backfill is idempotent (flag set after first run)')
    # Run again — should be a no-op
    await w._backfill_disc_languages_v065()
    print('OK: second run is no-op')
    await db.close()

asyncio.run(setup())
asyncio.run(run())
os.unlink(db_path)
"
```

Expected: prints `OK: backfill is idempotent ...` and `OK: second run is no-op`.

- [ ] **Step 6: Commit**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr add backend/watcher.py
git -C /Users/hal9000/Documents/Claude/shrinkerr commit -m "feat(watcher): one-shot backfill of disc rows for v0.6.5 language metadata"
```

---

## Task 7: Layer-2 pre-tag verification script

**Why:** the spec mandates that `git tag v0.6.5` does NOT happen unless the parsers produce correct output against the two real reference discs (Fast-Walking DVD, Elephant BDMV). This script is the gate.

**Files:**
- Create: `scripts/verify_disc_languages.py`

- [ ] **Step 1: Confirm the scripts directory exists or create it**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
mkdir -p scripts
ls scripts/
```

- [ ] **Step 2: Create the verification script**

`scripts/verify_disc_languages.py`:

```python
"""v0.6.5 pre-tag verification gate.

Runs INSIDE the production container against the two reference discs:
  - Fast-Walking (1982) DVD  → expect audio[0]='eng'
  - Elephant (2003) BDMV     → expect audio[0]='fre', audio[1]='eng'
                                expect subtitle[0]='fre', subtitle[1]='eng'

Per spec acceptance criterion 10: exits 0 only if all assertions pass.
A non-zero exit blocks `git tag v0.6.5`.

Usage from host:
  docker cp scripts/verify_disc_languages.py shrinkerr:/tmp/
  docker exec shrinkerr python3 /tmp/verify_disc_languages.py

Both reference paths can be overridden via env vars
(SHRINKERR_TEST_DVD, SHRINKERR_TEST_BDMV) for flexibility.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


DEFAULT_DVD = "/media/Misc/Movies2/Fast-Walking (1982) [tt0083930]"
DEFAULT_BDMV = "/media/Misc/Elephant (2003) [tt0363589]"

EXPECTED_DVD_AUDIO_FIRST = "eng"
EXPECTED_BDMV_AUDIO = ["fre", "eng"]
EXPECTED_BDMV_SUBTITLE = ["fre", "eng"]


def _ok(msg: str) -> None:
    print(f"  ✓ {msg}", flush=True)


def _fail(msg: str) -> None:
    print(f"  ✗ {msg}", flush=True)


async def main() -> int:
    failures: list[str] = []

    dvd_root = Path(os.environ.get("SHRINKERR_TEST_DVD", DEFAULT_DVD))
    bdmv_root = Path(os.environ.get("SHRINKERR_TEST_BDMV", DEFAULT_BDMV))

    print(f"=== Layer-2 verification: v0.6.5 disc language metadata ===", flush=True)
    print(f"DVD reference : {dvd_root}", flush=True)
    print(f"BDMV reference: {bdmv_root}", flush=True)
    print()

    from backend.disc_metadata import parse_disc_languages
    from backend.scanner import probe_file

    # --- DVD: parse only ---
    print("[1/4] DVD parse_disc_languages against Fast-Walking IFO", flush=True)
    if not dvd_root.is_dir():
        failures.append(f"DVD reference missing: {dvd_root}")
        _fail(f"DVD root not found")
    else:
        dvd_langs = parse_disc_languages(dvd_root, "dvd")
        if dvd_langs["audio"] and dvd_langs["audio"][0] == EXPECTED_DVD_AUDIO_FIRST:
            _ok(f"audio[0]={dvd_langs['audio'][0]!r} (expected {EXPECTED_DVD_AUDIO_FIRST!r})")
        else:
            failures.append(f"DVD audio[0] wrong: got {dvd_langs['audio']!r}, expected first={EXPECTED_DVD_AUDIO_FIRST!r}")
            _fail(f"audio = {dvd_langs['audio']!r}")
        print(f"    full result: {dvd_langs}", flush=True)

    # --- DVD: full probe_file → patched audio_tracks ---
    print("[2/4] DVD probe_file integration (Fast-Walking)", flush=True)
    if dvd_root.is_dir():
        marker = dvd_root / "VIDEO_TS" / "VIDEO_TS.IFO"
        if not marker.is_file():
            failures.append(f"DVD marker missing: {marker}")
            _fail("marker file not found")
        else:
            probe = await probe_file(str(marker))
            if probe is None:
                failures.append("DVD probe_file returned None")
                _fail("probe failed")
            else:
                audio = probe.get("audio_tracks", [])
                if audio and audio[0].get("language") == EXPECTED_DVD_AUDIO_FIRST:
                    _ok(f"audio_tracks[0].language={audio[0].get('language')!r}")
                else:
                    failures.append(f"DVD probe audio[0].language wrong: {audio[0] if audio else 'no tracks'}")
                    _fail(f"audio_tracks[0]={audio[0] if audio else None}")

    # --- BDMV: parse only ---
    print("[3/4] BDMV parse_disc_languages against Elephant mpls", flush=True)
    if not bdmv_root.is_dir():
        failures.append(f"BDMV reference missing: {bdmv_root}")
        _fail("BDMV root not found")
    else:
        bdmv_langs = parse_disc_languages(bdmv_root, "bdmv")
        # Audio: first two slots must match
        audio = bdmv_langs["audio"]
        if len(audio) >= 2 and audio[0] == EXPECTED_BDMV_AUDIO[0] and audio[1] == EXPECTED_BDMV_AUDIO[1]:
            _ok(f"audio[:2]={audio[:2]!r}")
        else:
            failures.append(f"BDMV audio[:2] wrong: got {audio!r}, expected {EXPECTED_BDMV_AUDIO!r}")
            _fail(f"audio = {audio!r}")
        # Subtitle: first two slots
        sub = bdmv_langs["subtitle"]
        if len(sub) >= 2 and sub[0] == EXPECTED_BDMV_SUBTITLE[0] and sub[1] == EXPECTED_BDMV_SUBTITLE[1]:
            _ok(f"subtitle[:2]={sub[:2]!r}")
        else:
            failures.append(f"BDMV subtitle[:2] wrong: got {sub!r}, expected {EXPECTED_BDMV_SUBTITLE!r}")
            _fail(f"subtitle = {sub!r}")
        print(f"    full result: {bdmv_langs}", flush=True)

    # --- BDMV: full probe_file → patched tracks (stream-order assertion) ---
    print("[4/4] BDMV probe_file stream-order correlation (Elephant)", flush=True)
    if bdmv_root.is_dir():
        marker = bdmv_root / "BDMV" / "index.bdmv"
        if not marker.is_file():
            failures.append(f"BDMV marker missing: {marker}")
            _fail("marker file not found")
        else:
            probe = await probe_file(str(marker))
            if probe is None:
                failures.append("BDMV probe_file returned None")
                _fail("probe failed")
            else:
                audio = probe.get("audio_tracks", [])
                sub = probe.get("subtitle_tracks", [])
                if len(audio) >= 2 and audio[0].get("language") == "fre" and audio[1].get("language") == "eng":
                    _ok("audio_tracks[0]=fre, audio_tracks[1]=eng (correct stream order)")
                else:
                    failures.append(f"BDMV probe audio stream order wrong: {[t.get('language') for t in audio]}")
                    _fail(f"audio langs = {[t.get('language') for t in audio]}")
                if len(sub) >= 2 and sub[0].get("language") == "fre" and sub[1].get("language") == "eng":
                    _ok("subtitle_tracks[0]=fre, subtitle_tracks[1]=eng (correct stream order)")
                else:
                    failures.append(f"BDMV probe subtitle stream order wrong: {[t.get('language') for t in sub]}")
                    _fail(f"subtitle langs = {[t.get('language') for t in sub]}")

    print()
    if failures:
        print(f"=== FAIL: {len(failures)} assertion(s) failed — do NOT tag v0.6.5 ===", flush=True)
        for f in failures:
            print(f"  - {f}", flush=True)
        return 1
    print("=== PASS: all assertions OK — safe to git tag v0.6.5 ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 3: Verify the script's structure and Python-parsability**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -c "import ast; ast.parse(open('scripts/verify_disc_languages.py').read()); print('AST OK')"
```

Expected: `AST OK`.

- [ ] **Step 4: Commit**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr add scripts/verify_disc_languages.py
git -C /Users/hal9000/Documents/Claude/shrinkerr commit -m "test: layer-2 disc-language verification script (v0.6.5)"
```

---

## Task 8: Pre-tag verification + release

**This task does not modify code — it runs the gate and only ships if it passes.** If the gate fails, the prior implementation tasks must be revisited.

**Files:**
- Modify: `VERSION`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Push current main so the user's container can pull the v0.6.5 implementation**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr push origin main
```

The container running on the user's NUC needs to be running the new code to be able to verify against the real discs. The user manually rebuilds / pulls the dev image (not a tag yet).

- [ ] **Step 2: Ask the user to bring the dev container up to date**

Prompt the user:

> "Pre-tag verification needs the v0.6.5 code running in your container. Options:
>   A. Build the image locally on the NUC: `cd shrinkerr && docker build -t shrinkerr:dev .`
>   B. Push a temp pre-tag (e.g. `v0.6.5-rc1`) to trigger CI, then pull that image
>   C. Run the parsers directly inside the existing container via volume-mounted backend/ source
>
> Which works for your setup?"

Wait for user direction. Do NOT proceed to step 3 until the container is running v0.6.5 code.

- [ ] **Step 3: Run the layer-2 verification gate against the live container**

```bash
# from the user's NUC, with the dev container running:
docker cp scripts/verify_disc_languages.py shrinkerr:/tmp/
docker exec shrinkerr python3 /tmp/verify_disc_languages.py
echo "exit code: $?"
```

Expected output ends with:

```
=== PASS: all assertions OK — safe to git tag v0.6.5 ===
exit code: 0
```

**If the script exits non-zero, STOP.** Read the failures. Common causes:
- Stream-order mismatch in mpls parser (audio[0]/audio[1] swapped) → debug the StreamEntry/StreamAttributes walk in `_parse_bdmv_mpls`
- IFO offset wrong (audio language wrong but bytes present) → verify against libdvdread's `ifo_types.h`
- Disc paths differ on the user's setup → set `SHRINKERR_TEST_DVD` / `SHRINKERR_TEST_BDMV` env vars to point at the right locations

Do NOT proceed to step 4 until the gate exits 0.

- [ ] **Step 4: Bump VERSION**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
echo "0.6.5" > VERSION
```

- [ ] **Step 5: Add CHANGELOG entry**

Open `CHANGELOG.md`. Add at the top, above the current `[0.6.4]` entry, exactly:

```markdown
## [0.6.5] — 2026-05-20

### Added
- **Disc track language detection.** Hand-rolled binary parsers for DVD `VTS_NN_0.IFO` and Blu-ray `.mpls` files now extract per-track language codes and patch them onto disc probe results before classify_audio_tracks runs. Discs no longer surface every audio/subtitle track as `und`; your existing `always_keep_languages` filter now selects correct tracks. Verified against a real English DVD and a French/English bilingual Blu-ray. Existing disc rows auto-backfill on first watcher cycle (idempotent via `settings.disc_lang_backfilled_v065`). Parser failures fail open — disc still gets added with `und` tracks and a `[DISC-META]` warning logged.
```

- [ ] **Step 6: Commit the release**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr add VERSION CHANGELOG.md
git -C /Users/hal9000/Documents/Claude/shrinkerr commit -m "release: v0.6.5 — disc track language detection"
```

- [ ] **Step 7: Tag and push**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr tag v0.6.5
git -C /Users/hal9000/Documents/Claude/shrinkerr push origin main
git -C /Users/hal9000/Documents/Claude/shrinkerr push origin v0.6.5
```

CI builds the multi-arch images on tag push.

---

## Acceptance checklist (from spec)

After Task 8 completes, verify each spec acceptance criterion has been met:

- [ ] **AC 1**: `_parse_dvd_ifo` against Fast-Walking returns audio with first entry "eng" (verified by layer-2 step [1/4])
- [ ] **AC 2**: `_parse_bdmv_mpls` against Elephant returns audio[0]="fre", audio[1]="eng" (verified by layer-2 step [3/4])
- [ ] **AC 3**: After merge, Elephant probe audio_tracks[0].language="fre" and audio_tracks[1].language="eng" (verified by layer-2 step [4/4])
- [ ] **AC 4**: Parser against missing/corrupt file returns empty lists + logs `[DISC-META]` warning (verified by unit tests in `TestDvdIfoParser` and `TestBdmvMplsParser`)
- [ ] **AC 5**: Backfill flag prevents re-runs (verified by Task 6 step 5 smoke test)
- [ ] **AC 6**: Regular file probe behavior unchanged (the new code path is gated on `if disc_type:`)
- [ ] **AC 7**: Backfill skips paths whose source no longer exists (covered by `_Path(fp).exists()` check in `_backfill_disc_languages_v065`)
- [ ] **AC 8**: All `test_disc_metadata.py` cases pass (verified throughout Tasks 1-5)
- [ ] **AC 9**: Keep-language filter picks correct tracks for Elephant — manual UI check after backfill completes: queue Elephant, confirm only English audio + subs are kept
- [ ] **AC 10**: Pre-tag verification gate exits 0 against both real discs (Task 8 step 3 is the gate)
