# DVD / Blu-ray Disc Folder Conversion Design Spec

**Version target:** v0.6.0 (major feature — first non-patch bump since v0.5.0)
**Status:** Design — awaiting implementation plan
**Decisions locked via Q&A:** scope, scanner display, output location, hybrid-disc handling

---

## Background

Shrinkerr currently treats individual video files as the unit of scan + conversion. Many users have ripped DVD collections stored as raw `VIDEO_TS/` folder structures (or Blu-ray `BDMV/` folders), one folder per disc. Today those folders surface in the scanner as a confusing list of individual `.VOB` / `.m2ts` files — none of which are individually convertible, and most of which represent menus / trailers rather than the main feature.

ffmpeg in Shrinkerr's image is already built with `--enable-libdvdread --enable-libdvdnav` and `--enable-libbluray`, which means ffmpeg's `dvd:/` and `bluray:/` input protocols handle disc-folder demuxing natively: they parse IFO / playlist metadata, auto-pick the longest title, and expose its audio / subtitle / video streams the same way ffprobe does for a regular file.

This feature wires the existing scanner / queue / converter pipeline to recognize disc folders and convert their main feature to HEVC, replacing the disc folder with a single MKV file in the same parent directory.

## Scope (locked via Q&A)

- **Main feature only** — auto-pick the longest title (DVD) or playlist (Blu-ray). Extras / menus / trailers ignored. TV-on-DVD episodic discs convert only their longest episode.
- **Auto-detect during scan** — no separate "Add DVD" workflow. Scanner walks media dirs, recognises `VIDEO_TS/` and `BDMV/` markers.
- **Both DVD and BDMV** — same feature ships both. Folders with BOTH (combo packs) treated as Blu-ray.
- **Inline display** — disc items appear alongside files in the Scanner page with a disc icon and badge.
- **Output replaces source in-place** — output MKV lands in the parent folder; the `VIDEO_TS/` or `BDMV/` folder is deleted (or trashed per existing post-conversion source-handling setting).

## Design

### Detection (scanner.py)

During the directory walk, when entering a folder, check for marker subdirectories before recursing:

```python
def _classify_disc(folder: Path) -> str | None:
    """Return 'bdmv', 'dvd', or None depending on disc structure."""
    if (folder / "BDMV" / "index.bdmv").is_file():
        return "bdmv"  # BDMV wins on combo discs (Blu-ray is the main feature)
    if (folder / "VIDEO_TS").is_dir() and any(folder.glob("VIDEO_TS/VIDEO_TS.IFO")):
        return "dvd"
    return None
```

When a disc marker is found:
- Skip recursive descent (the inside is opaque to the rest of the scanner)
- Register the parent folder as a single disc-source item
- Compute `file_size` as the sum of all `.VOB` / `.m2ts` byte sizes
- Skip the loose VOB/M2TS files inside — they're never standalone items

### Storage (database.py)

Add one new column to `scan_results`:

```sql
ALTER TABLE scan_results ADD COLUMN disc_type TEXT DEFAULT NULL;
```

Values: `'dvd'`, `'bdmv'`, or `NULL` (regular file). Schema migration is additive — no data backfill needed; existing rows get NULL.

`file_path` for disc items is the path to a representative file inside the disc structure (not the folder itself):
- DVD: `<folder>/VIDEO_TS/VIDEO_TS.IFO`
- BDMV: `<folder>/BDMV/index.bdmv`

This way every consumer that does `os.path.dirname(file_path)` or `os.path.basename(file_path)` keeps working. Only the explicit "is this a disc?" check (`disc_type IS NOT NULL`) needs new awareness.

### Probe (scanner.probe_file)

When asked to probe a disc-marker file, switch to the protocol-based input:

```python
async def probe_disc(folder: Path, disc_type: str) -> dict | None:
    protocol = "dvd" if disc_type == "dvd" else "bluray"
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
           "-show_streams", "-show_format",
           f"{protocol}:{folder}"]
    # ... runs ffprobe → returns the longest-title's stream info
```

ffprobe with `dvd:/` returns the streams of the main title only. Resulting `video_codec`, `audio_tracks`, `subtitle_tracks`, `duration` are populated normally and feed into Shrinkerr's existing `needs_conversion`, `classify_audio_tracks`, `classify_subtitle_tracks` pipelines without further changes.

`file_size` for the disc is NOT taken from ffprobe (which would report only the main-title bytes) but computed by summing all `*.VOB` + `*.m2ts` on disk — accurate disk-usage view.

### Watcher (watcher.py)

When the watcher discovers new files / folders:
- File: same as today
- Directory containing `VIDEO_TS/VIDEO_TS.IFO` → register as DVD item, skip recursion
- Directory containing `BDMV/index.bdmv` → register as BDMV item, skip recursion
- Loose `.VOB` or `.m2ts` outside a disc-marker context → ignored (they're not standalone media)

The existing stale-cleanup logic handles deleted disc folders the same way it handles deleted files (path goes away → scan_results row removed).

### UI (frontend)

**Scanner page rows:**
- `disc_type === 'dvd'` rows: small DVD icon (could use Lucide's `Disc` or `Disc3`), badge `DVD` next to the codec badge
- `disc_type === 'bdmv'` rows: same shape, `Blu-ray` badge
- Title display: parent folder name (file_path is `…/VIDEO_TS/VIDEO_TS.IFO` → render parent's parent's basename: `Fast-Walking (1982) [tt0083930]`)
- File size displays the summed disk usage (already computed at scan time)
- Estimated savings, queue actions, filters, sorting all work unchanged

**File detail panel:**
- Audio + subtitle tracks render from the probe data (same as files)
- A small note: "Main feature only — extras and menus will be discarded"

**Queue rendering:**
- Job type is `convert` (always — discs always need encoding)
- Same progress display as a regular convert job

### Conversion (converter.py)

Cmd builder special-cases disc-input by selecting the protocol-prefixed input:

```python
if disc_type:
    protocol = "dvd" if disc_type == "dvd" else "bluray"
    disc_folder = Path(input_path).parent.parent  # strip /VIDEO_TS/VIDEO_TS.IFO
    encode_input_path = f"{protocol}:{disc_folder}"
else:
    encode_input_path = input_path
```

Everything else in `_build_ffmpeg_cmd_impl` works as-is — the encoder args, audio handling, subtitle mapping, HW decode toggles, VMAF skip rules, threads cap, etc. all operate on the ffmpeg stream model which is identical between file-input and protocol-input.

**HW decode interaction:** ffmpeg's `dvd:/` and `bluray:/` protocols always demux into the native software decoder regardless of `-hwaccel`. NVDEC/QSV/VAAPI flags become no-ops for disc input. The cmd builder still emits them when toggles are on, but they're ignored by ffmpeg. No special handling needed; document the no-op behaviour.

**VMAF interaction:** VMAF compares decoded source frames to encoded output. With dvd:/ input it works correctly (software decode produces CPU frames). The v0.5.7 "skip VMAF when HW decode is active" rule doesn't fire because we set `hw_decode_active = False` for disc input regardless of the toggle state.

### Output filename construction

For disc input, the output filename is built from probe metadata since there's no source filename to rename. Pattern matches the user's library style (spaces, no dots between tokens):

```
<parent folder name> <resolution> <source-quality> <audio codec> <channels> <encoder>.mkv
```

Examples:
- `Fast-Walking (1982) [tt0083930]/VIDEO_TS/` → `Fast-Walking (1982) [tt0083930] 480p DVDRip AC3 2.0 x265.mkv`
- `The Matrix (1999) [tt0133093]/BDMV/` → `The Matrix (1999) [tt0133093] 1080p Bluray DTS 5.1 x265.mkv`

Components:
- `<parent folder name>`: `os.path.basename(os.path.dirname(os.path.dirname(file_path)))` — strip the trailing `/VIDEO_TS/VIDEO_TS.IFO` segments
- `<resolution>`: derived from probe height — `2160p`, `1080p`, `720p`, `576p` (PAL DVDs), `480p` (NTSC DVDs)
- `<source-quality>`: hardcoded — `DVDRip` for DVD, `Bluray` for BDMV
- `<audio codec>`: primary audio track's codec, scene-style — `AC3`, `DTS`, `EAC3`, `TrueHD`, etc. Reuse `backend/rename.py` codec mapping (already used for the `AudioCodec` filename token).
- `<channels>`: stereo → `2.0`, 5.1 → `5.1`, 7.1 → `7.1`, etc. Reuse `backend/rename.py:_format_channels()` — already exists and handles the edge cases (mono / 6.1 / etc.).
- `<encoder>`: per v0.5.x — `x265` for libx265, `h265` for NVENC/QSV/VAAPI. Reuse `converter._hevc_tag_for_encoder()`.

The encoder-side rename (`rename_source_to_target_codec`) doesn't apply to disc output (no source codec tag to rewrite). The source-quality rename (`rename_source_quality_in_filename`) also doesn't apply — we're building the name fresh.

### Output location

Same parent folder as `VIDEO_TS/` or `BDMV/`. The disc-marker subdirectory is then deleted (or trashed, per existing post-conversion source-handling setting) once the conversion completes successfully.

Concrete example for the user's `/Movies/Fast-Walking (1982) [tt0083930]/`:

Before:
```
Movies/
└── Fast-Walking (1982) [tt0083930]/
    └── VIDEO_TS/
        ├── VIDEO_TS.IFO
        ├── VTS_01_0.IFO
        ├── VTS_01_1.VOB
        └── …
```

After (with post-conversion delete on, default):
```
Movies/
└── Fast-Walking (1982) [tt0083930]/
    └── Fast-Walking (1982) [tt0083930] 480p DVDRip AC3 2.0 x265.mkv
```

After (with post-conversion trash on):
```
Movies/
└── Fast-Walking (1982) [tt0083930]/
    └── Fast-Walking (1982) [tt0083930] 480p DVDRip AC3 2.0 x265.mkv
.trash/
└── … (VIDEO_TS contents moved here)
```

### Post-conversion source handling

The existing "Delete source after conversion" / "Move to trash" / "Keep" setting applies to the disc-marker subdirectory (`VIDEO_TS/` or `BDMV/`) — same code path as for files but operating on a folder. The deletion / move logic needs to handle folders explicitly (use `shutil.rmtree` / `shutil.move` on the folder). The DVD/BDMV subfolder is opaque — Shrinkerr doesn't need to enumerate its contents; just nuke or move it as a unit.

### Filename rename + sibling detection

The scanner's "skip already-converted siblings" loop (scanner.py:920+) needs awareness of disc items:
- For a regular file `Foo.x264.mkv`, the sibling check looks for `Foo.x265.mkv` in the same dir
- For a disc folder `Fast-Walking (1982) [tt0083930]/VIDEO_TS/`, the converted sibling is the constructed-name MKV in the parent folder

**Matching strategy**: equality on the full constructed name won't work because the constructed name depends on probe-time data (resolution / audio codec / channels) that may have shifted slightly between scans. Use a **prefix match on the parent folder name token** instead — if the parent folder contains ANY `.mkv` file whose stem starts with the parent folder name AND contains either `DVDRip` (for DVD) or `Bluray` (for BDMV), treat that as the converted sibling and skip the disc. This is robust to probe drift and handles user-edited filenames.

### Rules engine

Rules with `video_codec` conditions work — DVD discs report `mpeg2video`, BDMV reports `h264`/`vc1`/`hevc` per source. Other rule conditions (`size`, `audio_codec`, `title`, etc.) also work since they read the same probe-derived fields. No rules-engine changes required.

### Settings impact

No new settings. The feature reuses:
- Encoder choice (`default_encoder`)
- All quality knobs (NVENC CQ / libx265 CRF / presets)
- Audio / sub keep-language settings
- Post-conversion source handling
- HW decode toggles (no-op for disc input but harmless)
- Auto-queue (disc items get auto-queued like any other "needs conversion" item)

## Risk surface

| Risk | Mitigation |
|---|---|
| ffmpeg's libdvdnav can hang on damaged DVDs | Standard ffmpeg timeout already covers this |
| Disc folder size estimation (file_size) vs actual encode bytes | Same logic as oversized files — we already have disk-space pre-check (v0.3.x); applies to disc total size |
| Folder paths in scan_results break some downstream code | We DON'T put folder paths in scan_results — file_path always points to a real file (IFO / index.bdmv). disc_type column flags the special handling |
| User has a non-DVD folder that happens to contain a file called `VIDEO_TS.IFO` | Marker check requires BOTH the folder structure AND a valid IFO that ffprobe can parse. False positives are theoretically possible but practically nil |
| Post-conversion delete of folder fails (permissions, in-use VOB) | Same error handling as file delete — surfaces in the failed-job log |
| Encrypted UHD Blu-rays (libbluray can't AACS-decrypt) | ffprobe fails → disc gets marked as unprobable → user notified, same as any unprobable file |
| Multi-disc DVD sets (`Movie Disc1/`, `Movie Disc2/`) | Each is a separate scan item, each converts independently — user can manually merge or use a rule |
| TV-on-DVD episodic discs lose all but the longest episode | Documented user-choice trade-off — multi-title extraction explicitly out of scope |

## Acceptance criteria

1. **Detection**: scanner walks a directory tree containing one VIDEO_TS and one BDMV folder; both appear as scannable items with correct `disc_type`. Internal VOB/M2TS files do NOT appear as separate items.
2. **Probe**: `ffprobe dvd:/path` / `ffprobe bluray:/path` integration produces a valid scan_results row with main-title `video_codec`, `audio_tracks_json`, `duration`, and aggregated `file_size`.
3. **Hybrid handling**: a folder with both `VIDEO_TS/` AND `BDMV/` is classified as BDMV.
4. **Conversion**: queueing a disc item produces a `convert` job; the ffmpeg cmd uses `dvd:/` or `bluray:/` input; encode completes with the longest title only.
5. **Output naming**: result file matches the documented pattern (parent folder name + probe-derived resolution/audio/codec tokens) and lives in the parent folder.
6. **Post-conversion**: per the user's "Delete original after conversion" setting, the `VIDEO_TS/` or `BDMV/` folder is deleted / trashed / kept appropriately.
7. **Sibling detection**: scanning a folder that already contains both a disc subdirectory AND its converted MKV skip-flags the disc.
8. **UI**: Scanner rows for disc items show a disc icon + DVD/Blu-ray badge; file detail panel renders audio/sub tracks from probe; queue progress works.
9. **No regression on files**: regular file scans/encodes/renames continue working identically to v0.5.26.
10. **HW decode no-op**: disc conversion with `nvenc_hw_decode=true` doesn't fail (ffmpeg ignores the flag for protocol-based input); worker log emits a clear note that HW decode is bypassed for disc input (exact wording at the implementer's discretion).
