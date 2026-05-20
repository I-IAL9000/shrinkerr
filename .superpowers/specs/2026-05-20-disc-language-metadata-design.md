# DVD IFO + Blu-ray mpls Language Metadata Parsing Design Spec

**Version target:** v0.6.5 (continuation of the v0.6.x disc-folder feature line)
**Status:** Design — awaiting implementation plan
**Decisions locked via Q&A:** hand-rolled parsers, fail-open to und, auto-backfill, v0.6.5 release

---

## Background

v0.6.0 added DVD VIDEO_TS / Blu-ray BDMV folder support. v0.6.2 replaced the fictional `dvd:` ffmpeg protocol with `concat:` over main-feature VOBs. v0.6.4 fixed the cosmetic post-conversion failure. The feature works end-to-end for both DVDs (Fast-Walking 1982 — verified) and BDMVs (Elephant 2003 — verified).

One remaining defect: **every disc surfaces all audio and subtitle tracks with `language=und`**.

Root cause, by disc type:

- **DVD**: We bypass libdvdread entirely by concat-demuxing raw VOB files. The VOB Program Streams (MPEG-2 PS) identify tracks by sub-PID, not by language. Language metadata lives in the IFO sidecar (`VTS_NN_0.IFO`), which we currently never read.
- **Blu-ray**: libbluray (via ffmpeg's `bluray:` protocol) does read `.mpls` playlist files, but many commercial discs don't actually tag their streams with ISO 639 codes in the playlist headers. The metadata exists in menu BD-J / IGS packages as pixel art, not machine-readable fields. So ffprobe correctly returns what libbluray gave it: nothing.

User impact: with `always_keep_languages = ["eng", "isl", "ice"]`, every disc track gets dropped during conversion because `und` matches none of those. Result is a video-only output. User must manually re-select every track before queueing.

User stated requirement: "I want IFO / mpls metadata parsing to be sure, these could be german or japanese blurays/dvds and I don't want to be guessing the language." Heuristics (TMDB original_language lookup, treat-und-as-native) are explicitly rejected.

## Scope (locked via Q&A)

- **Hand-rolled pure-Python parsers** for both IFO and mpls. No new pip / apt deps. Both formats are 25+ years stable and well-documented.
- **Fail-open on parser failure**: log a `[DISC-META]` warning and return empty lists. Tracks stay `und`. Disc still gets added to the scanner. Worst case: same as today.
- **One-shot auto-backfill** of existing disc rows (those with `und` tracks) on first watcher cycle after v0.6.5 boots. Idempotent via a `settings` table flag.
- **Single integration point** in `backend/scanner.py:probe_file` — after ffprobe builds the track dicts, patch language fields by stream-order index.

## Design

### Module layout

New file `backend/disc_metadata.py` (~250 LoC) containing:

- `parse_disc_languages(disc_folder: Path, disc_type: str) -> dict` — public entry point
- `_parse_dvd_ifo(ifo_path: Path) -> dict` — DVD VTS IFO parser
- `_parse_bdmv_mpls(mpls_path: Path) -> dict` — single BDMV mpls parser
- `_find_main_bdmv_playlist(playlist_dir: Path) -> Path | None` — picks longest-duration .mpls
- `_iso639_1_to_2(code: str) -> str` — 2-letter to 3-letter language code mapping
- `_ISO639_1_TO_2` — static dict, ~50 common language entries

Return shape for all parsers (and the public entry point):

```python
{"audio": [str, ...], "subtitle": [str, ...]}
```

Each list contains ISO 639-2 (3-letter) language codes in stream order. Empty string `""` for unknown/unmapped codes (caller maps to `"und"`). Empty lists on parser failure.

### DVD IFO parser (`_parse_dvd_ifo`)

VTS_NN_0.IFO structure (DVD-Video spec, libdvdread `ifo_types.h`):

```
bytes [0:12]            : "DVDVIDEO-VTS" magic — fail-fast if mismatch
byte  [0x202]           : nr_of_audio_streams (0-8)
bytes [0x204:0x244]     : audio_attrs[] — 8 entries × 8 bytes each
                          per entry:
                            +0  audio_format / multichan / lang_type / app_mode (packed)
                            +1  quantization / sample_freq / channels (packed)
                            +2,+3  lang_code (2-byte ASCII ISO 639-1, e.g. "en")
                            +4  lang_extension
                            +5  code_extension
                            +6,+7  padding
byte  [0x254]           : nr_of_subp_streams (0-32)
bytes [0x256:0x316]     : subp_attrs[] — 32 entries × 6 bytes each
                          per entry:
                            +0  code_mode / lang_type (packed)
                            +1  reserved
                            +2,+3  lang_code (2-byte ASCII ISO 639-1)
                            +4  lang_extension
                            +5  code_extension
```

Algorithm:

1. Read first 0x320 bytes from `VTS_NN_0.IFO`.
2. Assert magic `data[:12] == b"DVDVIDEO-VTS"`. On mismatch, return empty.
3. `n_audio = data[0x202]` (clamp to 8 defensively).
4. For `i` in `range(n_audio)`: extract `data[0x204 + 8*i + 2 : 0x204 + 8*i + 4]`, decode as ASCII, lookup in `_ISO639_1_TO_2`.
5. `n_subp = data[0x254]` (clamp to 32).
6. For `i` in `range(n_subp)`: extract `data[0x256 + 6*i + 2 : 0x256 + 6*i + 4]`, decode + map.
7. Any exception during read/decode → log + return `{"audio": [], "subtitle": []}`.

Title set NN is already computed by `_dvd_main_title_vobs()` in v0.6.2 (largest VTS_NN by total VOB size). We use the same NN to locate the IFO file.

### Blu-ray mpls parser (`_parse_bdmv_mpls` + `_find_main_bdmv_playlist`)

mpls file structure (BD-ROM Part 3 spec):

```
Header (40 bytes):
  bytes [0:4]    : "MPLS" magic
  bytes [4:8]    : version ("0100" / "0200" / "0300" all valid)
  bytes [8:12]   : PlayList_start_address (uint32 BE)
  bytes [12:16]  : PlayListMark_start_address (uint32 BE)
  bytes [16:20]  : ExtensionData_start_address (uint32 BE)
  bytes [20:40]  : reserved

At PlayList_start_address:
  uint32 BE      : length
  uint16 BE      : reserved
  uint16 BE      : number_of_PlayItems
  uint16 BE      : number_of_SubPaths
  PlayItem[] follows

Each PlayItem (variable length, declared in first 2 bytes):
  uint16 BE      : length (NOT including these 2 bytes)
  bytes [2:7]    : clip_id (5 ASCII)
  bytes [7:11]   : clip_codec_id (4 ASCII, "M2TS")
  uint16 BE      : flags (incl. IsMultiAngle bit)
  byte           : ref_to_STC_id
  uint32 BE      : IN_time (45 kHz ticks)
  uint32 BE      : OUT_time
  bytes [22:30]  : UO_mask_table (8 bytes)
  byte [30]      : flags
  byte [31]      : still_mode
  uint16 BE      : still_time
  [conditional]  : multi_clip_entries (if IsMultiAngle)
  STN_table follows

STN_table:
  uint16 BE      : length
  uint16 BE      : reserved
  byte           : number_of_primary_audio_streams
  byte           : number_of_primary_PG_streams (subtitles)
  byte           : number_of_primary_IG_streams (menus)
  byte           : number_of_secondary_audio_streams
  byte           : number_of_secondary_video_streams
  byte           : number_of_secondary_PG_streams
  bytes [10:22]  : reserved (12 bytes)
  StreamEntry + StreamAttributes pairs follow (video, audio, PG, IG order)

StreamEntry:
  byte           : length
  byte           : stream_type
  type-specific  : (skip — variable length)

StreamAttributes:
  byte           : length
  byte           : stream_coding_type
  [coding-type-specific bytes]
  bytes [3:6]    : lang_code (3-byte ASCII ISO 639-2) — for audio/PG/IG
  [padding to declared length]
```

Algorithm for `_find_main_bdmv_playlist(playlist_dir)`:

1. Enumerate `playlist_dir.glob("*.mpls")`.
2. For each .mpls: parse header, walk PlayItem array, sum `(OUT_time - IN_time) / 45000` across all PlayItems → total duration in seconds.
3. Return the .mpls with the largest total duration. This replicates libbluray's auto-pick.
4. Return `None` if no .mpls files or all fail to parse.

Algorithm for `_parse_bdmv_mpls(mpls_path)`:

1. Read entire .mpls file.
2. Verify "MPLS" magic.
3. Seek to `PlayList_start_address`, read first PlayItem (languages are consistent across all PlayItems in a single playlist).
4. Walk past the PlayItem header fields to the STN_table.
5. Read stream counts.
6. For each audio entry: skip the StreamEntry block (length-prefixed), read StreamAttributes, extract `lang_code` (3 bytes at offset +3 within the attributes block), decode as ASCII, strip whitespace.
7. Same for PG (subtitle) entries.
8. Return `{"audio": [...], "subtitle": [...]}`.
9. Any exception → log + return empty.

### Stream-order correlation

The single load-bearing assumption: **the parser's stream order matches ffprobe's stream order**.

Why this holds:

- **DVD**: VOB Program Streams place audio at sub-PIDs `0xBD/0x80+N` (AC-3), `0xBD/0xA0+N` (LPCM/DTS); subtitles at `0xBD/0x20+N`. The IFO's audio_attrs array indexes 1..N correspond to sub-PIDs 0x80..0x80+N-1 in PID order. ffprobe via `concat:` enumerates streams in PID order. → Positional 1:1.
- **Blu-ray**: STN_table primary_audio entries appear in PID order in the .mpls binary. libbluray (via ffmpeg's `bluray:` protocol) enumerates streams in the same order. → Positional 1:1.

Hedge: if `len(audio_tracks) ≠ len(langs["audio"])` at the merge step, we patch `min(M, N)` tracks and log `[DISC-META] count mismatch: ffmpeg N audio vs IFO M`. Excess tracks on either side stay `und`. We don't try to guess which got dropped.

### Language code normalization

DVD IFO returns 2-byte ISO 639-1 codes (e.g. `"en"`, `"de"`, `"ja"`). Shrinkerr's keep-language settings and ffprobe's convention both use 3-byte ISO 639-2 (`"eng"`, `"ger"`, `"jpn"`). Static dict `_ISO639_1_TO_2` covers ~50 common languages:

```
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
```

(Final exact list confirmed during implementation against ISO 639 authoritative table.)

Unknown 2-letter codes return `""`. Zeroed bytes (`b"\x00\x00"`) return `""`. Callers convert `""` to `"und"` at merge time.

BD mpls returns 3-byte ISO 639-2 already — pass through after `.decode("ascii", errors="replace").strip()`. Empty/whitespace strings return `""`.

### Integration point

`backend/scanner.py:probe_file` already routes disc markers (v0.6.2 work). After ffprobe builds `audio_tracks` and `subtitle_tracks`, before constructing the return dict, ~10 lines:

```python
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
            print(f"[DISC-META] count mismatch for {disc_folder}: "
                  f"ffmpeg audio={len(audio_tracks)}/IFO {len(langs['audio'])}, "
                  f"ffmpeg sub={len(subtitle_tracks)}/IFO {len(langs['subtitle'])}",
                  flush=True)
    except Exception as exc:
        print(f"[DISC-META] failed for {disc_folder}: {exc}", flush=True)
```

Nothing else in the probe flow changes. `classify_audio_tracks` downstream consumes the patched `language` fields the same way it does for regular files.

### Backfill

Existing disc rows in `scan_results` predating v0.6.5 still have `und` tracks stored in `audio_tracks_json`. On first container start running v0.6.5, a one-shot pass re-probes them and updates the rows.

Flag: row `disc_lang_backfilled_v065 = "true"` in `settings` table (key/value schema, already in use for other flags).

Trigger: end of FileWatcher's first `check_once()` call. Async, non-blocking on FastAPI startup.

Algorithm:

```python
async def _backfill_disc_languages_v065(self):
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        async with db.execute(
            "SELECT value FROM settings WHERE key='disc_lang_backfilled_v065'"
        ) as cur:
            row = await cur.fetchone()
        if row and row["value"] == "true":
            return
        async with db.execute(
            "SELECT file_path FROM scan_results "
            "WHERE disc_type IS NOT NULL "
            "AND audio_tracks_json LIKE '%\"language\":\"und\"%'"
        ) as cur:
            candidates = [r["file_path"] for r in await cur.fetchall()]
        if candidates:
            print(f"[WATCHER] v0.6.5 backfill: re-probing {len(candidates)} disc rows",
                  flush=True)
        for fp in candidates:
            if not Path(fp).exists():
                continue  # stale row; let normal stale-removal handle it
            probe = await probe_file(fp)
            if probe is None:
                continue
            # Update the row via the same write path the watcher uses
            await self._update_disc_row_languages(fp, probe)
        await db.execute(
            "INSERT OR REPLACE INTO settings(key, value) "
            "VALUES('disc_lang_backfilled_v065', 'true')"
        )
        await db.commit()
    finally:
        await db.close()
```

`_update_disc_row_languages` writes only the changed fields (`audio_tracks_json`, `subtitle_tracks_json`, derived `has_removable_tracks`, `has_removable_subs`). Implementation should first look for an existing watcher / scanner helper that already does the equivalent partial-row update; if none fits cleanly, add a small focused helper rather than overload one that handles full-row writes.

For Hallur's current state (2 existing disc rows: Fast-Walking + Elephant): the backfill re-probes both, lands proper languages, and the keep-language filter then correctly drops French / keeps English for Elephant. Fast-Walking is already converted, so its row may or may not still exist by the time backfill runs — either way, the backfill is a no-op for missing paths.

## Testing

### Layer 1 — Unit tests (synthetic fixtures)

`backend/tests/test_disc_metadata.py` exercises the parsers against hand-crafted binary fixtures. Pure Python bytes literals; no real disc files in the repo.

Coverage:

- DVD: 2 audio + 2 sub langs (eng/ger, jpn/fre) — verifies normal path + multi-language
- DVD: empty lang_code bytes (`b"\x00\x00"`) — verifies pass-through-as-und
- DVD: malformed magic — verifies fail-open returns empty
- DVD: truncated file — verifies struct-error fail-open
- BDMV: full mpls with 2 audio + 2 sub entries, English+German — verifies normal path
- BDMV: two .mpls files of different durations — verifies longest-pick selection
- BDMV: version 0100 vs 0200 — verifies both versions parse
- ISO 639-1 → ISO 639-2 mapping: a handful of known codes + an unknown returning empty

Runs in CI on every commit.

### Layer 2 — Pre-tag integration verification (against Hallur's real discs)

Before any `git tag v0.6.5`, a diagnostic script runs against the actual container:

```bash
docker exec shrinkerr python3 /tmp/sk_disc_lang_verify.py
```

The script:

1. Calls `_parse_dvd_ifo` on Fast-Walking's `VTS_01_0.IFO`. Asserts `{"audio": ["eng"], "subtitle": []}` (English-only DVD, no subs).
2. Calls `_parse_bdmv_mpls` on Elephant's main playlist. Asserts `{"audio": ["fre", "eng"], "subtitle": ["fre", "eng"]}` (French/English bilingual BD per user confirmation).
3. Runs full new `probe_file` against both. For Fast-Walking: audio_tracks[0].language == "eng". For Elephant: audio_tracks[0].language == "fre", audio_tracks[1].language == "eng", same for subs.
4. Exits 0 only if all assertions pass.

The Elephant case is the load-bearing test: it verifies both multi-language parsing AND stream-order correlation. If the parser swaps tracks 0 and 1, the assertion fails — we don't ship.

If layer-2 fails: don't tag. Debug. Retry.

### Layer 3 — Multi-language coverage beyond English

Synthetic fixtures (layer 1) cover German, Japanese, French parsing. The real-disc layer 2 covers eng + fre. The unknowns are: do the parsers behave correctly for Asian-locale discs (which historically use ISO 639-1 codes like `ja`, `zh-Hant`, `ko`)?

The static `_ISO639_1_TO_2` dict covers `ja`, `zh`, `ko`. Synthetic fixtures verify the lookup. No real-disc coverage for these unless Hallur acquires test material later. Acceptable risk given format stability.

## Risk surface

| Risk | Mitigation |
|---|---|
| Stream-order assumption breaks on some unusual disc | Hedged by len-mismatch warning + partial patch + fail-open on parse errors |
| BD mpls picks the wrong playlist (multi-disc TV box sets where the largest .mpls is a play-all) | We mirror libbluray's pick (largest by total duration). If libbluray's pick matches what ffprobe enumerates, we agree by construction |
| IFO format variations (region/extension fields shifting offsets) | Hand-rolled parser uses fixed offsets per the spec; offsets are stable across all DVD-Video region/format variants per libdvdread's code |
| Corrupt IFO/mpls causes parser exception | All paths wrapped in try/except → log + return empty → track stays `und` (current behavior) |
| Backfill runs forever on slow CIFS / 100+ disc rows | Async; non-blocking. If interrupted (container restart), flag stays unset → resumes next start. Idempotent. |
| ISO 639-1 → -2 mapping table incomplete | Unknown codes return `""` → tracks stay `und` → user manually overrides in UI. Same as today |
| BD playlist file contains AACS-encrypted CPS data | We don't read encrypted regions. Headers + STN_table are always plaintext per spec |
| Disc on CIFS share temporarily disconnects mid-parse | OSError handled → empty return → track stays `und`. Next probe cycle retries |

## Acceptance criteria

1. **DVD parser correctness**: `_parse_dvd_ifo` against Fast-Walking's `VTS_01_0.IFO` returns a non-empty `audio` list whose first entry is `"eng"`. (Exact subtitle list discovered at layer 2 — could be `[]` if the disc authored 0 subtitle streams; we assert the actual IFO content matches what the user expects from the disc.)
2. **BDMV parser correctness**: `_parse_bdmv_mpls` against Elephant's main playlist returns `audio[0] == "fre"` and `audio[1] == "eng"` (per user confirmation: first audio track is French). Subtitle list shape verified at layer 2 against the user's expectations of the disc.
3. **Stream-order correlation**: after the merge step, Elephant's `audio_tracks[0].language == "fre"` AND `audio_tracks[1].language == "eng"` (not swapped). This is the load-bearing assertion that catches positional mapping bugs.
4. **Fail-open**: a parser run against a corrupted / truncated / missing file returns `{"audio": [], "subtitle": []}` and emits a `[DISC-META]` log line. probe_file itself does NOT return None — disc still gets added.
5. **Backfill idempotent**: after first run sets the flag, subsequent watcher cycles do NOT re-run the backfill loop.
6. **No regression for files**: regular file probe behavior byte-identical to v0.6.4 (the new code path only fires when `disc_type` is set).
7. **No regression for already-converted discs**: backfill skips paths whose source no longer exists.
8. **Unit test suite**: all `test_disc_metadata.py` cases pass.
9. **Keep-language filter works**: with Elephant re-probed, `classify_audio_tracks` marks audio[0] (fre) drop, audio[1] (eng) keep — matching user's `always_keep_languages = ["eng","isl","ice"]`.
10. **Pre-tag verification gate**: Layer 2 script exits 0 against both real discs before any `git tag v0.6.5`. If it fails, no tag.
