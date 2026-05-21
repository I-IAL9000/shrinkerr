# DVD / Blu-ray ISO File Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend Shrinkerr's v0.6.x disc-folder support to also accept `.iso` files containing DVD-Video or BDMV structures. Detection, probe, encode, language metadata, output filename, and post-conversion source handling all become ISO-aware while preserving existing folder behavior unchanged.

**Architecture:** A new `_classify_disc_iso` peeks inside `.iso` files via pycdlib (pure-Python UDF + ISO 9660 reader) to determine `disc_type`. Existing parsers refactor to expose `_bytes(data)` cores fed by either filesystem reads (folder path) or pycdlib extractions (ISO path). Probe and encode route to ffmpeg's `-f dvdvideo -i /path.iso` (DVD ISO) or `bluray:/path.iso` (BD ISO) — both verified working in advance.

**Tech Stack:** pycdlib 1.14.0 (single new pip dep, pure Python, ~250 KB). Existing FastAPI + aiosqlite + pytest backend. No system packages, no schema migration, no DB backfill.

**Spec:** [`.superpowers/specs/2026-05-21-iso-disc-support-design.md`](../specs/2026-05-21-iso-disc-support-design.md) — committed at `546b34d`.

---

## File Structure

- Modify: `requirements.txt` — add pycdlib
- Modify: `backend/disc_metadata.py` — split parsers, add ISO helpers + dispatcher
- Modify: `backend/scanner.py` — `.iso` extension support + classification + probe routing
- Modify: `backend/watcher.py` — mirror walk integration
- Modify: `backend/converter.py` — encode routing, output filename rule, source-handling file branch
- Modify: `backend/tests/test_disc_metadata.py` — new ISO tests
- Modify: `scripts/verify_disc_languages.py` — ISO assertions for layer-2 gate
- Modify: `VERSION`, `CHANGELOG.md`

10 tasks. No new files — all logic lands in existing modules. The Dockerfile pip layer picks up pycdlib from `requirements.txt` automatically — no Dockerfile edits needed.

---

## Task 1: Add pycdlib dependency

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add pycdlib to requirements.txt**

Edit `requirements.txt`. Insert this line after `psutil==6.1.0` (or anywhere in the list — order doesn't matter):

```
pycdlib==1.14.0
```

- [ ] **Step 2: Install locally for development testing**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
pip3 install --break-system-packages pycdlib==1.14.0
python3 -c "import pycdlib; print('pycdlib', pycdlib.PyCdlib.__module__, 'OK')"
```

Expected: `pycdlib pycdlib.pycdlib OK`.

- [ ] **Step 3: Confirm Dockerfile pip layer picks it up automatically**

```bash
grep -n 'requirements.txt' /Users/hal9000/Documents/Claude/shrinkerr/Dockerfile /Users/hal9000/Documents/Claude/shrinkerr/Dockerfile.nvenc
```

Expected: both Dockerfiles `COPY requirements.txt` then `pip3 install -r requirements.txt`. **No Dockerfile changes needed** — the new pycdlib line installs on the next image rebuild.

- [ ] **Step 4: Commit**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr add requirements.txt
git -C /Users/hal9000/Documents/Claude/shrinkerr commit -m "deps: add pycdlib for ISO UDF reads (v0.7.0)"
```

---

## Task 2: Refactor parsers — split `_bytes` core from path wrapper

**Files:**
- Modify: `backend/disc_metadata.py`

**Why:** ISO-side parsing feeds raw bytes (extracted via pycdlib) directly into the parsers without a tempfile round-trip. The existing path-based callers keep working via thin wrappers.

- [ ] **Step 1: Run existing tests to get the baseline pass count**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py -v 2>&1 | tail -5
```

Note the pass count (45+ tests). This is the "must remain green" baseline.

- [ ] **Step 2: Refactor `_parse_dvd_ifo`**

Find the existing `_parse_dvd_ifo(ifo_path: Path) -> dict[str, list[str]]` in `backend/disc_metadata.py`. Replace it with this split:

```python
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
```

- [ ] **Step 3: Refactor `_parse_bdmv_mpls`**

Find the existing `_parse_bdmv_mpls(mpls_path: Path)` in `backend/disc_metadata.py`. Move the parse body into a `_bytes` core. Replace with this split:

```python
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
```

- [ ] **Step 4: Refactor `_mpls_total_duration`**

Find the existing function. Replace with:

```python
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
```

- [ ] **Step 5: Run existing tests — must still pass**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py -v 2>&1 | tail -10
```

Expected: same pass count as Step 1 baseline. The refactor is API-preserving.

- [ ] **Step 6: Add a smoke test for the new `_bytes` functions**

Append to `backend/tests/test_disc_metadata.py`:

```python
from backend.disc_metadata import (
    _parse_dvd_ifo_bytes,
    _parse_bdmv_mpls_bytes,
    _mpls_total_duration_bytes,
)


class TestParserBytesAPIs:
    """v0.7.0+: smoke-test the new _bytes core functions. Most coverage
    is via the existing path-based tests; these just verify the new entry
    points are usable directly."""

    def test_parse_dvd_ifo_bytes_with_valid_fixture(self):
        fixture = _build_dvd_ifo(audio_langs=[b"en"], subp_langs=[])
        assert _parse_dvd_ifo_bytes(fixture) == {"audio": ["eng"], "subtitle": []}

    def test_parse_dvd_ifo_bytes_with_empty_bytes(self):
        assert _parse_dvd_ifo_bytes(b"") == {"audio": [], "subtitle": []}

    def test_parse_bdmv_mpls_bytes_with_valid_fixture(self):
        fixture = _build_mpls_full(
            duration_sec=100, audio_langs=[b"eng"], pg_langs=[],
        )
        assert _parse_bdmv_mpls_bytes(fixture)["audio"] == ["eng"]

    def test_mpls_total_duration_bytes(self):
        fixture = _build_mpls_with_duration(seconds=120.5)
        # Allow tiny floating-point drift from the 45 kHz round-trip
        assert abs(_mpls_total_duration_bytes(fixture) - 120.5) < 0.001
```

- [ ] **Step 7: Run all tests to confirm refactor + new smoke tests pass**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py -v 2>&1 | tail -10
```

Expected: baseline + 4 new tests, all green.

- [ ] **Step 8: Commit**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr add backend/disc_metadata.py backend/tests/test_disc_metadata.py
git -C /Users/hal9000/Documents/Claude/shrinkerr commit -m "refactor(disc-meta): split parsers into _bytes core + path wrapper (v0.7.0)"
```

---

## Task 3: `_classify_disc_iso` — peek inside ISO files

**Files:**
- Modify: `backend/disc_metadata.py`
- Modify: `backend/tests/test_disc_metadata.py`

- [ ] **Step 1: Write failing tests**

Append to `backend/tests/test_disc_metadata.py`:

```python
import pycdlib
import io as _io
from backend.disc_metadata import _classify_disc_iso


def _build_minimal_dvd_iso(tmp_path):
    """Use pycdlib to write a tiny ISO containing /VIDEO_TS/VIDEO_TS.IFO
    (just enough for classification). UDF + ISO 9660 dual-format."""
    iso = pycdlib.PyCdlib()
    iso.new(udf="2.50")
    # Add VIDEO_TS dir + VIDEO_TS.IFO file via UDF
    iso.add_directory(udf_path="/VIDEO_TS")
    iso.add_fp(
        _io.BytesIO(b"DVDVIDEO-VTS" + b"\x00" * 64),
        len(b"DVDVIDEO-VTS") + 64,
        udf_path="/VIDEO_TS/VIDEO_TS.IFO",
    )
    out = tmp_path / "fake_dvd.iso"
    iso.write(str(out))
    iso.close()
    return out


def _build_minimal_bdmv_iso(tmp_path):
    iso = pycdlib.PyCdlib()
    iso.new(udf="2.50")
    iso.add_directory(udf_path="/BDMV")
    iso.add_fp(
        _io.BytesIO(b"INDX0200" + b"\x00" * 32),
        len(b"INDX0200") + 32,
        udf_path="/BDMV/index.bdmv",
    )
    out = tmp_path / "fake_bdmv.iso"
    iso.write(str(out))
    iso.close()
    return out


def _build_empty_iso(tmp_path):
    """Non-video ISO — no VIDEO_TS, no BDMV."""
    iso = pycdlib.PyCdlib()
    iso.new(udf="2.50")
    iso.add_directory(udf_path="/READMES")
    out = tmp_path / "fake_other.iso"
    iso.write(str(out))
    iso.close()
    return out


class TestClassifyDiscIso:
    def test_dvd_iso_classified(self, tmp_path):
        iso = _build_minimal_dvd_iso(tmp_path)
        assert _classify_disc_iso(iso) == "dvd"

    def test_bdmv_iso_classified(self, tmp_path):
        iso = _build_minimal_bdmv_iso(tmp_path)
        assert _classify_disc_iso(iso) == "bdmv"

    def test_non_video_iso_returns_none(self, tmp_path):
        iso = _build_empty_iso(tmp_path)
        assert _classify_disc_iso(iso) is None

    def test_missing_iso_returns_none(self, tmp_path):
        assert _classify_disc_iso(tmp_path / "does_not_exist.iso") is None

    def test_garbage_file_returns_none(self, tmp_path):
        path = tmp_path / "not_an_iso.iso"
        path.write_bytes(b"definitely not an iso file")
        assert _classify_disc_iso(path) is None

    def test_combo_iso_prefers_bdmv(self, tmp_path):
        """If an ISO has both VIDEO_TS and BDMV (rare combo disc),
        BDMV wins — same priority as the folder-based _classify_disc."""
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.50")
        iso.add_directory(udf_path="/VIDEO_TS")
        iso.add_fp(_io.BytesIO(b"x" * 100), 100, udf_path="/VIDEO_TS/VIDEO_TS.IFO")
        iso.add_directory(udf_path="/BDMV")
        iso.add_fp(_io.BytesIO(b"y" * 100), 100, udf_path="/BDMV/index.bdmv")
        out = tmp_path / "combo.iso"
        iso.write(str(out))
        iso.close()
        assert _classify_disc_iso(out) == "bdmv"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py::TestClassifyDiscIso -v
```

Expected: ImportError on `_classify_disc_iso`.

- [ ] **Step 3: Implement `_classify_disc_iso`**

Append to `backend/disc_metadata.py` (near the other classifier helpers):

```python
def _classify_disc_iso(iso_path: Path) -> Optional[str]:
    """Peek inside an ISO file and return 'dvd', 'bdmv', or None.

    BDMV wins on combo discs (same priority as folder-based
    `_classify_disc` in scanner.py). Uses pycdlib to read UDF + ISO 9660
    directory tables — no payload extraction at this stage. Fail-open:
    any pycdlib error returns None so non-video ISOs are silently
    skipped rather than blocking the scan. v0.7.0+.
    """
    try:
        import pycdlib
    except ImportError:
        print("[DISC-META] pycdlib not installed; ISO support disabled", flush=True)
        return None

    if not iso_path.is_file():
        return None

    iso = pycdlib.PyCdlib()
    try:
        iso.open(str(iso_path))
    except Exception as exc:
        print(f"[DISC-META] not a valid ISO: {iso_path}: {exc}", flush=True)
        return None

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


def _iso_has_path(iso, path: str) -> bool:
    """Check existence of a path inside an ISO via UDF facade first,
    then ISO 9660 (with optional ';1' version suffix). Used by the
    classifier and the sidecar extraction helpers."""
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py::TestClassifyDiscIso -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr add backend/disc_metadata.py backend/tests/test_disc_metadata.py
git -C /Users/hal9000/Documents/Claude/shrinkerr commit -m "feat(disc-meta): _classify_disc_iso via pycdlib peek (v0.7.0)"
```

---

## Task 4: ISO sidecar extraction helpers

**Files:**
- Modify: `backend/disc_metadata.py`
- Modify: `backend/tests/test_disc_metadata.py`

- [ ] **Step 1: Write failing tests for the extraction helpers**

Append to `backend/tests/test_disc_metadata.py`:

```python
from backend.disc_metadata import (
    _extract_iso_file,
    _pick_main_vts_in_iso,
    _pick_main_mpls_in_iso,
)


class TestIsoExtractors:
    def test_extract_iso_file_udf_path(self, tmp_path):
        # Build an ISO with a known file
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.50")
        iso.add_directory(udf_path="/X")
        iso.add_fp(_io.BytesIO(b"HELLO_PAYLOAD"), 13, udf_path="/X/file.bin")
        out = tmp_path / "x.iso"
        iso.write(str(out))
        iso.close()

        iso2 = pycdlib.PyCdlib()
        iso2.open(str(out))
        try:
            data = _extract_iso_file(iso2, "/X/file.bin")
            assert data == b"HELLO_PAYLOAD"
        finally:
            iso2.close()

    def test_extract_iso_file_missing_raises(self, tmp_path):
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.50")
        out = tmp_path / "empty.iso"
        iso.write(str(out))
        iso.close()

        iso2 = pycdlib.PyCdlib()
        iso2.open(str(out))
        try:
            with pytest.raises(FileNotFoundError):
                _extract_iso_file(iso2, "/does/not/exist")
        finally:
            iso2.close()

    def test_pick_main_vts_picks_largest(self, tmp_path):
        """Build an ISO with two title sets: VTS_01 (3 large VOBs) and
        VTS_02 (1 small VOB). Picker should return '01'."""
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.50")
        iso.add_directory(udf_path="/VIDEO_TS")
        # VTS_01: 3 VOBs of 1000 bytes each
        for i in range(1, 4):
            iso.add_fp(
                _io.BytesIO(b"\x00" * 1000), 1000,
                udf_path=f"/VIDEO_TS/VTS_01_{i}.VOB",
            )
        # VTS_02: 1 VOB of 100 bytes
        iso.add_fp(
            _io.BytesIO(b"\x00" * 100), 100,
            udf_path="/VIDEO_TS/VTS_02_1.VOB",
        )
        # Menu chunks (should be ignored)
        iso.add_fp(
            _io.BytesIO(b"\x00" * 50), 50,
            udf_path="/VIDEO_TS/VTS_01_0.VOB",
        )
        out = tmp_path / "multi_vts.iso"
        iso.write(str(out))
        iso.close()

        iso2 = pycdlib.PyCdlib()
        iso2.open(str(out))
        try:
            assert _pick_main_vts_in_iso(iso2) == "01"
        finally:
            iso2.close()

    def test_pick_main_vts_no_vobs_returns_none(self, tmp_path):
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.50")
        iso.add_directory(udf_path="/VIDEO_TS")
        # only IFO, no VOBs
        iso.add_fp(_io.BytesIO(b"x" * 100), 100, udf_path="/VIDEO_TS/VIDEO_TS.IFO")
        out = tmp_path / "novobs.iso"
        iso.write(str(out))
        iso.close()

        iso2 = pycdlib.PyCdlib()
        iso2.open(str(out))
        try:
            assert _pick_main_vts_in_iso(iso2) is None
        finally:
            iso2.close()

    def test_pick_main_mpls_picks_longest(self, tmp_path):
        """Build an ISO with three .mpls of different durations. Picker
        should return the bytes of the longest."""
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.50")
        iso.add_directory(udf_path="/BDMV")
        iso.add_directory(udf_path="/BDMV/PLAYLIST")
        # Three playlists; middle one is longest
        for name, duration in [("00000.mpls", 60), ("00100.mpls", 5000), ("00200.mpls", 120)]:
            data = _build_mpls_with_duration(seconds=duration)
            iso.add_fp(_io.BytesIO(data), len(data), udf_path=f"/BDMV/PLAYLIST/{name}")
        out = tmp_path / "multi_mpls.iso"
        iso.write(str(out))
        iso.close()

        iso2 = pycdlib.PyCdlib()
        iso2.open(str(out))
        try:
            result = _pick_main_mpls_in_iso(iso2)
            assert result is not None
            # The picked bytes should be the longest playlist (~5000 sec)
            assert abs(_mpls_total_duration_bytes(result) - 5000) < 1
        finally:
            iso2.close()

    def test_pick_main_mpls_no_playlist_dir_returns_none(self, tmp_path):
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.50")
        out = tmp_path / "empty.iso"
        iso.write(str(out))
        iso.close()

        iso2 = pycdlib.PyCdlib()
        iso2.open(str(out))
        try:
            assert _pick_main_mpls_in_iso(iso2) is None
        finally:
            iso2.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py::TestIsoExtractors -v
```

Expected: ImportError on the new helpers.

- [ ] **Step 3: Implement the extraction helpers**

Append to `backend/disc_metadata.py`:

```python
def _extract_iso_file(iso, path: str) -> bytes:
    """Read a file from inside an open pycdlib ISO. Tries UDF facade
    first, then ISO 9660 (with optional ';1' version suffix).
    Raises FileNotFoundError if the path is absent in both facades."""
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
            children = iso.list_children(**{walker_kw: "/VIDEO_TS"})
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
            children = iso.list_children(**{walker_kw: "/BDMV/PLAYLIST"})
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py::TestIsoExtractors -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr add backend/disc_metadata.py backend/tests/test_disc_metadata.py
git -C /Users/hal9000/Documents/Claude/shrinkerr commit -m "feat(disc-meta): ISO sidecar extraction helpers (v0.7.0)"
```

---

## Task 5: `parse_disc_languages_iso` + dispatcher

**Files:**
- Modify: `backend/disc_metadata.py`
- Modify: `backend/tests/test_disc_metadata.py`

- [ ] **Step 1: Write failing tests**

Append to `backend/tests/test_disc_metadata.py`:

```python
from backend.disc_metadata import parse_disc_languages_iso


class TestParseDiscLanguagesIso:
    def test_dvd_iso_full_pipeline(self, tmp_path):
        """End-to-end: build a DVD-like ISO with a VTS_01_0.IFO containing
        a known audio language, run the full parse_disc_languages_iso
        pipeline, expect the extracted IFO bytes to flow through the
        bytes parser correctly."""
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.50")
        iso.add_directory(udf_path="/VIDEO_TS")
        # Build a synthetic IFO with audio=['en']
        ifo_data = _build_dvd_ifo(audio_langs=[b"en"], subp_langs=[])
        iso.add_fp(_io.BytesIO(ifo_data), len(ifo_data), udf_path="/VIDEO_TS/VTS_01_0.IFO")
        # Add a VOB so _pick_main_vts_in_iso picks "01"
        iso.add_fp(_io.BytesIO(b"\x00" * 1000), 1000, udf_path="/VIDEO_TS/VTS_01_1.VOB")
        out = tmp_path / "test_dvd.iso"
        iso.write(str(out))
        iso.close()

        result = parse_disc_languages_iso(out, "dvd")
        assert result == {"audio": ["eng"], "subtitle": []}

    def test_bdmv_iso_full_pipeline(self, tmp_path):
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.50")
        iso.add_directory(udf_path="/BDMV")
        iso.add_directory(udf_path="/BDMV/PLAYLIST")
        mpls_data = _build_mpls_full(
            duration_sec=5000,
            audio_langs=[b"fre", b"eng"],
            pg_langs=[b"fre", b"eng"],
        )
        iso.add_fp(_io.BytesIO(mpls_data), len(mpls_data), udf_path="/BDMV/PLAYLIST/00100.mpls")
        out = tmp_path / "test_bdmv.iso"
        iso.write(str(out))
        iso.close()

        result = parse_disc_languages_iso(out, "bdmv")
        assert result == {"audio": ["fre", "eng"], "subtitle": ["fre", "eng"]}

    def test_unknown_disc_type_returns_empty(self, tmp_path):
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.50")
        out = tmp_path / "empty.iso"
        iso.write(str(out))
        iso.close()

        assert parse_disc_languages_iso(out, "unknown") == {"audio": [], "subtitle": []}

    def test_missing_iso_returns_empty(self, tmp_path):
        assert parse_disc_languages_iso(tmp_path / "nope.iso", "dvd") == {"audio": [], "subtitle": []}

    def test_dvd_iso_missing_ifo_returns_empty(self, tmp_path):
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.50")
        iso.add_directory(udf_path="/VIDEO_TS")
        # VOB exists but no IFO
        iso.add_fp(_io.BytesIO(b"\x00" * 100), 100, udf_path="/VIDEO_TS/VTS_01_1.VOB")
        out = tmp_path / "no_ifo.iso"
        iso.write(str(out))
        iso.close()

        assert parse_disc_languages_iso(out, "dvd") == {"audio": [], "subtitle": []}
```

- [ ] **Step 2: Add dispatcher tests**

Append:

```python
from backend.disc_metadata import parse_disc_languages


class TestParseDiscLanguagesDispatcher:
    def test_folder_path_routes_to_existing_logic(self, tmp_path):
        """Folder path should hit the v0.6.5 folder dispatcher unchanged."""
        disc_root = tmp_path / "Movie"
        video_ts = disc_root / "VIDEO_TS"
        video_ts.mkdir(parents=True)
        (video_ts / "VTS_01_0.IFO").write_bytes(
            _build_dvd_ifo(audio_langs=[b"en"], subp_langs=[])
        )
        (video_ts / "VTS_01_1.VOB").write_bytes(b"\x00" * 100)
        result = parse_disc_languages(disc_root, "dvd")
        assert result == {"audio": ["eng"], "subtitle": []}

    def test_iso_path_routes_to_iso_logic(self, tmp_path):
        iso = pycdlib.PyCdlib()
        iso.new(udf="2.50")
        iso.add_directory(udf_path="/VIDEO_TS")
        ifo_data = _build_dvd_ifo(audio_langs=[b"de"], subp_langs=[])
        iso.add_fp(_io.BytesIO(ifo_data), len(ifo_data), udf_path="/VIDEO_TS/VTS_01_0.IFO")
        iso.add_fp(_io.BytesIO(b"\x00" * 100), 100, udf_path="/VIDEO_TS/VTS_01_1.VOB")
        out = tmp_path / "test.iso"
        iso.write(str(out))
        iso.close()
        result = parse_disc_languages(out, "dvd")
        assert result == {"audio": ["ger"], "subtitle": []}

    def test_nonexistent_path_returns_empty(self, tmp_path):
        assert parse_disc_languages(tmp_path / "missing", "dvd") == {"audio": [], "subtitle": []}
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py::TestParseDiscLanguagesIso backend/tests/test_disc_metadata.py::TestParseDiscLanguagesDispatcher -v
```

Expected: ImportError on `parse_disc_languages_iso`.

- [ ] **Step 4: Implement `parse_disc_languages_iso` and update the dispatcher**

In `backend/disc_metadata.py`, append:

```python
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
        print(f"[DISC-META] ISO open failed for {iso_path}: {exc}", flush=True)
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
```

Find the existing `parse_disc_languages(disc_root: Path, disc_type: str)` in `backend/disc_metadata.py` (added in v0.6.5 Task 5). Replace it with this dispatcher version:

```python
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
```

The old `parse_disc_languages` body's contents move into `_parse_disc_languages_folder` verbatim (including the lazy `from backend.scanner import _dvd_main_title_vobs` and its comment). The new top-level `parse_disc_languages` is the dispatcher.

- [ ] **Step 5: Run all tests**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -m pytest backend/tests/test_disc_metadata.py -v 2>&1 | tail -15
```

Expected: all green (baseline + new ISO tests + dispatcher tests).

- [ ] **Step 6: Commit**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr add backend/disc_metadata.py backend/tests/test_disc_metadata.py
git -C /Users/hal9000/Documents/Claude/shrinkerr commit -m "feat(disc-meta): parse_disc_languages_iso + folder/ISO dispatcher (v0.7.0)"
```

---

## Task 6: Scanner integration — `.iso` walk + probe routing

**Files:**
- Modify: `backend/scanner.py`

- [ ] **Step 1: Read the existing scanner walk to find insertion points**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
grep -n 'def _classify_disc\|def probe_file\|extensions = \|os.walk\|disc_marker' backend/scanner.py | head -30
```

Locate:
- `_classify_disc(folder)` — v0.6.0 folder classifier
- `probe_file(file_path)` — has disc-routing block from v0.6.0/v0.6.2
- The scanner walk where disc detection happens

- [ ] **Step 2: Extend `probe_file` to handle ISO inputs**

In `backend/scanner.py`, find the existing disc-routing block in `probe_file` (the if/elif that checks for `index.bdmv` and `video_ts.ifo`). Add an ISO branch BEFORE the existing folder-marker branches:

```python
    p = Path(file_path)
    disc_type: Optional[str] = None
    disc_folder: Optional[Path] = None
    ffprobe_input_args: list[str] = []  # extra args before -i for disc ISO

    # v0.7.0: ISO file support. If file_path is a .iso, peek inside via
    # pycdlib to determine disc_type, then route to the appropriate
    # ffmpeg input syntax. DVD ISO uses `-f dvdvideo -i /path.iso`,
    # BD ISO uses `bluray:/path.iso`. No mount, no extraction at probe
    # time.
    if p.is_file() and p.suffix.lower() == ".iso":
        from backend.disc_metadata import _classify_disc_iso
        disc_type = _classify_disc_iso(p)
        if disc_type == "dvd":
            disc_folder = p           # ISO IS the disc — disc_folder semantics differ
            probe_input = str(p)
            ffprobe_input_args = ["-f", "dvdvideo"]
        elif disc_type == "bdmv":
            disc_folder = p
            probe_input = f"bluray:{p}"
        else:
            # Not a video ISO — fall through to regular-file probe (will
            # likely fail; but caller treats failures as 'corrupt' and
            # surfaces the row).
            probe_input = file_path
    elif p.name.lower() == "index.bdmv" and p.parent.name.lower() == "bdmv":
        # Existing v0.6.0/0.6.2 BDMV folder logic — unchanged
        disc_type = "bdmv"
        disc_folder = p.parent.parent
        probe_input = f"bluray:{disc_folder}"
    elif p.name.lower() == "video_ts.ifo" and p.parent.name.lower() == "video_ts":
        # Existing v0.6.2 DVD folder logic — unchanged
        disc_type = "dvd"
        disc_folder = p.parent.parent
        probe_input = _dvd_concat_input(disc_folder)
        if probe_input is None:
            print(f"[PROBE] DVD probe failed: no main-feature VOBs found in {disc_folder}/VIDEO_TS/", flush=True)
            return None
    else:
        probe_input = file_path
```

Then find the ffprobe cmd construction (around `cmd = ["ffprobe", "-v", "quiet", ...]` and the `if disc_type:` block that adds analyzeduration). Modify to splice `ffprobe_input_args` in before `-i`:

```python
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
    ]
    if disc_type:
        cmd.extend(["-analyzeduration", "200M", "-probesize", "200M"])
    if ffprobe_input_args:
        cmd.extend(ffprobe_input_args)
    cmd.extend(["-i", probe_input])
```

- [ ] **Step 3: Add `.iso` to scanner walk extensions**

Find the scanner walk's extension filter. It looks like:

```python
extensions = {ext.lower() for ext in settings.video_extensions}
```

`.iso` won't be in `settings.video_extensions` by default. Augment the per-walk extension set with `.iso` whenever the disc-folder pre-pass is active (we always want to consider ISO files for disc-classification regardless of user-configured video extensions):

Find the disc-walk function (it's the one with the v0.6.1 disc-aware walk). The augmentation:

```python
    extensions = {ext.lower() for ext in settings.video_extensions}
    extensions.add(".iso")  # v0.7.0: include ISO files in the walk for
                            # disc-image classification (separate from
                            # user-configured video extensions)
```

(Same line for the watcher's version of the walk — handled in Task 7.)

- [ ] **Step 4: Hook ISO classification into the per-file scanner loop**

The scanner's per-file loop processes each candidate file. For ISOs we need to classify and (when classified as disc) set the row's `disc_type` so the downstream probe_file already-supports-disc routing fires.

Find the section that constructs `ScannedFile` for a probed file. Currently `disc_type` comes from `probe.get("disc_type")`. probe_file (after Step 2) sets this from `_classify_disc_iso` — but only inside `probe_file`. We need the value in the outer scope after probe returns.

`probe_file` already returns `disc_type` inside the result dict for folder markers (v0.6.0). After Step 2 the same return will carry `disc_type` for ISOs. Verify by reading the end of `probe_file`:

```python
    result = {
        "video_codec": video_codec,
        # ... other fields ...
        "audio_tracks": audio_tracks,
        "subtitle_tracks": subtitle_tracks,
    }
    if disc_type:
        result["disc_type"] = disc_type
        # ... v0.6.0 disc_total_size patch — only for folder discs (BDMV STREAM dir / VOB sum)
        if disc_folder.is_dir():  # folder case
            total = _disc_total_size(disc_folder, disc_type)
            if total > 0:
                result["file_size"] = total
        # else: ISO case — file_size already comes from the ISO's own
        # bytes (ffprobe format.size for ISO disc-image is the ISO file
        # size, which is correct). No patch needed.
    return result
```

Verify the existing v0.6.0 `result["file_size"]` patch only fires for folder discs (where `_disc_total_size` walks VOBs/m2ts). For ISO inputs, ffprobe's `format.size` already reflects the ISO file's actual byte count, which is what we want.

- [ ] **Step 5: Verify imports + syntax**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -c "import ast; ast.parse(open('backend/scanner.py').read()); print('AST OK')"
python3 -c "from backend.scanner import probe_file; print('import OK')"
```

Expected: both print OK.

- [ ] **Step 6: Smoke test against the user's real DVD ISO**

(Plan author: this requires the container running with v0.7.0 code in place; user runs it after pull. Implementer can validate locally by mocking pycdlib in a Python -c snippet.)

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -c "
import asyncio, tempfile
from pathlib import Path
import sys; sys.path.insert(0, '.')

from backend.disc_metadata import _classify_disc_iso, parse_disc_languages, parse_disc_languages_iso
print('imports OK')

# Build a tiny synthetic DVD-like ISO and verify the full pipeline
import pycdlib, io as _io
iso = pycdlib.PyCdlib()
iso.new(udf='2.50')
iso.add_directory(udf_path='/VIDEO_TS')

# Use the same helper that the tests use
exec(open('backend/tests/test_disc_metadata.py').read().split('class TestIso639Mapping')[0])
# That defines _build_dvd_ifo
ifo_data = _build_dvd_ifo(audio_langs=[b'en'], subp_langs=[])
iso.add_fp(_io.BytesIO(ifo_data), len(ifo_data), udf_path='/VIDEO_TS/VTS_01_0.IFO')
iso.add_fp(_io.BytesIO(b'\\x00' * 1000), 1000, udf_path='/VIDEO_TS/VTS_01_1.VOB')

with tempfile.NamedTemporaryFile(suffix='.iso', delete=False) as f:
    iso.write(f.name)
    iso.close()
    p = Path(f.name)
    assert _classify_disc_iso(p) == 'dvd', 'classifier'
    result = parse_disc_languages(p, 'dvd')
    assert result == {'audio': ['eng'], 'subtitle': []}, result
    print('OK: synthetic DVD ISO classified + languages parsed correctly')
"
```

Expected: ends with `OK: synthetic DVD ISO classified + languages parsed correctly`.

- [ ] **Step 7: Commit**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr add backend/scanner.py
git -C /Users/hal9000/Documents/Claude/shrinkerr commit -m "feat(scanner): ISO classification + probe routing for .iso files (v0.7.0)"
```

---

## Task 7: Watcher integration

**Files:**
- Modify: `backend/watcher.py`

- [ ] **Step 1: Read the watcher's disc-aware walk to find insertion points**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
grep -n 'extensions = \|os.walk\|_classify_disc\|disc-aware walk\|v0.6.1' backend/watcher.py | head -15
```

The v0.6.1 disc-aware walk pre-pass is around the `check_once` method. It already detects VIDEO_TS / BDMV folders. We extend the same pre-pass to also recognize `.iso` files.

- [ ] **Step 2: Add `.iso` to the watcher's walk extensions and ISO recognition to the pre-pass**

In `backend/watcher.py:check_once` (and any helper it calls for the walk), find the extension set construction and augment:

```python
        extensions = {ext.lower() for ext in settings.video_extensions}
        extensions.add(".iso")  # v0.7.0: include ISO files for disc-image
                                # classification (separate from user-configured
                                # video extensions)
```

Find the disc-folder pre-pass that maps inner VIDEO_TS/BDMV file events to the marker file. Add a sibling branch that, for each `.iso` candidate, leaves the file_path as-is (we don't redirect ISO paths to inner markers — the ISO IS the disc file). This branch is a no-op pass-through but documented:

```python
                # v0.7.0: .iso files are disc images. Unlike folder discs
                # (which we map to inner marker files), the ISO file itself
                # IS the scan item. Pass through unchanged; probe_file
                # handles ISO classification + routing.
                if p.suffix.lower() == ".iso":
                    pass  # explicit no-op — keep the .iso path as-is
                elif "VIDEO_TS" in p.parts or "BDMV" in p.parts:
                    # ... existing v0.6.1 folder mapping logic ...
```

Adapt to the exact structure of the existing pre-pass — the key insight is that `.iso` files don't need redirection.

- [ ] **Step 3: Syntax check + import smoke test**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -c "import ast; ast.parse(open('backend/watcher.py').read()); print('AST OK')"
python3 -c "from backend.watcher import FileWatcher; print('import OK')"
```

Expected: both print OK.

- [ ] **Step 4: Commit**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr add backend/watcher.py
git -C /Users/hal9000/Documents/Claude/shrinkerr commit -m "feat(watcher): recognize .iso files in disc-aware walk (v0.7.0)"
```

---

## Task 8: Converter integration — encode input, output filename, source handling

**Files:**
- Modify: `backend/converter.py`

- [ ] **Step 1: Extend `convert_file` to route ISO encode input**

Find the existing v0.6.2 disc-routing block in `convert_file` (search for `disc_type` and `encode_input_path`). Add the ISO branch:

```python
    # v0.6.0+ / v0.7.0: disc-folder input or disc ISO input.
    disc_type = probe_data.get("disc_type") if probe_data else None
    encode_input_path = input_path
    ffmpeg_input_args: list[str] = []

    if disc_type:
        p = Path(input_path)
        if p.is_file() and p.suffix.lower() == ".iso":
            # v0.7.0: ISO input — ffmpeg accepts directly via libdvdread/libbluray
            if disc_type == "dvd":
                encode_input_path = str(p)
                ffmpeg_input_args = ["-f", "dvdvideo"]
            else:  # bdmv
                encode_input_path = f"bluray:{p}"
        else:
            # v0.6.2+: folder input — existing logic (concat: / bluray:/folder)
            disc_root = p.parent.parent
            if disc_type == "dvd":
                encode_input_path = _dvd_concat_input(disc_root)
                if encode_input_path is None:
                    raise RuntimeError(
                        f"DVD encode failed: no main-feature VOBs in {disc_root}/VIDEO_TS/"
                    )
            else:  # bdmv
                encode_input_path = f"bluray:{disc_root}"
        print(
            f"[CONVERT] Disc input detected ({disc_type}, "
            f"{'iso' if p.is_file() else 'folder'}); input={encode_input_path}",
            flush=True,
        )
```

Find the ffmpeg cmd construction further down. Add `ffmpeg_input_args` splice before `-i`:

```python
    # ... existing cmd list build ...
    if disc_type:
        cmd.extend(["-analyzeduration", "200M", "-probesize", "200M"])
    if ffmpeg_input_args:
        cmd.extend(ffmpeg_input_args)
    cmd.extend(["-i", encode_input_path])
```

- [ ] **Step 2: Add `_is_media_dir_root` helper**

Append to `backend/converter.py` (near other small helpers):

```python
async def _is_media_dir_root(candidate: Path) -> bool:
    """Return True if `candidate` is one of the user's configured
    media_dirs (path equality after normalizing trailing slashes).
    Used by build_disc_output_filename to decide whether an ISO at
    `candidate / xxx.iso` is 'loose' (use ISO stem for filename) vs
    'in a movie folder' (use parent folder name). v0.7.0+."""
    try:
        import aiosqlite
        from backend.database import DB_PATH
    except ImportError:
        return False
    norm = str(candidate).rstrip("/")
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute("SELECT path FROM media_dirs") as cur:
            for r in await cur.fetchall():
                if str(r["path"]).rstrip("/") == norm:
                    return True
    finally:
        await db.close()
    return False
```

- [ ] **Step 3: Extend `build_disc_output_filename` for ISO inputs**

Find the existing `build_disc_output_filename` in `backend/converter.py` (v0.6.0+, last touched in v0.6.8). The current header reads from `disc_marker_path` and computes `disc_root = p.parent.parent`. For ISO inputs the layout differs — the ISO file IS the input, not a marker inside a folder.

Convert the function to async (so it can await `_is_media_dir_root`) and add ISO branching:

```python
async def build_disc_output_filename(
    disc_marker_path: str,
    disc_type: str,
    probe_data: dict,
    encoder: str | None = None,
) -> str:
    """Construct a scene-style output filename for a converted disc.

    Folder-based disc (v0.6.0+):
        disc_marker_path = .../<parent>/VIDEO_TS/VIDEO_TS.IFO  or  .../<parent>/BDMV/index.bdmv
        output lives in <parent> with base_name = <parent>.name

    ISO file disc (v0.7.0+):
        disc_marker_path = /path/to/movie.iso
        output lives in /path/to/ with base_name = either
          • <parent_folder>.name (when ISO is inside a movie-named folder), or
          • <iso>.stem (when ISO is loose at a media_dir root)

    Metadata-ID tags ([tt0363589], [imdb-...], [tmdb-...], [tvdb-...],
    {tmdb-...}, {tvdb-...}) get stripped from base_name in either path
    (v0.6.8+ behavior).
    """
    import re as _re
    from backend.rename import _format_channels
    p = Path(disc_marker_path)

    if p.is_file() and p.suffix.lower() == ".iso":
        # v0.7.0: ISO file input
        iso_parent = p.parent
        if await _is_media_dir_root(iso_parent):
            base_name = p.stem
        else:
            base_name = iso_parent.name
        output_dir = iso_parent
    else:
        # v0.6.0: folder-based disc — marker is .../<parent>/VIDEO_TS/VIDEO_TS.IFO or .../<parent>/BDMV/index.bdmv
        disc_root = p.parent.parent
        base_name = disc_root.name
        output_dir = disc_root

    # v0.6.8: strip metadata-ID tags from the FILE name
    base_name = _re.sub(
        r"\s*[\[\{](?:tt\d+|(?:imdb|tmdb|tvdb)[-:][a-zA-Z0-9]+)[\]\}]",
        "",
        base_name,
    ).strip()

    # Resolution token from probe video height
    h = int(probe_data.get("video_height") or 0)
    if h >= 2000:
        res = "2160p"
    elif h >= 1000:
        res = "1080p"
    elif h >= 700:
        res = "720p"
    elif h >= 560:
        res = "576p"  # PAL DVD typical
    else:
        res = "480p"  # NTSC DVD typical

    source_quality = "Bluray" if disc_type == "bdmv" else "DVDRip"

    # Primary audio track → scene-style codec + channels tokens.
    audio_tracks = probe_data.get("audio_tracks") or []
    audio_token = ""
    channels_token = ""
    if audio_tracks:
        a = audio_tracks[0]
        codec_raw = a.get("codec") or ""
        if codec_raw:
            audio_token = get_audio_display_name(codec_raw, a.get("profile") or "")
        ch = int(a.get("channels") or 0)
        if ch > 0:
            channels_token = _format_channels(ch)

    # Encoder tag (reuse existing helper)
    codec_tag = _hevc_tag_for_encoder(encoder)  # "x265" or "h265"

    # Assemble scene-style name (space-separated, matching user's library style)
    tokens = [base_name, res, source_quality]
    if audio_token:
        tokens.append(audio_token)
    if channels_token:
        tokens.append(channels_token)
    tokens.append(codec_tag)
    name = " ".join(tokens) + ".mkv"
    return str(output_dir / name)
```

(Note: in the v0.6.8 version the final line was `str(disc_root / name)`. Renamed to `output_dir` here so it works for both folder and ISO branches.)

- [ ] **Step 4: Update callers — `build_disc_output_filename` is now async**

Find every call site:

```bash
grep -n 'build_disc_output_filename' backend/converter.py
```

There should be one caller in `convert_file` (set in v0.6.2). Change `build_disc_output_filename(...)` to `await build_disc_output_filename(...)`. The enclosing `convert_file` is already async.

- [ ] **Step 5: Extend post-conversion source handling for ISO files**

Find the v0.6.0 Task 8 source-handling block in `convert_file` (search for `if disc_type:` followed by `source_to_handle`). Add a file-vs-folder branch:

```python
        if disc_type:
            source = Path(input_path)
            if source.is_file() and source.suffix.lower() == ".iso":
                # v0.7.0: ISO source — single file operation
                if backup_days and backup_days > 0:
                    # Backup the ISO to .shrinkerr_backup/ in its parent dir
                    custom_backup = live_settings.get("backup_folder", "")
                    if custom_backup:
                        backup_dir = Path(custom_backup) / source.parent.name
                        backup_dir.mkdir(parents=True, exist_ok=True)
                    else:
                        backup_dir = source.parent / ".shrinkerr_backup"
                        backup_dir.mkdir(exist_ok=True)
                    backup_path = backup_dir / source.name
                    if backup_path.is_symlink():
                        raise OSError(
                            f"Refusing to move into backup path — destination is a symlink: {backup_path}"
                        )
                    shutil.move(str(source), str(backup_path))
                    result_backup_path = str(backup_path)
                    print(f"[CONVERT] ISO backed up to: {backup_path}", flush=True)
                elif use_trash:
                    try:
                        from send2trash import send2trash
                        send2trash(str(source))
                        print(f"[CONVERT] ISO moved to trash: {source.name}", flush=True)
                    except Exception as trash_exc:
                        print(f"[CONVERT] Trash failed ({trash_exc}), falling back to permanent delete", flush=True)
                        source.unlink()
                else:
                    source.unlink()
                    print(f"[CONVERT] Removed ISO: {source}", flush=True)
            else:
                # v0.6.0+ folder source — existing logic, unchanged
                source_to_handle = source.parent  # the VIDEO_TS/ or BDMV/ folder
                # ... existing folder branch ...
```

Adapt to the existing folder-handling code (don't rewrite that block).

- [ ] **Step 6: Verify syntax + imports**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -c "import ast; ast.parse(open('backend/converter.py').read()); print('AST OK')"
python3 -c "from backend.converter import convert_file, build_disc_output_filename, _is_media_dir_root; print('import OK')"
```

Expected: both print OK.

- [ ] **Step 7: Smoke-test the output filename builder**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -c "
import asyncio, tempfile, aiosqlite, os
from pathlib import Path
from backend.converter import build_disc_output_filename

async def main():
    # Build a throwaway DB with a media_dir for the loose-ISO test
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    db = await aiosqlite.connect(db_path)
    await db.execute('CREATE TABLE media_dirs(path TEXT, enabled INTEGER, auto_scan INTEGER)')
    await db.execute('INSERT INTO media_dirs VALUES(?, 1, 1)', ('/media/Movies',))
    await db.commit()
    await db.close()

    # Monkey-patch DB_PATH for the helper
    import backend.database as _db
    _db.DB_PATH = db_path

    # Touch an ISO file so the helper sees is_file()=True
    iso = Path(tempfile.mkdtemp()) / 'Elephant (2003) [tt0363589]' / 'rz0u.iso'
    iso.parent.mkdir(parents=True)
    iso.write_bytes(b'\\x00')

    probe = {'video_height': 1080, 'audio_tracks': [{'codec': 'eac3', 'channels': 6}]}
    name = await build_disc_output_filename(str(iso), 'bdmv', probe, encoder='nvenc')
    print('ISO in movie folder:', name)
    assert name.endswith('Elephant (2003) 1080p Bluray EAC3 5.1 h265.mkv'), name

    # Loose ISO at media_dir root
    loose = Path('/media/Movies/Elephant.iso')
    loose.touch() if loose.parent.exists() else None  # skip if /media doesn't exist locally
    # If we can't create at /media, just verify the logic via temp:
    tmp_root = Path(tempfile.mkdtemp())
    db2 = await aiosqlite.connect(db_path)
    await db2.execute('DELETE FROM media_dirs')
    await db2.execute('INSERT INTO media_dirs VALUES(?, 1, 1)', (str(tmp_root),))
    await db2.commit()
    await db2.close()
    loose2 = tmp_root / 'Elephant.iso'
    loose2.write_bytes(b'\\x00')
    name2 = await build_disc_output_filename(str(loose2), 'bdmv', probe, encoder='nvenc')
    print('Loose ISO at media_dir root:', name2)
    assert name2.endswith('Elephant 1080p Bluray EAC3 5.1 h265.mkv'), name2
    os.unlink(db_path)
    print('OK')

asyncio.run(main())
"
```

Expected: ends with `OK`. Both filenames match the expected scene-style + ID-tag-stripped patterns.

- [ ] **Step 8: Commit**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr add backend/converter.py
git -C /Users/hal9000/Documents/Claude/shrinkerr commit -m "feat(converter): ISO encode routing + output filename rules + source-handling (v0.7.0)"
```

---

## Task 9: Layer-2 verification script extension

**Files:**
- Modify: `scripts/verify_disc_languages.py`

- [ ] **Step 1: Extend the layer-2 script with ISO assertions**

Open `scripts/verify_disc_languages.py` (created in v0.6.5 Task 7). Add ISO test paths and assertions after the existing folder-based assertions.

At the top, add:

```python
DEFAULT_DVD_ISO = "/media/Misc/Movies2/The Skin I Live In (2011) [tt1189073]/sublime-skinilivein.iso"
DEFAULT_BDMV_ISO = "/media/Misc/Movies2/Elephant (2003) [tt0363589]/rz0u.iso"
```

Inside `async def main()`, after the existing 4 assertion blocks, add two new blocks:

```python
    dvd_iso = Path(os.environ.get("SHRINKERR_TEST_DVD_ISO", DEFAULT_DVD_ISO))
    bdmv_iso = Path(os.environ.get("SHRINKERR_TEST_BDMV_ISO", DEFAULT_BDMV_ISO))

    print()
    print(f"DVD ISO  : {dvd_iso}", flush=True)
    print(f"BDMV ISO : {bdmv_iso}", flush=True)

    # --- DVD ISO: classify + parse ---
    print("[5/8] DVD ISO classification + parse (Skin I Live In)", flush=True)
    from backend.disc_metadata import _classify_disc_iso, parse_disc_languages
    if not dvd_iso.is_file():
        failures.append(f"DVD ISO missing: {dvd_iso}")
        _fail("DVD ISO not found")
    else:
        dt = _classify_disc_iso(dvd_iso)
        if dt == "dvd":
            _ok(f"classifier returned 'dvd'")
        else:
            failures.append(f"DVD ISO classifier returned {dt!r}, expected 'dvd'")
            _fail(f"classifier returned {dt!r}")
        dvd_iso_langs = parse_disc_languages(dvd_iso, "dvd")
        if dvd_iso_langs["audio"] and dvd_iso_langs["audio"][0]:
            _ok(f"audio[0]={dvd_iso_langs['audio'][0]!r} (eyeball-confirm: expected spa or similar for Almodóvar)")
        else:
            failures.append(f"DVD ISO audio empty: {dvd_iso_langs!r}")
            _fail(f"audio = {dvd_iso_langs['audio']!r}")
        print(f"    full result: {dvd_iso_langs}", flush=True)

    # --- DVD ISO: end-to-end probe ---
    print("[6/8] DVD ISO probe_file integration", flush=True)
    if dvd_iso.is_file():
        probe = await probe_file(str(dvd_iso))
        if probe is None:
            failures.append("DVD ISO probe_file returned None")
            _fail("probe failed")
        else:
            audio = probe.get("audio_tracks", [])
            if audio and audio[0].get("language") and audio[0].get("language") != "und":
                _ok(f"audio_tracks[0].language={audio[0].get('language')!r}")
            else:
                failures.append(f"DVD ISO probe audio[0].language wrong: {audio[0] if audio else 'no tracks'}")
                _fail(f"audio[0]={audio[0] if audio else None}")
            if probe.get("disc_type") == "dvd":
                _ok(f"disc_type={probe.get('disc_type')!r}")
            else:
                failures.append(f"DVD ISO probe disc_type wrong: {probe.get('disc_type')!r}")

    # --- BD ISO: classify + parse ---
    print("[7/8] BD ISO classification + parse (Elephant)", flush=True)
    if not bdmv_iso.is_file():
        failures.append(f"BD ISO missing: {bdmv_iso}")
        _fail("BD ISO not found")
    else:
        dt = _classify_disc_iso(bdmv_iso)
        if dt == "bdmv":
            _ok(f"classifier returned 'bdmv'")
        else:
            failures.append(f"BD ISO classifier returned {dt!r}, expected 'bdmv'")
            _fail(f"classifier returned {dt!r}")
        bdmv_iso_langs = parse_disc_languages(bdmv_iso, "bdmv")
        audio = bdmv_iso_langs["audio"]
        if len(audio) >= 2 and audio[0] == "fre" and audio[1] == "eng":
            _ok(f"audio[:2]={audio[:2]!r}")
        else:
            failures.append(f"BD ISO audio[:2] wrong: got {audio!r}, expected ['fre','eng']")
            _fail(f"audio = {audio!r}")
        sub = bdmv_iso_langs["subtitle"]
        if len(sub) >= 2 and sub[0] == "fre" and sub[1] == "eng":
            _ok(f"subtitle[:2]={sub[:2]!r}")
        else:
            failures.append(f"BD ISO subtitle[:2] wrong: got {sub!r}, expected ['fre','eng']")

    # --- BD ISO: end-to-end probe + stream-order ---
    print("[8/8] BD ISO probe_file stream-order (Elephant)", flush=True)
    if bdmv_iso.is_file():
        probe = await probe_file(str(bdmv_iso))
        if probe is None:
            failures.append("BD ISO probe_file returned None")
            _fail("probe failed")
        else:
            audio = probe.get("audio_tracks", [])
            sub = probe.get("subtitle_tracks", [])
            if len(audio) >= 2 and audio[0].get("language") == "fre" and audio[1].get("language") == "eng":
                _ok("audio_tracks[0]=fre, audio_tracks[1]=eng (correct stream order)")
            else:
                failures.append(f"BD ISO audio stream order wrong: {[t.get('language') for t in audio]}")
            if len(sub) >= 2 and sub[0].get("language") == "fre" and sub[1].get("language") == "eng":
                _ok("subtitle_tracks[0]=fre, subtitle_tracks[1]=eng (correct stream order)")
            else:
                failures.append(f"BD ISO subtitle stream order wrong: {[t.get('language') for t in sub]}")
```

Update the existing header bumper that says `=== Layer-2 verification: v0.6.5 disc language metadata ===` to read `v0.7.0`. Update the print header bumper accordingly.

- [ ] **Step 2: AST check**

```bash
cd /Users/hal9000/Documents/Claude/shrinkerr
python3 -c "import ast; ast.parse(open('scripts/verify_disc_languages.py').read()); print('AST OK')"
```

Expected: `AST OK`.

- [ ] **Step 3: Commit**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr add scripts/verify_disc_languages.py
git -C /Users/hal9000/Documents/Claude/shrinkerr commit -m "test: extend layer-2 verify script for ISO support (v0.7.0)"
```

---

## Task 10: Pre-tag verification + release

**HARD GATE** per the spec: `git tag v0.7.0` does NOT happen unless the verification script exits 0 against the user's real ISO files.

**Files:**
- Modify: `VERSION`, `CHANGELOG.md`

- [ ] **Step 1: Push main so the user can pull**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr push origin main
```

- [ ] **Step 2: Ask the user to bring the dev container up to date with v0.7.0 code**

Prompt:

> "Pre-tag verification needs the v0.7.0 code running with pycdlib installed inside your container. Build the dev image locally:
> ```
> cd /path/to/shrinkerr
> git pull origin main
> docker build -t shrinkerr:dev .
> docker stop shrinkerr && docker rm shrinkerr
> docker run -d --name shrinkerr ... shrinkerr:dev   # your normal run invocation
> ```
> Then confirm pycdlib is installed:
> ```
> docker exec shrinkerr python3 -c 'import pycdlib; print(pycdlib.PyCdlib.__module__)'
> ```
> Expected: `pycdlib.pycdlib`. Reply when ready."

Wait for user confirmation. Do NOT proceed to Step 3 until the dev image is running.

- [ ] **Step 3: Run the layer-2 verification gate**

```bash
docker cp /path/to/shrinkerr/scripts/verify_disc_languages.py shrinkerr:/tmp/
docker exec shrinkerr python3 /tmp/verify_disc_languages.py
echo "exit code: $?"
```

Expected output ends with:

```
=== PASS: all assertions OK — safe to git tag v0.7.0 ===
exit code: 0
```

**If the script exits non-zero, STOP.** Read the failures. Common causes:
- pycdlib UDF read issue on the specific ISO → debug `_classify_disc_iso` / `_pick_main_*_in_iso`
- DVD ISO IFO has count=0 quirk (covered by v0.6.6 fallback in `_extract_dvd_langs`)
- Stream-order swap on BD ISO → debug `_parse_bdmv_mpls_bytes` (should match folder-based v0.6.5 behavior since the byte parser is shared)
- Disc paths differ on user's setup → set `SHRINKERR_TEST_DVD_ISO` / `SHRINKERR_TEST_BDMV_ISO` env vars

Do NOT proceed to Step 4 until the gate exits 0.

- [ ] **Step 4: Bump VERSION**

```bash
echo "0.7.0" > /Users/hal9000/Documents/Claude/shrinkerr/VERSION
```

- [ ] **Step 5: Add CHANGELOG entry**

Open `CHANGELOG.md`. Prepend above the current top entry exactly:

```markdown
## [0.7.0] — 2026-05-21

### Added
- **DVD and Blu-ray ISO file support.** `.iso` files containing VIDEO_TS or BDMV structures are now first-class scan items alongside the v0.6.x unpacked folder support. ffmpeg reads the ISO directly via `-f dvdvideo -i /path.iso` (DVD) or `bluray:/path.iso` (BD) — no mount, no extraction, no Dockerfile changes. Language metadata extracted from sidecar IFO/mpls files inside the ISO via pycdlib (new pip dep, pure Python). Output MKV lands in the ISO's parent folder (or alongside the ISO when it's loose at a media_dir root), with the same scene-style naming + metadata-ID strip applied as folder discs. Post-conversion source-handling unlinks/trashes/backs-up the ISO file per the existing setting. Non-video ISOs (Linux installers, games) are silently ignored.
```

- [ ] **Step 6: Commit + tag + push**

```bash
git -C /Users/hal9000/Documents/Claude/shrinkerr add VERSION CHANGELOG.md
git -C /Users/hal9000/Documents/Claude/shrinkerr commit -m "release: v0.7.0 — DVD/Blu-ray ISO file support"
git -C /Users/hal9000/Documents/Claude/shrinkerr tag v0.7.0
git -C /Users/hal9000/Documents/Claude/shrinkerr push origin main
git -C /Users/hal9000/Documents/Claude/shrinkerr push origin v0.7.0
```

CI builds the multi-arch images on tag push.

---

## Acceptance checklist (from spec)

After Task 10 completes, verify each spec acceptance criterion:

- [ ] **AC 1**: `_classify_disc_iso` returns `'dvd'` on Skin I Live In ISO (layer-2 step [5/8])
- [ ] **AC 2**: `_classify_disc_iso` returns `'bdmv'` on Elephant ISO (layer-2 step [7/8])
- [ ] **AC 3**: `_classify_disc_iso` returns `None` for non-video ISOs (covered by `TestClassifyDiscIso.test_non_video_iso_returns_none`)
- [ ] **AC 4**: DVD ISO `probe_file` returns mpeg2video + audio_tracks + disc_type='dvd' (layer-2 step [6/8])
- [ ] **AC 5**: BD ISO `probe_file` returns h264 + audio_tracks + disc_type='bdmv' (layer-2 step [8/8])
- [ ] **AC 6**: `parse_disc_languages_iso(skin_iso, 'dvd')` returns non-empty audio with first entry not `'und'` (layer-2 step [5/8])
- [ ] **AC 7**: `parse_disc_languages_iso(elephant_iso, 'bdmv')` returns `audio = ['fre', 'eng']`, `subtitle = ['fre', 'eng']` (layer-2 step [7/8])
- [ ] **AC 8**: Output filename — parent-folder rule (covered by `build_disc_output_filename` smoke test in Task 8 Step 7)
- [ ] **AC 9**: Output filename — loose-ISO rule (same smoke test)
- [ ] **AC 10**: Post-conversion source-handling — manual queue+convert of Elephant.iso after layer-2 passes (out-of-band end-to-end check)
- [ ] **AC 11**: No regression for existing folder discs — all 45+ existing disc-metadata tests still green (verified throughout Tasks 2-5)
- [ ] **AC 12**: No regression for regular files — Task 6 Step 2's branch structure preserves the non-disc fallthrough exactly
- [ ] **AC 13**: Pre-tag verification gate exits 0 (Task 10 Step 3)
