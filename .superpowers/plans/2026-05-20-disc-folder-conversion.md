# DVD / Blu-ray Disc Folder Conversion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add support for transcoding raw DVD `VIDEO_TS/` and Blu-ray `BDMV/` folder structures via ffmpeg's `dvd:/` and `bluray:/` protocols. The scanner detects disc folders, the converter routes through the existing pipeline using the protocol-prefixed input, and the disc subdirectory is replaced with a single MKV in its parent folder.

**Architecture:** New `disc_type` column on `scan_results` flags disc items; `file_path` stays a real file path (the IFO / `index.bdmv`) so existing path-handling code keeps working. Probe, classification, rules engine, queue, estimate all reuse the regular code paths because ffmpeg's `dvd:/`/`bluray:/` protocols expose the longest title as a normal stream-list. Only the cmd builder + output filename construction + post-conversion source-handling have disc-specific branches.

**Tech Stack:** FastAPI + aiosqlite SQLite backend, React/TypeScript frontend. ffmpeg n7.x with `--enable-libdvdread --enable-libdvdnav --enable-libbluray` (already in both Docker images).

**Spec:** [`.superpowers/specs/2026-05-20-disc-folder-conversion-design.md`](../specs/2026-05-20-disc-folder-conversion-design.md)

---

## File Structure

Files this plan modifies. Each task focuses on one file or one tight cluster.

- `backend/database.py` — schema migration: `disc_type TEXT` column on `scan_results`
- `backend/scanner.py` — `_classify_disc()` helper, `probe_file()` extension for protocol input, scanner walk recognizes disc folders + skips internal VOB/M2TS, sibling detection prefix-match
- `backend/watcher.py` — same disc detection in the watcher's new-file path
- `backend/converter.py` — `build_disc_output_filename()` helper, `convert_file()` routes disc input through `dvd:/`/`bluray:/`, `_build_ffmpeg_cmd_impl()` accepts the protocol-prefixed `encode_input_path` unchanged, post-conversion source-handling extended to `shutil.rmtree`/`move` on the disc subdir
- `frontend/src/pages/ScannerPage.tsx` (and related row-rendering components) — disc icon + badge per `disc_type`

No new files. Each task touches a single primary file with maybe a small import elsewhere.

---

## Task 1: Schema migration — add `disc_type` column

**Files:**
- Modify: `backend/database.py`

- [ ] **Step 1: Add the column-additive migration**

In `backend/database.py`, find the existing `_ALTER_PAIRS` (or equivalent) list of additive migrations. Add:

```python
("scan_results", "disc_type", "TEXT DEFAULT NULL"),
```

Right after the existing scan_results migrations (e.g. after `health_status` or whatever was added most recently). The migration framework re-runs at startup and is idempotent on `ALTER TABLE … ADD COLUMN`.

If `_ALTER_PAIRS` doesn't exist by that exact name, find the function that runs `ALTER TABLE scan_results ADD COLUMN` for the health-check columns (around `backend/database.py:364`) and add a matching tuple/entry there.

- [ ] **Step 2: Sanity-check the migration runs**

```bash
python3 -c "
import asyncio
from backend.database import init_db
asyncio.run(init_db())
"
# Should print no errors. Verify column exists:
sqlite3 ./data/shrinkerr.db "PRAGMA table_info(scan_results)" | grep disc_type
# Expected output: <colnum>|disc_type|TEXT|0||0
```

- [ ] **Step 3: Commit**

```bash
git add backend/database.py
git commit -m "feat(db): add disc_type column to scan_results (v0.6.0)"
```

---

## Task 2: Disc classification helper + probe extension

**Files:**
- Modify: `backend/scanner.py`

- [ ] **Step 1: Add `_classify_disc()`**

In `backend/scanner.py`, near the top of the module (after the constants block, before `probe_file`), add:

```python
def _classify_disc(folder: Path) -> str | None:
    """Return 'bdmv', 'dvd', or None for a candidate folder.
    BDMV wins on combo discs (Blu-ray is the main feature)."""
    bdmv_index = folder / "BDMV" / "index.bdmv"
    if bdmv_index.is_file():
        return "bdmv"
    dvd_ifo = folder / "VIDEO_TS" / "VIDEO_TS.IFO"
    if dvd_ifo.is_file():
        return "dvd"
    return None
```

- [ ] **Step 2: Add `_disc_marker_path()` helper**

Right after `_classify_disc`, add:

```python
def _disc_marker_path(folder: Path, disc_type: str) -> Path:
    """Return the real-file path Shrinkerr stores as scan_results.file_path
    for a disc item. Always inside the disc subdirectory; lets every
    existing `os.path.dirname(file_path)` consumer keep working."""
    if disc_type == "bdmv":
        return folder / "BDMV" / "index.bdmv"
    return folder / "VIDEO_TS" / "VIDEO_TS.IFO"


def _disc_total_size(folder: Path, disc_type: str) -> int:
    """Sum bytes of all media-payload files in the disc structure.
    For DVDs that's *.VOB; for BDMV that's BDMV/STREAM/*.m2ts.
    Returns 0 on any error (caller falls back to whatever size estimate
    they have)."""
    try:
        if disc_type == "bdmv":
            stream_dir = folder / "BDMV" / "STREAM"
            return sum(f.stat().st_size for f in stream_dir.glob("*.m2ts") if f.is_file())
        return sum(f.stat().st_size for f in (folder / "VIDEO_TS").glob("*.VOB") if f.is_file())
    except OSError:
        return 0
```

- [ ] **Step 3: Extend `probe_file()` to handle disc-marker paths**

In `probe_file()` (currently around line 11–151), add at the top of the function — before the ffprobe subprocess is built:

```python
async def probe_file(file_path: str) -> Optional[dict]:
    """Run ffprobe on a file and return parsed metadata dict, or None on failure.

    v0.6.0: when `file_path` points at a disc-marker file (VIDEO_TS.IFO /
    BDMV/index.bdmv), probe the disc-folder via ffmpeg's dvd:/ or bluray:/
    protocol instead. ffmpeg auto-picks the longest title; the resulting
    streams + duration are the main feature only. `file_size` is patched
    to the disc-total via _disc_total_size() since ffprobe's format size
    only covers the main title.
    """
    p = Path(file_path)
    disc_type = None
    if p.name == "index.bdmv" and p.parent.name == "BDMV":
        disc_type = "bdmv"
        disc_folder = p.parent.parent
        probe_input = f"bluray:{disc_folder}"
    elif p.name == "VIDEO_TS.IFO" and p.parent.name == "VIDEO_TS":
        disc_type = "dvd"
        disc_folder = p.parent.parent
        probe_input = f"dvd:{disc_folder}"
    else:
        probe_input = file_path
        disc_folder = None
    # ... rest of probe runs against probe_input (use this variable in
    # the existing cmd) ...
```

Then in the `cmd = [...]` block, swap `file_path` for `probe_input`. At the end of the function, if `disc_type` is set:

```python
    if disc_type:
        result["disc_type"] = disc_type
        # ffprobe's format.size for dvd:/ only covers the main title;
        # use disk-wide size for accurate UI display.
        total = _disc_total_size(disc_folder, disc_type)
        if total > 0:
            result["file_size"] = total
    return result
```

- [ ] **Step 4: Self-test (no actual disc needed — sanity-check the helpers)**

```bash
python3 -c "
from pathlib import Path
from backend.scanner import _classify_disc, _disc_marker_path, _disc_total_size
import tempfile, os

with tempfile.TemporaryDirectory() as td:
    base = Path(td)
    # No disc → None
    assert _classify_disc(base) is None

    # DVD
    (base / 'VIDEO_TS').mkdir()
    (base / 'VIDEO_TS' / 'VIDEO_TS.IFO').write_bytes(b'fake-ifo')
    (base / 'VIDEO_TS' / 'VTS_01_1.VOB').write_bytes(b'x' * 100)
    (base / 'VIDEO_TS' / 'VTS_01_2.VOB').write_bytes(b'y' * 200)
    assert _classify_disc(base) == 'dvd', f'expected dvd, got {_classify_disc(base)!r}'
    assert _disc_marker_path(base, 'dvd') == base / 'VIDEO_TS' / 'VIDEO_TS.IFO'
    assert _disc_total_size(base, 'dvd') == 300

    # Combo (BDMV wins)
    (base / 'BDMV').mkdir()
    (base / 'BDMV' / 'index.bdmv').write_bytes(b'fake-index')
    (base / 'BDMV' / 'STREAM').mkdir()
    (base / 'BDMV' / 'STREAM' / '00000.m2ts').write_bytes(b'z' * 1000)
    assert _classify_disc(base) == 'bdmv'
    assert _disc_marker_path(base, 'bdmv') == base / 'BDMV' / 'index.bdmv'
    assert _disc_total_size(base, 'bdmv') == 1000

print('OK')
"
```

Expected output: `OK`

- [ ] **Step 5: Commit**

```bash
git add backend/scanner.py
git commit -m "feat(scanner): disc classification helpers + probe_file disc-protocol routing (v0.6.0)"
```

---

## Task 3: Scanner walk integration

**Files:**
- Modify: `backend/scanner.py`

- [ ] **Step 1: Update the walk to recognise disc folders**

Find the `os.walk` block at line ~914. Replace with a walk that prunes `dirs` when a disc marker is found AND adds the disc-marker file to `all_files`:

```python
    # Collect all candidate files first
    all_files = []
    for root, dirs, files in os.walk(dir_path):
        root_path = Path(root)

        # v0.6.0: disc-folder detection. When a directory contains a
        # VIDEO_TS/VIDEO_TS.IFO or BDMV/index.bdmv marker, register the
        # marker file as a single scan item and skip descent into the
        # disc subdirectory — its VOBs / M2TS are opaque to Shrinkerr.
        disc_type = _classify_disc(root_path)
        if disc_type:
            marker = _disc_marker_path(root_path, disc_type)
            all_files.append(marker)
            # Don't recurse INTO VIDEO_TS/BDMV — they're internal.
            dirs[:] = [d for d in dirs if d not in ("VIDEO_TS", "BDMV")]
            # Skip the files-in-this-dir loop below too — there shouldn't
            # be any video files alongside VIDEO_TS/BDMV, but if there
            # are, they're not part of the disc and the next walk pass
            # will pick them up via the regular handling.
            continue

        for name in files:
            if name.startswith("."):
                continue
            if Path(name).suffix.lower() in extensions:
                all_files.append(root_path / name)
```

- [ ] **Step 2: Pass disc_type through to scan_results write**

Find where scan_results rows get INSERT'ed inside `scan_directory()`. Currently the call is roughly `_upsert_scan_result(...)` or a raw INSERT. Add `disc_type` to the column list. The value comes from `probe_data.get("disc_type")` (set by Task 2's probe extension).

Search for `INSERT INTO scan_results` in scanner.py and ensure `disc_type` is included with the value `probe.get("disc_type")` from the probe result.

If the INSERT is done via a list of columns + a parameterized statement (likely), append `"disc_type"` to the column list and `probe_data.get("disc_type")` to the values tuple in the same position.

- [ ] **Step 3: Manual integration test (no real disc needed — just verify the walk skips marker dirs cleanly)**

```bash
mkdir -p /tmp/sktest/Movie/VIDEO_TS
touch /tmp/sktest/Movie/VIDEO_TS/VIDEO_TS.IFO
touch /tmp/sktest/Movie/VIDEO_TS/VTS_01_1.VOB

python3 -c "
import os
from pathlib import Path
from backend.scanner import _classify_disc, _disc_marker_path

# Simulate the walk logic
all_files = []
for root, dirs, files in os.walk('/tmp/sktest'):
    rp = Path(root)
    dt = _classify_disc(rp)
    if dt:
        all_files.append(_disc_marker_path(rp, dt))
        dirs[:] = [d for d in dirs if d not in ('VIDEO_TS', 'BDMV')]
        continue
    for n in files:
        all_files.append(rp / n)

print('Files:', [str(f) for f in all_files])
# Expected: ['/tmp/sktest/Movie/VIDEO_TS/VIDEO_TS.IFO'] only
# NOT '/tmp/sktest/Movie/VIDEO_TS/VTS_01_1.VOB'
assert any('VIDEO_TS.IFO' in str(f) for f in all_files)
assert not any('.VOB' in str(f) for f in all_files)
print('OK')
"

rm -rf /tmp/sktest
```

Expected output: ends with `OK`.

- [ ] **Step 4: Commit**

```bash
git add backend/scanner.py
git commit -m "feat(scanner): walk recognises VIDEO_TS / BDMV folders, skips internal recursion (v0.6.0)"
```

---

## Task 4: Sibling-detection prefix-match for disc items

**Files:**
- Modify: `backend/scanner.py`

- [ ] **Step 1: Extend the already-converted-sibling skip loop**

Find the existing loop at scanner.py:920+ (`from backend.converter import rename_source_to_target_codec, rename_source_quality_in_filename`). After the existing logic that handles file siblings, add an additional branch for disc-marker paths.

Per the spec (§Filename rename + sibling detection): for a disc item, the converted sibling is an MKV in the **parent folder** (one level up from VIDEO_TS / BDMV) whose stem starts with the parent folder's name AND contains either `DVDRip` (for DVD) or `Bluray` (for BDMV).

```python
    # v0.6.0: disc sibling detection. For a disc-marker file (e.g.
    # `Movie/VIDEO_TS/VIDEO_TS.IFO`), the converted MKV would sit in
    # the parent folder (`Movie/`) and be named with the parent-folder
    # name + DVDRip|Bluray + various probe-derived tokens. Prefix-match
    # on parent folder name + source-quality token is robust to the
    # probe-time variability of the other tokens.
    for f in list(all_files):
        if f.name == "VIDEO_TS.IFO" and f.parent.name == "VIDEO_TS":
            disc_root = f.parent.parent  # the Movie/ folder
            disc_root_name = disc_root.name
            for sibling in disc_root.iterdir():
                if sibling.suffix.lower() != ".mkv":
                    continue
                if not sibling.stem.startswith(disc_root_name):
                    continue
                if "dvdrip" in sibling.stem.lower():
                    skip_paths.add(str(f))
                    break
        elif f.name == "index.bdmv" and f.parent.name == "BDMV":
            disc_root = f.parent.parent
            disc_root_name = disc_root.name
            for sibling in disc_root.iterdir():
                if sibling.suffix.lower() != ".mkv":
                    continue
                if not sibling.stem.startswith(disc_root_name):
                    continue
                if "bluray" in sibling.stem.lower():
                    skip_paths.add(str(f))
                    break
```

(Adapt to the actual `skip_paths` variable name — the existing code stores skip targets in either `skip_paths` or similar; match what's there.)

- [ ] **Step 2: Syntax check**

```bash
python3 -c "import ast; ast.parse(open('backend/scanner.py').read()); print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add backend/scanner.py
git commit -m "feat(scanner): disc-item sibling skip-detection on prefix-match (v0.6.0)"
```

---

## Task 5: Watcher disc detection

**Files:**
- Modify: `backend/watcher.py`

- [ ] **Step 1: Add disc-folder discovery to the watcher's new-file path**

The watcher's polling loop produces `new_files` — paths it just discovered. Find that loop (search for `new_files` assignments) and add a pre-pass that maps disc-folder discoveries to their marker files:

```python
    # v0.6.0: when the watcher discovers a path inside VIDEO_TS/ or
    # BDMV/, map it to the disc-marker file the scanner expects.
    # When it discovers the disc folder itself (Movie/), check for
    # markers and add them. Either way, the marker file is what flows
    # through the rest of the pipeline.
    from backend.scanner import _classify_disc, _disc_marker_path

    new_files_disc_adjusted = []
    seen_discs: set[str] = set()
    for fp in new_files:
        p = Path(fp)
        # Case A: path is inside VIDEO_TS or BDMV → use the disc root's marker
        if "VIDEO_TS" in p.parts or "BDMV" in p.parts:
            # Walk up to find the disc-root (the folder CONTAINING VIDEO_TS or BDMV)
            disc_root = p
            while disc_root.parent != disc_root:
                if disc_root.name in ("VIDEO_TS", "BDMV"):
                    disc_root = disc_root.parent
                    break
                disc_root = disc_root.parent
            disc_type = _classify_disc(disc_root)
            if disc_type:
                marker = str(_disc_marker_path(disc_root, disc_type))
                if marker not in seen_discs:
                    new_files_disc_adjusted.append(marker)
                    seen_discs.add(marker)
                continue  # don't add the inner VOB/M2TS file
        # Case B: path is the disc-root folder itself (rare; depends on
        # filesystem-event granularity)
        if p.is_dir():
            disc_type = _classify_disc(p)
            if disc_type:
                marker = str(_disc_marker_path(p, disc_type))
                if marker not in seen_discs:
                    new_files_disc_adjusted.append(marker)
                    seen_discs.add(marker)
                continue
        # Default: regular file, pass through
        new_files_disc_adjusted.append(fp)

    new_files = new_files_disc_adjusted
```

Place this pre-pass right before the existing `for file_path in new_files:` loop.

- [ ] **Step 2: Syntax check**

```bash
python3 -c "import ast; ast.parse(open('backend/watcher.py').read()); print('OK')"
```

- [ ] **Step 3: Commit**

```bash
git add backend/watcher.py
git commit -m "feat(watcher): map VIDEO_TS / BDMV discoveries to disc-marker paths (v0.6.0)"
```

---

## Task 6: Output filename builder

**Files:**
- Modify: `backend/converter.py`

- [ ] **Step 1: Add `build_disc_output_filename()`**

In `backend/converter.py`, near the existing `get_output_path` function, add:

```python
def build_disc_output_filename(
    disc_marker_path: str,
    disc_type: str,
    probe_data: dict,
    encoder: str | None = None,
) -> str:
    """Construct a scene-style filename for a converted disc, parent
    folder name + probe-derived resolution / audio / channels / codec
    tokens. Reuses backend/rename.py helpers for channels and audio
    codec formatting; reuses converter._hevc_tag_for_encoder() for
    the video codec token.

    Output goes to the disc-root folder (parent of VIDEO_TS/BDMV),
    NOT inside VIDEO_TS/BDMV. v0.6.0.

    Example: `/Movies/Movie (1999) [tt0133093]/VIDEO_TS/VIDEO_TS.IFO`
             → `/Movies/Movie (1999) [tt0133093]/Movie (1999) [tt0133093] 480p DVDRip AC3 2.0 x265.mkv`
    """
    from backend.rename import _format_channels
    p = Path(disc_marker_path)
    disc_root = p.parent.parent  # strip /VIDEO_TS/VIDEO_TS.IFO (or /BDMV/index.bdmv)
    base_name = disc_root.name

    # Resolution token from probe video height
    h = int(probe_data.get("video_height") or 0)
    if h >= 2000: res = "2160p"
    elif h >= 1000: res = "1080p"
    elif h >= 700: res = "720p"
    elif h >= 560: res = "576p"
    else: res = "480p"

    source_quality = "Bluray" if disc_type == "bdmv" else "DVDRip"

    # Primary audio: first track. Codec → uppercase scene tag.
    audio_tracks = probe_data.get("audio_tracks") or []
    audio_token = ""
    channels_token = ""
    if audio_tracks:
        a = audio_tracks[0]
        codec_raw = (a.get("codec") or "").lower()
        # Scene-style codec naming
        if codec_raw == "eac3": audio_token = "EAC3"
        elif codec_raw == "ac3": audio_token = "AC3"
        elif codec_raw == "dts": audio_token = "DTS"
        elif codec_raw == "truehd": audio_token = "TrueHD"
        elif codec_raw == "flac": audio_token = "FLAC"
        elif codec_raw == "aac": audio_token = "AAC"
        elif codec_raw.startswith("pcm"): audio_token = "LPCM"
        elif codec_raw: audio_token = codec_raw.upper()
        ch = int(a.get("channels") or 0)
        if ch > 0:
            channels_token = _format_channels(ch)

    # Encoder tag (reuse converter helper)
    codec_tag = _hevc_tag_for_encoder(encoder)  # "x265" or "h265"

    # Assemble scene-style name (space-separated to match user's library style)
    tokens = [base_name, res, source_quality]
    if audio_token: tokens.append(audio_token)
    if channels_token: tokens.append(channels_token)
    tokens.append(codec_tag)
    name = " ".join(tokens) + ".mkv"
    return str(disc_root / name)
```

- [ ] **Step 2: Self-test**

```bash
python3 -c "
from backend.converter import build_disc_output_filename

# DVD case from the user's library
probe = {
    'video_height': 480,
    'audio_tracks': [{'codec': 'ac3', 'channels': 2}],
}
out = build_disc_output_filename(
    '/Movies/Fast-Walking (1982) [tt0083930]/VIDEO_TS/VIDEO_TS.IFO',
    'dvd', probe, encoder='libx265',
)
print('DVD:', out)
expected = '/Movies/Fast-Walking (1982) [tt0083930]/Fast-Walking (1982) [tt0083930] 480p DVDRip AC3 2.0 x265.mkv'
assert out == expected, f'got: {out!r}\nexpected: {expected!r}'

# BDMV case
probe = {
    'video_height': 1080,
    'audio_tracks': [{'codec': 'dts', 'channels': 6}],
}
out = build_disc_output_filename(
    '/Movies/The Matrix (1999) [tt0133093]/BDMV/index.bdmv',
    'bdmv', probe, encoder='nvenc',
)
print('BDMV:', out)
expected = '/Movies/The Matrix (1999) [tt0133093]/The Matrix (1999) [tt0133093] 1080p Bluray DTS 5.1 h265.mkv'
assert out == expected

# Edge: no audio track info
probe = {'video_height': 720, 'audio_tracks': []}
out = build_disc_output_filename(
    '/Movies/Old Movie/VIDEO_TS/VIDEO_TS.IFO',
    'dvd', probe, encoder='libx265',
)
print('No-audio DVD:', out)
assert out == '/Movies/Old Movie/Old Movie 720p DVDRip x265.mkv'

print('All cases pass.')
"
```

- [ ] **Step 3: Commit**

```bash
git add backend/converter.py
git commit -m "feat(converter): build_disc_output_filename for scene-style disc output names (v0.6.0)"
```

---

## Task 7: Converter integration — route disc input through ffmpeg's protocol

**Files:**
- Modify: `backend/converter.py`

- [ ] **Step 1: Detect disc input in `convert_file()` and set the protocol-prefixed input**

Find `convert_file()`. Right after `source_video_codec` / `source_pix_fmt` get populated from `probe_data`, add disc detection:

```python
    # v0.6.0: disc-folder input. probe_data["disc_type"] is set by the
    # scanner when file_path points at a VIDEO_TS.IFO or BDMV/index.bdmv.
    # Encode-input then becomes ffmpeg's dvd:/ or bluray:/ protocol with
    # the disc-root folder; ffmpeg auto-selects the longest title.
    disc_type = probe_data.get("disc_type") if probe_data else None
    if disc_type:
        disc_root = Path(input_path).parent.parent
        protocol = "bluray" if disc_type == "bdmv" else "dvd"
        encode_input_path = f"{protocol}:{disc_root}"
        print(f"[CONVERT] Disc input detected ({disc_type}); using {protocol}:/{disc_root.name}", flush=True)
    else:
        encode_input_path = input_path
```

Find every other place in `convert_file()` that currently passes `input_path` to ffmpeg sub-cmds (encoder cmd builder, prestrip, etc.) and switch to `encode_input_path` for disc cases. Most of these are already using a local `encode_input_path` variable; verify each one.

- [ ] **Step 2: Force HW decode off for disc input**

After the existing `_hw_use = False; _hw_backend = None; _hw_on_device = True` block, add a disc override:

```python
    if disc_type:
        # ffmpeg's dvd:/ and bluray:/ protocols always demux through
        # the native software decoder; -hwaccel is silently ignored.
        # Set the flags to False so downstream filter-chain logic
        # doesn't try to insert scale_cuda etc. that would expect HW
        # frames.
        _hw_use = False
        _hw_backend = None
        _hw_on_device = True
        print(f"[CONVERT] HW decode bypassed for disc input (ffmpeg protocol demux is software-only)", flush=True)
    hw_decode_active = _hw_use
```

- [ ] **Step 3: Output path override for disc**

Find where the output path is determined (likely `output_path = get_output_path(input_path, ..., encoder=encoder)`). Wrap it:

```python
    if disc_type:
        output_path = build_disc_output_filename(
            input_path, disc_type, probe_data, encoder=encoder,
        )
    else:
        output_path = get_output_path(input_path, suffix=..., encoder=encoder)
```

- [ ] **Step 4: Adjust temp-path so it's not inside the disc subdirectory**

`get_temp_path()` puts `.converting.mkv` in the same directory as the input. For disc input, that'd land inside `VIDEO_TS/` or `BDMV/`. Either:
- Compute the temp path from `output_path` instead (which is already in the disc-root folder), OR
- Add a `disc_type`-aware branch in `get_temp_path`

Use whichever pattern matches the surrounding code best. Probably simplest:

```python
    if disc_type:
        temp_path = str(Path(output_path).with_suffix(".converting.mkv"))
    else:
        temp_path = get_temp_path(input_path)
```

- [ ] **Step 5: Sanity test — disc-input cmd shape**

```bash
python3 -c "
# Mock convert_file's cmd-building entrypoint via _build_ffmpeg_cmd_impl
from backend.converter import _build_ffmpeg_cmd_impl
cmd = _build_ffmpeg_cmd_impl(
    'dvd:/Movies/Fast-Walking (1982) [tt0083930]',
    '/Movies/Fast-Walking (1982) [tt0083930]/Fast-Walking 480p DVDRip AC3 2.0 x265.mkv',
    encoder='nvenc',
    source_codec='mpeg2video',
)
# Verify -i is the protocol-prefixed path
i = cmd.index('-i')
assert cmd[i+1] == 'dvd:/Movies/Fast-Walking (1982) [tt0083930]', f'-i argument: {cmd[i+1]!r}'
print('OK — disc-input cmd shape:')
print(' '.join(cmd[:8]))
"
```

- [ ] **Step 6: Commit**

```bash
git add backend/converter.py
git commit -m "feat(converter): route disc input via dvd:/ / bluray:/ protocols (v0.6.0)"
```

---

## Task 8: Post-conversion source-handling for disc folders

**Files:**
- Modify: `backend/converter.py` (or `backend/queue.py` if that's where source handling lives — verify)

- [ ] **Step 1: Find the existing post-conversion source-handling site**

Search for where the original file gets deleted / trashed / kept after a successful conversion. Likely in `convert_file()` near the end, around the "trash" / "delete" / "backup" handling.

- [ ] **Step 2: Extend it to handle disc subdirectories**

When `disc_type` is set, the "source" being handled is the **disc subdirectory** (`VIDEO_TS/` or `BDMV/`), not the marker file:

```python
    # v0.6.0: disc-source handling. The "original" is the entire disc
    # subdirectory (VIDEO_TS/ or BDMV/), not just the marker file.
    if disc_type:
        disc_subdir = Path(input_path).parent  # /Movies/Foo/VIDEO_TS
        # Replace the existing single-file delete/move with a directory operation.
        # Behaviour mapping:
        #   delete  → shutil.rmtree(disc_subdir)
        #   trash   → shutil.move(disc_subdir, trash_target_dir / disc_subdir.name)
        #   keep    → no-op
    else:
        # ... existing file handling ...
```

Adapt to the actual existing source-handling function names / settings. The key insight is that **the disc-marker path's parent** is the directory we want to operate on, not the marker file or the disc-root folder.

- [ ] **Step 3: Commit**

```bash
git add backend/converter.py
git commit -m "feat(converter): post-conversion source-handling for disc subdirectories (v0.6.0)"
```

---

## Task 9: Frontend — disc icon + badge in Scanner page

**Files:**
- Modify: `frontend/src/api.ts` (or wherever `ScannedFile` TS type lives)
- Modify: `frontend/src/pages/ScannerPage.tsx` (or the row-rendering component)

- [ ] **Step 1: Extend the `ScannedFile` TS type**

Find the `ScannedFile` interface (likely in `frontend/src/api.ts` or `frontend/src/types.ts`). Add:

```typescript
disc_type?: "dvd" | "bdmv" | null;
```

- [ ] **Step 2: Render a disc icon + badge for disc items**

In the scanner row component (wherever the file's codec badge renders), branch on `disc_type`:

```tsx
{file.disc_type === "dvd" && (
  <span style={{ /* small badge styling matching the existing codec badge */ }}>
    DVD
  </span>
)}
{file.disc_type === "bdmv" && (
  <span style={{ /* same badge styling */ }}>
    Blu-ray
  </span>
)}
```

Use a small SVG disc icon (Lucide's `Disc` or inline SVG) inline with the badge. Match the styling of the existing `MPEG-2` / `H.264` etc. codec badges that already exist in the file list.

For the title display: when `disc_type` is set, render the disc-root folder name (one level up from `parent.parent` of `file_path`) instead of the marker filename. The existing render probably uses `file.file_name` — verify it's set to the disc-root name by the backend (Task 3 should populate this correctly via the scanner's `file_name` derivation).

- [ ] **Step 3: TypeScript compile check**

```bash
cd frontend && npx tsc --noEmit 2>&1 | head -10
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/api.ts frontend/src/pages/ScannerPage.tsx
git commit -m "feat(scanner): disc icon + DVD/Blu-ray badge in Scanner rows (v0.6.0)"
```

---

## Task 10: Smoke test + ship v0.6.0

**Files:**
- Modify: `VERSION`, `CHANGELOG.md`

- [ ] **Step 1: End-to-end test (on real hardware if available)**

If a test DVD or BDMV folder is available, run an actual scan + convert:

```bash
# Drop a test DVD folder into a watched media dir
# Verify scan picks it up as DVD
# Add to queue
# Verify ffmpeg cmd uses dvd:/ protocol
# Verify output filename is in scene-style
# Verify VIDEO_TS folder is deleted (or trashed) after success
```

If no real disc is available, this can be deferred — the helper-level sanity tests in earlier tasks cover the unit behaviour.

- [ ] **Step 2: Bump VERSION**

```bash
echo "0.6.0" > VERSION
```

- [ ] **Step 3: Add CHANGELOG entry**

Prepend to `CHANGELOG.md` above the v0.5.26 entry. One-liner format per the project's CLAUDE.md preference:

```markdown
## [0.6.0] — 2026-05-20

### Added
- **DVD and Blu-ray folder support**: raw `VIDEO_TS/` and `BDMV/` folder structures are now scannable items. Shrinkerr detects them automatically, picks the longest title via ffmpeg's `dvd:/` / `bluray:/` protocols (libdvdread/libdvdnav/libbluray, already in the image), and transcodes that title to HEVC. Output is a scene-style MKV in the disc's parent folder; the original `VIDEO_TS/` or `BDMV/` subdirectory follows the user's existing post-conversion source-handling setting (delete / trash / keep). Combo discs with both VIDEO_TS and BDMV are treated as Blu-ray. Main-feature only — extras and menus discarded.
```

- [ ] **Step 4: Commit + tag + push**

```bash
git add VERSION CHANGELOG.md
git commit -m "release: v0.6.0 — DVD / Blu-ray disc folder conversion"
git tag v0.6.0
git push origin main
git push origin v0.6.0
```

---

## Acceptance checklist (from spec)

After all tasks complete, verify each spec criterion:

- [ ] Scanner walks a tree with VIDEO_TS + BDMV; both appear as disc items, VOB/M2TS files are NOT separate rows
- [ ] ffprobe via `dvd:/` / `bluray:/` populates scan_results with correct codec, audio, duration, aggregated file_size
- [ ] Hybrid disc (both VIDEO_TS and BDMV) classifies as BDMV
- [ ] Queueing a disc creates `convert` job using `dvd:/` or `bluray:/` input
- [ ] Output filename matches scene-style pattern, lands in parent folder
- [ ] Disc subdirectory is deleted / trashed / kept per the user's existing setting after success
- [ ] Sibling skip-detect: scanning a folder with both disc subdir AND its converted MKV flags the disc as skip
- [ ] Scanner UI shows disc icon + DVD/Blu-ray badge for disc rows
- [ ] No regression for regular file scans / conversions
- [ ] HW decode is bypassed for disc input; worker logs note the bypass
