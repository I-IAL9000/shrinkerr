# DVD / Blu-ray ISO File Support Design Spec

**Version target:** v0.7.0 (first minor bump since v0.6.0)
**Status:** Design — awaiting implementation plan
**Decisions locked via Q&A:** pycdlib for UDF reads, `.iso` only (no `.img` / `.bin+.cue`), MVP includes language metadata

---

## Background

v0.6.x added support for unpacked DVD `VIDEO_TS/` and Blu-ray `BDMV/` folder structures. v0.6.0 verified that ffmpeg's `concat:` and `bluray:` input protocols handle folder-based discs; v0.6.5 added IFO/mpls language-metadata parsers that operate on sidecar files on the host filesystem. Most users distribute / download discs as `.iso` files instead of unpacked folders — a single 25-50 GB BD image or 4-9 GB DVD image rather than a directory tree.

This spec extends the v0.6.x disc-folder pipeline to also accept `.iso` files. Output filename, conversion behavior, post-conversion source handling, and language metadata extraction all need to be ISO-aware while preserving the existing folder-based paths unchanged.

## Verified ground truth (before any design)

Two ffprobe diagnostics run against the user's real ISO files (Elephant BD, The Skin I Live In DVD) inside the production container:

| Test | Result |
|---|---|
| `ffprobe bluray:/path/to.iso` on a real BD ISO | ✅ Streams returned cleanly; libbluray opens the ISO directly. No Dockerfile changes needed for BD ISO encoding. |
| `ffprobe -f dvdvideo -i /path/to.iso` on a real DVD ISO | ✅ MPEG-2 video + AC-3 audio surfaced; libdvdread reads UDF inside the ISO via its image-file code path. |
| `ffprobe -f dvdvideo -i /bd.iso` (wrong-type) | Fails with `Unable to open the VMG` — clean content-mismatch error, NOT the `Couldn't find device name` failure we hit on folders. Means libdvdread's image-file code path works in our build. |
| pycdlib `iso.get_file_from_iso_fp(udf_path='/VIDEO_TS/VIDEO_TS.IFO')` on the DVD ISO | ✅ 12,288 bytes extracted, magic `DVDVIDEO-VMG`. pycdlib reads UDF inside ISO; we can extract sidecar files. |

## Scope (locked via Q&A)

- **`.iso` extension only.** Other disc-image formats (`.img`, `.nrg`, `.bin+.cue`) are out of scope. Adding them later would need their own detection paths and is rarely-encountered for movies anyway.
- **Single new dependency: `pycdlib`.** Pure-Python UDF + ISO 9660 reader, ~250 KB, added to the Dockerfile's pip layer. No system packages, no apt deps.
- **Reuses existing `disc_type` values** (`'dvd'`, `'bdmv'`) — no new database columns, no schema migration. The folder-vs-ISO branch is decided at runtime by checking `Path(file_path).is_file()` and the `.iso` suffix.
- **Language-metadata parity with folder discs.** The same `_parse_dvd_ifo` and `_parse_bdmv_mpls` byte-level logic runs on sidecar bytes extracted from inside the ISO via pycdlib.
- **Pre-tag verification gate is a HARD requirement** (same as v0.6.5): no `git tag v0.7.0` unless a layer-2 script exits 0 against the user's two real ISO test files.

## Design

### Architecture overview

```
[Scanner walk] → discovers /path/to/movie.iso
    ↓
[ISO classifier] (new) — pycdlib peeks inside:
    /BDMV/index.bdmv     → disc_type = 'bdmv'
    /VIDEO_TS/VIDEO_TS.IFO → disc_type = 'dvd'
    else                 → not a video disc, silently ignored
    ↓
[scan_results row inserted] file_path = ISO file path, disc_type set
    ↓
[probe_file dispatcher]
    if path.is_dir():        existing v0.6.x disc-folder path (concat: / bluray:/folder)
    elif path.suffix==.iso:  new ISO path (-f dvdvideo -i /path.iso  OR  bluray:/path.iso)
    else:                    existing regular-file path
    ↓
[Language metadata patch] parse_disc_languages(path, disc_type) dispatches:
    folder  → existing v0.6.5+ logic (_dvd_main_title_vobs / _find_main_bdmv_playlist)
    ISO     → new parse_disc_languages_iso() via pycdlib
    ↓
[ScannedFile constructed, classify_audio_tracks runs] (unchanged)
    ↓
[Convert pipeline]
    encode_input_path = "{file_path}" (DVD ISO) or "bluray:{file_path}" (BD ISO)
    output_path       = parent-folder-name or ISO-stem (rules below) + scene-style tokens
    source-handling   = unlink(.iso) / trash / move-to-backup (file ops, not folder ops)
```

### File / module changes

| File | Purpose |
|---|---|
| `backend/disc_metadata.py` | Refactor: split each `_parse_*(path)` into `_parse_*_bytes(data: bytes)` + path wrapper. Add `parse_disc_languages_iso(iso_path, disc_type)` + helpers `_pick_main_vts_in_iso(iso)` and `_pick_main_mpls_in_iso(iso)`. Top-level `parse_disc_languages` becomes a dispatcher (folder vs ISO). |
| `backend/scanner.py` | Add `.iso` to recognized extensions in the disc-walk pre-pass. Add `_classify_disc_iso(iso_path)` that uses pycdlib to peek for VIDEO_TS / BDMV markers. `probe_file` routes ISO inputs (DVD: `-f dvdvideo`, BDMV: `bluray:`). |
| `backend/watcher.py` | Mirror the same `.iso` recognition + classification in the watcher's disc-aware walk path. |
| `backend/converter.py` | `convert_file`: when source is `.iso`, route `encode_input_path` to the protocol-prefixed form (BD) or pass-through (DVD `-f dvdvideo`). `build_disc_output_filename` extended with the parent-folder-vs-stem rule for ISOs. Post-source-handling distinguishes file (`.iso`) from folder (`VIDEO_TS/` / `BDMV/`). |
| `backend/tests/test_disc_metadata.py` | Add `TestParseDvdIso`, `TestParseBdmvIso`, and dispatcher tests. Use pycdlib to synthesize small in-memory ISOs with known sidecar payloads for hermetic tests. |
| `scripts/verify_disc_languages.py` | Extend with two new assertions against the user's real ISOs. |
| `Dockerfile` (and `Dockerfile.nvenc`) | Add `pycdlib` to the pip install layer. |
| `VERSION` | `0.7.0` |
| `CHANGELOG.md` | One-liner entry per project convention. |

No new database columns. No schema migration. No backfill.

### Detection (`_classify_disc_iso`)

```python
def _classify_disc_iso(iso_path: Path) -> Optional[str]:
    """Peek inside an ISO file and return 'dvd', 'bdmv', or None.

    BDMV wins on combo discs (same priority as folder-based _classify_disc).
    Uses pycdlib to read UDF directory tables only — no payload extraction.
    Fail-open: returns None on any pycdlib error so non-video ISOs are
    silently skipped rather than blocking the scan.
    """
```

Implementation: open the ISO with pycdlib, check existence of `/BDMV/index.bdmv` via UDF facade (then ISO 9660 fallback), then `/VIDEO_TS/VIDEO_TS.IFO`. Close and return.

A non-video ISO (Linux installer, game, software distribution) will lack both markers → return None → the scanner walk skips it. No row inserted, no probe attempted, no UI clutter.

### Probe and encode routing

In `probe_file` / `convert_file`, the existing disc-detection block (`disc_type` classification) is extended:

```python
p = Path(file_path)
disc_type = None
disc_folder = None  # for folder-based discs only

if p.is_dir():
    # Existing v0.6.x folder logic
    disc_type = _classify_disc(p)
    if disc_type:
        disc_folder = p
        if disc_type == 'dvd':
            encode_input_path = _dvd_concat_input(p)
        else:
            encode_input_path = f"bluray:{p}"
elif p.suffix.lower() == '.iso':
    # New v0.7.0 ISO logic
    disc_type = _classify_disc_iso(p)
    if disc_type == 'dvd':
        encode_input_path = str(p)
        ffprobe_extra_args = ['-f', 'dvdvideo']
    elif disc_type == 'bdmv':
        encode_input_path = f"bluray:{p}"
        ffprobe_extra_args = []
```

The ffmpeg cmd builder gets `ffprobe_extra_args` for DVD ISO (the `-f dvdvideo` flag). For BD ISO no extra flag — the `bluray:` protocol prefix is sufficient.

DVD ISO does NOT use the `concat:` workaround. The `dvdvideo` demuxer handles VOB ordering internally and produces correct duration without us assembling a VOB concat list — this is simpler than the folder case.

### Language metadata extraction (`parse_disc_languages_iso`)

```python
def parse_disc_languages_iso(iso_path: Path, disc_type: str) -> dict[str, list[str]]:
    """Extract per-stream language codes from an ISO file.

    DVD: enumerate /VIDEO_TS/VTS_NN_*.VOB inside the ISO via pycdlib,
         group by NN, sum byte sizes (mirroring _dvd_main_title_vobs),
         pick the largest title set. Extract that NN's VTS_NN_0.IFO
         bytes and feed to _parse_dvd_ifo_bytes.

    BDMV: enumerate /BDMV/PLAYLIST/*.mpls (typically 10-30 files,
          ~1 KB each). Extract each, pass to _mpls_total_duration_bytes,
          pick the longest. Pass that .mpls's bytes to _parse_bdmv_mpls_bytes.

    Fail-open: returns {"audio": [], "subtitle": []} on any pycdlib /
    extraction / parse error. Caller treats as "no metadata"; tracks
    stay 'und'.
    """
```

The byte-extraction helpers used internally:

```python
def _extract_iso_file(iso: pycdlib.PyCdlib, path: str) -> bytes:
    """Try UDF facade first, fall back to ISO 9660 (with optional ';1'
    version suffix). Return bytes on success, raise FileNotFoundError if
    neither succeeds."""

def _pick_main_vts_in_iso(iso: pycdlib.PyCdlib) -> Optional[str]:
    """Enumerate VTS_NN_*.VOB entries in /VIDEO_TS/, group by NN,
    sum sizes (excluding _0 menu), return the NN (e.g. '01') with the
    largest total. Returns None if no candidate found."""

def _pick_main_mpls_in_iso(iso: pycdlib.PyCdlib) -> Optional[bytes]:
    """Enumerate /BDMV/PLAYLIST/*.mpls, extract each, parse durations
    via _mpls_total_duration_bytes, return the longest playlist's raw
    bytes. Returns None if no playlists found."""
```

### Parser refactor (small, mechanical)

For each existing parser, split into a `_bytes(data)` core + a path-based wrapper that keeps the v0.6.5 API working:

```python
def _parse_dvd_ifo_bytes(data: bytes) -> dict[str, list[str]]:
    # Existing parse logic moved here, operates on raw bytes
    ...

def _parse_dvd_ifo(ifo_path: Path) -> dict[str, list[str]]:
    # Thin wrapper for existing folder-path callers
    try:
        return _parse_dvd_ifo_bytes(ifo_path.read_bytes())
    except OSError as exc:
        print(f"[DISC-META] could not read {ifo_path}: {exc}", flush=True)
        return {"audio": [], "subtitle": []}
```

Same split for `_parse_bdmv_mpls` and `_mpls_total_duration`. The existing v0.6.5+ disc-metadata unit tests (45+ across the synthetic-fixture parser suites) remain green — they exercise the path API which the wrappers preserve.

### Output filename construction

Extends `build_disc_output_filename` (v0.6.0+):

```
disc_marker_path = the ScannedFile's file_path

if disc_marker_path.endswith('.iso'):
    iso_path = Path(disc_marker_path)
    iso_parent = iso_path.parent
    # Is the ISO sitting loose at a media_dir root?
    is_loose = await _is_media_dir_root(iso_parent)
    base_name = iso_path.stem if is_loose else iso_parent.name
    output_dir = iso_parent
else:
    # Existing v0.6.x folder logic
    disc_root = Path(disc_marker_path).parent.parent
    base_name = disc_root.name
    output_dir = disc_root

# Apply v0.6.8 metadata-ID strip (e.g. [tt0363589]) — unchanged
base_name = strip_id_tags(base_name)

# Append scene-style tokens (res / source-quality / audio / encoder)
# — unchanged from v0.6.0
```

The `_is_media_dir_root` check reads `media_dirs` table. Path equality (after `.rstrip("/")` normalization) determines whether the ISO's parent IS one of the user's configured media directories. If yes, the parent name is the media-dir label and not movie-named — fall back to the ISO stem.

Concrete cases:
- `/media/Misc/Movies2/Elephant (2003) [tt0363589]/rz0u.iso` → `Elephant (2003) 1080p Bluray EAC3 5.1 h265.mkv`
- `/media/Misc/Movies2/The Skin I Live In (2011) [tt1189073]/sublime-skinilivein.iso` → `The Skin I Live In (2011) 480p DVDRip AC3 5.1 h265.mkv`
- `/media/Misc/Movies2/Elephant.iso` (loose at media_dir root) → `Elephant 1080p Bluray EAC3 5.1 h265.mkv`

### Post-conversion source handling

Branch the v0.6.0 Task 8 source-handling block on `Path(input_path).is_file()`:

```python
if disc_type:
    source = Path(input_path)
    if source.is_file():
        # ISO case
        if backup_days > 0:
            shutil.move(str(source), str(backup_dir / source.name))
        elif use_trash:
            send2trash(str(source))      # with shutil.move fallback
        else:
            source.unlink()
    else:
        # Folder case — existing v0.6.0 Task 8 logic, unchanged
        ...
```

Backup folder location: `source.parent / ".shrinkerr_backup"` — same as the folder branch. The disc-root's `.shrinkerr_backup/` collects backup artifacts whether the source was a folder or an ISO.

### Watcher integration

The watcher's existing disc-aware walk (v0.6.1) currently looks for VIDEO_TS / BDMV folders. v0.7.0 adds a second branch: when the walk's per-file loop encounters a `.iso` file, pass it through the new `_classify_disc_iso` to determine `disc_type`, then proceed normally. No change to the marker-deduplication or stale-removal logic — ISOs are first-class file paths in `scan_results`, not marker references inside a disc subdirectory.

### Existing scan_results rows

No backfill needed. `.iso` files are currently filtered out of the scan walk by the video-extensions list — there are no `.iso` rows in any user's database today. Once v0.7.0 ships and `.iso` joins the extensions list, the watcher's next cycle discovers any sitting `.iso` files and registers them fresh.

### Frontend

No frontend changes needed. The existing disc badges (DVD / Blu-ray, from v0.6.0 Task 9) fire on `disc_type` regardless of folder vs ISO. The `file_name` field set by the backend enricher will correctly show the disc-root parent (or ISO stem for loose ISOs), and the existing file-tree rendering treats the row like any other disc.

## Risk surface

| Risk | Mitigation |
|---|---|
| pycdlib doesn't support every UDF revision (BD UDF 2.50/2.60) | Fail-open: classifier returns None → ISO silently skipped. User notices missing item and can report. We've verified pycdlib reads the user's actual BD + DVD ISOs in the layer-2 test. |
| ISO has VIDEO_TS but no valid VOBs (broken rip) | `_pick_main_vts_in_iso` returns None → language list empty → tracks stay `und`. Probe still works via ffmpeg's `-f dvdvideo`. |
| Very large ISO over CIFS — pycdlib opens but is slow | pycdlib reads only the UDF directory tables, not payload data (except small sidecar extractions). I/O proportional to disc structure size (~few KB), not full ISO size. Sub-second even on remote shares. |
| Non-video ISO (Linux installer, game, software) | Classifier finds no VIDEO_TS / BDMV marker → ignored. No row inserted, no probe attempted. |
| Both ISO and unpacked folder for the same movie | Each is an independent scan_results row. Sibling-skip detection from v0.6.0 Task 4 already handles "converted MKV in parent" case. ISO+folder coexistence without a converted MKV produces two convert candidates — user picks. |
| pycdlib's PEP 668 install constraint on bookworm | Pip install layer in Dockerfile already uses `--break-system-packages` or a venv per the project's existing Dockerfile pattern; pycdlib added to the same layer. |
| Path with special characters (quotes, spaces, brackets) | ffmpeg's `bluray:`/`dvdvideo` accept these per our v0.6.5 testing; pycdlib accepts arbitrary path strings. No new escaping logic needed. |
| Multi-angle BD playlist (rare) | Existing v0.6.5 `_parse_bdmv_mpls` already bails to empty result on multi-angle (caller falls through to next-longest). Behavior carries over. |

## Acceptance criteria

1. **DVD ISO classification**: `_classify_disc_iso` against `sublime-skinilivein.iso` returns `'dvd'`.
2. **BD ISO classification**: `_classify_disc_iso` against `rz0u.iso` (Elephant) returns `'bdmv'`.
3. **Non-video ISO classification**: `_classify_disc_iso` against any ISO without VIDEO_TS / BDMV returns `None`.
4. **DVD ISO probe end-to-end**: `probe_file('/path/movie.iso')` for a DVD ISO returns a dict with `video_codec='mpeg2video'`, populated `audio_tracks`, `disc_type='dvd'`.
5. **BD ISO probe end-to-end**: same shape with `video_codec='h264'` (or similar), `disc_type='bdmv'`.
6. **DVD ISO language metadata**: `parse_disc_languages_iso(skin_iso, 'dvd')` returns a non-empty audio list whose first entry is the actual language tag stored in VTS_NN_0.IFO (likely `'spa'` for an Almodóvar original — verification script reports what was found and the user eyeball-confirms).
7. **BD ISO language metadata**: `parse_disc_languages_iso(elephant_iso, 'bdmv')` returns `audio[0] == 'fre'` and `audio[1] == 'eng'` — same expectations as the Elephant FOLDER case verified in v0.6.5.
8. **Output filename — parent-folder rule**: an ISO inside a movie-named folder produces output named for the folder (with metadata IDs stripped per v0.6.8).
9. **Output filename — loose-ISO rule**: an ISO at a media_dir root produces output named for the ISO stem.
10. **Post-conversion source handling**: ISO file gets unlinked / trashed / moved-to-backup per the user's setting, exactly mirroring the folder behavior on the equivalent setting.
11. **No regression for existing folder discs**: every v0.6.x folder-disc test still passes (the parser-bytes refactor is API-preserving). Implementation note: ISO-side unit tests will use mock pycdlib interfaces feeding pre-built sidecar byte streams to verify the dispatcher + extractor wiring, since pycdlib's write-side UDF support is limited and full-ISO synthesis isn't required to test the dispatch path.
12. **No regression for regular files**: non-ISO, non-disc-folder probe behavior is byte-identical.
13. **Pre-tag verification gate exits 0**: `scripts/verify_disc_languages.py` extended with ISO assertions, all green against the user's real ISOs, before any `git tag v0.7.0`.
