# Changelog

All notable changes to Shrinkerr are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.3] — 2026-07-13

### Added
- **Detected languages are now written back to the file, not just Shrinkerr's DB.** Clicking "Detect languages" (and the batch action) now also tags the file's tracks so the fix is real on disk without a re-encode — for the "just fix the unknown tracks" case. mkv files are edited in place via `mkvpropedit` (instant, no rewrite, no disk churn even on huge files); other containers fall back to an ffmpeg `-c copy` remux with atomic replace. Only tracks upgraded from `und` are written; already-tagged tracks are left alone. Fail-open — if the file write fails, the original is preserved and the DB detection is retained. Adds `mkvtoolnix` to the images.

## [0.8.2] — 2026-07-13

### Added
- **Charset detection for legacy non-Unicode subtitles.** Subtitles in GB2312/GBK/Big5 (Chinese), Shift-JIS (Japanese), EUC-KR (Korean), or Windows-1252/Latin-1 were being blind-decoded as Latin-1 — turning CJK double-byte text into mojibake that language detection couldn't read, leaving the track `und`. Now uses charset-normalizer to detect the real encoding before decoding, so these subs detect correctly (e.g. a GB2312 Chinese sub → `chi`). langdetect's regional codes (`zh-cn`/`zh-tw`) are normalized to `chi`.

### Changed
- **Conversion shows a "Detecting language…" status step** while faster-whisper runs on und audio tracks, instead of a silent pause before the encode.
- **The "Auto-detect languages" setting now appears in both the audio and subtitle sections** (one shared setting, shown in both places).

## [0.8.1] — 2026-07-13

### Fixed
- **v0.8.0 language detection didn't work.** Two bugs: (1) `faster-whisper` imports `requests`, which wasn't installed, so the model never loaded and every audio detection returned instantly with nothing found — added `requests` to requirements. (2) Subtitle text extraction used ffmpeg's srt **decoder** (`-f srt`), which rejects non-UTF-8 subtitles (Windows-1252/Latin-1, common in older/non-English releases) with "Invalid UTF-8 in decoded subtitles text" and produced zero output, leaving the track `und`. Now extracts with `-c:s copy` (raw bytes, no decode) and decodes tolerantly in Python (utf-8 → cp1252 → latin-1). **Note:** the faster-whisper tiny model still downloads on first audio-detection use — needs outbound network + a writable `/app/data/models`.

## [0.8.0] — 2026-07-13

### Added
- **Language detection for unknown (`und`) tracks.** Text subtitles are detected at scan time via langdetect; audio tracks via a faster-whisper tiny language-ID model — on-demand (a "Detect languages" button per file + a "Detect all unknown" batch action) and automatically before conversion when the new default-on **Auto-detect languages for unknown tracks** setting is enabled. Detected languages are confidence-gated (leave `und` rather than guess wrong), applied with `language_source="detected"`, and written to converted output as `-metadata:s:a/s:N language=` tags for both disc and regular files. Titles with unknown tracks surface in the audio-cleanup filter. faster-whisper's tiny model (~75 MB) downloads on first audio-detection use into the data volume; CUDA-accelerated on NVENC images, CPU elsewhere. Detection is fail-open — a failure never blocks a scan or conversion. **Image-based subtitles (PGS/VobSub) are deferred to v0.8.1** (they need OCR).

## [0.7.34] — 2026-07-08

### Fixed
- **AppleDouble `._*` sidecars still crashed conversions on v0.7.33** for already-scanned files. v0.7.33 fixed detection for *new* scans, but external subs are merged from **stored** `scan_results.subtitle_tracks_json` — rows scanned earlier still carried the `._` paths, so `._*.srt` / `._*.idx` were still fed to ffmpeg (exit 183/234) with no re-scan. Now guarded at the point of use: the convert-time external-sub merge skips hidden/AppleDouble paths via a shared `is_hidden_sidecar` helper, covering every stale row. Also made the external-subtitle map optional (`{idx}:s?`) so a sidecar that opens but exposes no subtitle stream can't hard-fail the job.

## [0.7.33] — 2026-07-08

### Fixed
- **Conversion failed (exit 183) when a macOS AppleDouble `._*.srt` sat next to the video.** External-subtitle detection matched `._<name>.eng.srt` resource-fork companions (they share the `.srt` extension and carry the episode key), then fed them to ffmpeg as `-i` inputs → `Invalid data found when processing input`, killing the whole conversion. Detection now skips hidden / dotfile subtitle candidates, mirroring the scanner walk's existing `._`/dotfile filter.

## [0.7.32] — 2026-07-08

### Fixed
- **Watcher stuck on "Scan in progress, skipping cycle" with no scan actually running.** When a scan subprocess hangs (e.g. `os.walk` blocked on a dead/slow network mount), `proc.is_alive()` stays True forever, the async monitor never returns, and `_scan_task` never completes — so the watcher skips every cycle indefinitely and no new items get picked up. New `scan_is_actively_running()` helper detects a hung scan via a stale progress file (no update in `SHRINKERR_STALE_SCAN_MINUTES`, default 15), kills the stuck subprocess, and clears the task so the watcher resumes — no container restart needed. The manual Scan / Rescan-folder endpoints use the same check, so hitting "Scan" also recovers.

## [0.7.31] — 2026-07-02

### Fixed
- **Conversion of a video-only source (no audio stream) failed with exit 234** (`Stream map '0:a' matches no streams`). Files with only video + subtitles — a video-only Blu-ray remux, or a sample clip — hit the default `-map 0:a` which ffmpeg treats as fatal when nothing matches. Changed to `-map 0:a?` (optional map) in both the main convert path and the subtitle-prestrip pass.

## [0.7.30] — 2026-06-28

### Fixed
- **Disc conversions left subtitle tracks tagged `und`** — the subtitle half of the v0.7.29 audio fix. The `bluray:` / `concat:` protocol strips embedded-subtitle language tags too, and the detected language was being dropped before it reached the ffmpeg command anyway (`subtitle_streams` was built with only codec + index, no `language`). Now carries the detected language through and stamps `-metadata:s:s:N language=X` on disc-conversion output subtitles. Regular files unaffected (ffmpeg copies the container tag). und/empty skipped.

## [0.7.29] — 2026-06-28

### Fixed
- **Disc conversions left audio tracks tagged `und` even when the language was detected.** Shrinkerr's IFO/mpls/libbluray parsers detect disc audio languages at scan time, but the conversion never wrote them to the output — the `bluray:` / `concat:` input protocol strips per-stream language tags, so `-c:a copy` produced `und` audio. The converter now injects `-metadata:s:a:N language=X` from the detected languages for disc conversions (both the map-all path and the track-removal/reorder keep-list path). Regular files are unaffected — they still rely on ffmpeg copying the tag from the source container. und/empty detections are skipped rather than written as bogus tags.

## [0.7.28] — 2026-06-28

### Fixed
- **Audio-cleanup remux failed with `dimensions not set` / `Could not write header` (exit 234)** on files with PNG/MJPEG cover art. The remux command mapped video with `-map 0:v?` (all video streams), which sweeps in attached-pic cover-art streams that the matroska muxer can't write a header for. Changed to `-map 0:v:0?` (first real video stream only), matching what the convert path already does. Cover art is dropped from the remux output, same as the convert path's deliberate behavior.

## [0.7.27] — 2026-05-31

### Added
- **macOS native install documentation** (`docs/native-install-mac.md`). Docker on Mac runs Linux containers in a VM that can't reach Apple's VideoToolbox encoder — performance is roughly 1 fps for software x265 vs. 30–100 fps native with VideoToolbox. New page covers brew prereqs, Python venv setup, frontend build, a `custom_ffmpeg_flags` recipe to invoke `hevc_videotoolbox` until it's a first-class encoder option, launchd service file, and troubleshooting. README + installation.md + troubleshooting.md updated to point Mac users at the native path.

## [0.7.26] — 2026-05-31

### Changed
- **Per-subfolder belt now triggers on absolute row count instead of percentage.** v0.7.22–25's `>50% of subfolder missing` trigger was the wrong signal — it fires on any entirely-deleted folder regardless of size, so deleting a single show (100% of that subfolder gone) still tripped preservation. The belt should only catch **catastrophic loss** (mount unmounted, drive failed) — typically 1000+ files lost from one subfolder. Switching to `≥ SHRINKERR_BELT_MIN_SIZE rows lost in one cycle` (default 1000) lets multi-show deletes, bulk renames, and even full-show wipes (100-500 rows) clean up normally while preserving genuine mount-loss patterns. **Tunable via the existing `SHRINKERR_BELT_MIN_SIZE` env var; semantics changed from "min subfolder size to protect" to "min absolute row-loss to trigger".**

## [0.7.25] — 2026-05-31

### Fixed
- **Per-subfolder belt was over-protective on small folders.** v0.7.22/23 preserved rows whenever a subfolder lost >50% in one cycle — which correctly caught the 20K-file partial-mount loss, but also caught "deleted a movie folder with 1 file" (100% missing for that subfolder) and refused to clean those rows. v0.7.25 adds a `MIN_BELT_PROTECTED_SIZE` threshold (default 5, env-tunable via `SHRINKERR_BELT_MIN_SIZE`): the belt only fires for subfolders with at least 5 known rows. Below that, recovery is trivial (one rescan, handful of files) and cleanup proceeds normally. Catastrophic-loss protection unchanged for large folders.

## [0.7.24] — 2026-05-31

### Fixed
- **Downscaled output filenames now reflect the new resolution.** A 2160p→1080p conversion produced `…2160p UHD x265…` filenames because `get_output_path` (and the disc-equivalent `build_disc_output_filename`) only rewrote the codec tag — the resolution tag passed through unchanged. v0.7.24 adds `rename_resolution_in_filename` which rewrites `2160p` / `1080p` / `720p` / `576p` / `480p` (any pixel-suffix form) plus colloquial `4K` / `UHD` markers when the encoder is downscaling. `target_resolution` is now threaded through both the regular-file and disc-file output-path builders and matches the value actually fed to the encoder. `target_resolution="copy"` (no scaling) leaves the filename's resolution tag alone.

## [0.7.23] — 2026-05-31

### Fixed
- **Manual scans now have the same per-subfolder partial-mount protection as the watcher.** v0.7.22 added a per-subfolder >50%-stale belt to the watcher's auto-removal, but `_scan_worker_process`'s orphan cleanup (the cleanup that runs after a user-initiated scan) didn't have it — a manual scan while a nested mount was pending would still delete every row under the affected subfolder. v0.7.23 mirrors the belt to the scanner side with the same threshold and log shape. **Known trade-off:** legitimate bulk moves now leave stale rows at the old path until manually cleaned from the UI — biased toward not-losing-data given the partial-mount loss has recurred three times.

## [0.7.22] — 2026-05-31

### Fixed
- **Per-subfolder protection against partial-mount stale-row deletion.** v0.7.7 added a global >50% sanity belt that aborts stale-row removal when more than half the library would be flagged stale in one cycle — fixes the unmounted-media_dir case. But that belt misses when a single subfolder under a walked media_dir is the unmounted one: a delayed Synology nested mount under `/media/.../TV1` leaves TV1 empty during the walk, every TV1 row gets flagged stale, and if TV1 is <50% of the library the global belt doesn't trip. v0.7.22 adds a per-subfolder sanity belt: any immediate subdirectory of a walked media_dir losing >50% of its known rows in one cycle is preserved (treated as likely partial-mount). Trigger a manual rescan once the mount is back to refresh those rows.

## [0.7.21] — 2026-05-31

### Fixed
- **Retrying a failed combined-encode job now actually re-runs the convert.** When a combined convert+cleanup job's video re-encode got discarded (e.g. NVENC crash, or the encoded output was larger than the original), the queue spawned an audio-only follow-up to apply the track cleanup without re-encoding. If that follow-up failed and the user clicked "retry", the old code re-ran the same audio-only job — so the source stayed h264 even though the user expected a fresh h265 convert. v0.7.21 escalates type='audio' retries to type='combined' when the source's `scan_results.needs_conversion` flag is still set. Audio-only retries on already-h265 sources stay as-is (legitimate track-cleanup use case). The response now returns `{"escalated": "audio→combined"}` when the upgrade fires.

## [0.7.20] — 2026-05-31

### Fixed
- **Broken watcher tests cleanup.** The v0.7.5–v0.7.8 watcher backfill tests (added under "skip local testing") INSERT'd into `scan_results` with a `file_name` column that doesn't exist and omitted the NOT NULL `file_size` + `scan_timestamp` columns — every one of them raised `OperationalError` immediately. CI never ran pytest so the breakage was silent. All 14 INSERT sites in `backend/tests/test_watcher.py` now use the real schema (drop `file_name`, supply `file_size=0` + sentinel `scan_timestamp`). Full watcher suite (16 tests) and database/audio/converter suites (42 combined) now pass locally. No production code touched.

## [0.7.19] — 2026-05-21

### Fixed
- **"database is locked" failures under concurrent writes.** SQLite's `busy_timeout` is per-connection (defaults to 0 = fail immediately) and doesn't persist with the DB file like WAL does. An audit found 118 of 134 `aiosqlite.connect` call sites didn't set the PRAGMA — every one of those would error instantly the moment a concurrent writer held the transaction. v0.7.19 monkey-patches `aiosqlite.connect` at module load in `backend/database.py` so every new connection applies `PRAGMA busy_timeout=60000` automatically. One surgical change, zero call-site edits, fully backward-compatible. Locked down with three tests against the `await` and `async with` invocation patterns.

## [0.7.18] — 2026-05-21

### Fixed
- **Audio-cleanup / remux failed with `Subtitle codec 94213 is not supported`** on sources containing `mov_text` subtitles (codec 94213 = `AV_CODEC_ID_MOV_TEXT`, typically present when an mp4 was previously remuxed to mkv). The remux path used `-map 0:s?` + global `-c copy`, which Matroska's muxer rejects for mov_text. v0.7.18 probes subtitle codecs upfront and builds per-stream maps so mov_text/tx3g get `-c:s:N srt` (lossless for text content) while everything else still copies cleanly. Main convert path already had this logic — this brings the remux path to parity.

## [0.7.17] — 2026-05-21

### Changed
- **Reworded the corrupt-source-detected message** from "Output file suspiciously small (… bytes vs … bytes) — likely corrupt" to lead with the actual diagnosis: "Original file likely corrupt — conversion produced an unusably-small output (N MB from N.NN GB source). ffmpeg hit data corruption ffprobe doesn't catch upfront. Source row marked corrupt; original preserved." The OUTPUT is the symptom; the ORIGINAL is what's corrupt. Same trigger (v0.7.11's <5%-of-original safety check), same DB side-effects (`health_status` / `probe_status` set on the source row).

## [0.7.16] — 2026-05-21

### Changed
- **Re-queuing an already-converted file gives a clear message** instead of a cryptic "Failed to probe file". When a conversion job's source no longer exists — almost always because it was already converted (the HEVC output replaced the h264 original) and the job is a stale retry or a queue from a pre-conversion scan row — the worker now reports "Source file no longer exists — most likely already converted; rescan to refresh" rather than treating it as a probe failure.

## [0.7.15] — 2026-05-21

### Fixed
- **v0.7.14 broke conversions** with `name 'encode_start_time' is not defined`. The v0.7.14 refactor moved the encode run-loop into a nested `_run_encode` helper, which scoped `encode_start_time` inside it — but the post-encode stats path (encode_seconds / encode_time) reads it from the outer function. Hoisted it to the outer scope and marked it `nonlocal` in the helper, alongside `all_lines`. Added an AST scope check to the dev process to catch this leak class.

## [0.7.14] — 2026-05-21

### Fixed
- **NVENC conversions that crash mid-stream now auto-retry with software decode.** NVDEC silently falls back to CPU decode for frames it can't handle on-GPU (sources with "unknown" colour metadata, certain WEB-DLs), and that mid-stream CUDA→CPU switch ("hwaccel changed") breaks the `scale_cuda` filter graph with `Error reinitializing filters!` (exit 218) — no input flag prevents it (v0.7.12's `-noautoscale` didn't). v0.7.14 detects that specific failure on the NVDEC-native CUDA path and retries the encode once with software decode + NVENC (software decoders handle every mid-stream change; the GPU still encodes). The fast NVDEC path stays the default for the 99% of files that work. Adds CI tests asserting the rebuilt command's structure (no `scale_cuda` on the software path; `-noautoscale` correctly positioned as an output option).

## [0.7.13] — 2026-05-21

### Fixed
- **v0.7.12 broke all conversions** with `exit 234 / Error parsing options for input file … Invalid argument`. The `-noautoscale` flag added in v0.7.12 is an ffmpeg *output* option but was placed before `-i`, so ffmpeg rejected it as a misplaced input option. Moved it after all `-i` inputs (still scoped to the NVDEC-native CUDA path). The v0.7.12 mid-stream-crash fix now actually works.

## [0.7.12] — 2026-05-21

### Fixed
- **NVENC conversion crashed mid-stream on some x264 WEB-DLs** with `Error reinitializing filters! / Impossible to convert between the formats supported by the filter 'Parsed_scale_cuda_0' and the filter 'auto_scale_0'`. ffmpeg auto-inserts an `auto_scale_0` CPU filter to bridge a mid-stream parameter reconfiguration ("hwaccel changed"), but that filter can't accept CUDA frames from our explicit `scale_cuda`. The encode dies after gigabytes of successful output. v0.7.12 adds `-noautoscale` on the NVDEC-native CUDA path so the auto-scaler isn't inserted — our explicit `scale_cuda=format=...` already handles all format alignment the encoder needs. QSV/VAAPI/CPU paths unaffected.

## [0.7.11] — 2026-05-21

### Fixed
- **Files corrupt enough to fail conversion now get flagged as corrupt in the UI** instead of staying labeled healthy. ffprobe sees container headers + first-frames metadata, so a source with corrupt data mid-stream passes the scan-time probe and shows up with "Convert to x265 (est. save N GB)" — until the user tries, the conversion produces a suspiciously-small output (the existing fail-safe catches this and preserves the original), and the row stayed labeled as healthy so the user could try again. v0.7.11 propagates the diagnosis: when the "output too small" check fires, the source row's `health_status` + `probe_status` get set to `corrupt` and the failure details go into `health_errors_json`. UI flags it via the existing isCorrupt indicator on next refresh.

## [0.7.10] — 2026-05-21

### Changed
- **ISO scan rows now display the `.iso` filename** (e.g. `rz0u.iso`) instead of the movie folder name (e.g. `Elephant (2003) [tt0363589]`). Folder discs unchanged (still show the disc-root folder, since the marker basename is useless as a label). Disambiguates folders holding multiple ISOs and matches what the file is actually named on disk. Affects both the scanner write path (`_disc_display_name`) and the API enricher (`_disc_aware_file_name`); `file_name` is computed at API time so existing rows pick up the new label immediately on next page load.

## [0.7.9] — 2026-05-21

### Changed
- **Scanner runs ffprobe in parallel** (default 4 concurrent). Pre-v0.7.9 scans were serial — a 120K-file library took ~12 h on a NUC because every ffprobe runs at ~300 ms. v0.7.9 pre-probes files in `asyncio.Semaphore`-bounded chunks, then runs the existing serial classify+emit loop with cached probe results. ~4× speedup on cold scans, no new DB-write contention with concurrent queue workers (probes are subprocess I/O only; writes still flow through the single-writer batched path). Tune via `SHRINKERR_SCAN_CONCURRENCY` (env var, default 4); set to 1 to restore serial behavior.

## [0.7.8] — 2026-05-21

### Fixed
- **v0.6.5 folder-disc backfill silently matched zero rows** (same JSON LIKE bug v0.7.5 had for BD ISOs, fixed in v0.7.6). New `_backfill_disc_languages_v078` runs the equivalent sweep for folder discs with the corrected selector and a fresh idempotency flag so existing installs re-run.
- **Stale corrupt markers persisted after a successful disc re-probe.** The `_clear_stale_disc_health_status` helper only cleared `health_status`, but the UI's `isCorrupt` is `probe_status === 'corrupt' || health_status === 'corrupt'`, so a probe that cleared `health_status` left the "ffprobe couldn't read a video stream" banner showing whenever `probe_status` was also stuck. Helper now resets `probe_status` (to `'ok'`) and `health_errors_json` alongside `health_status` in one statement; v0.7.8 backfill calls the helper on every successful row update.

## [0.7.7] — 2026-05-21

### Fixed
- **Watcher hard-DELETEd rows under temporarily-unmounted media volumes.** The watcher's stale-row pass computed `stale = known_paths - disk_files` across all configured media_dirs as one set. When one media_dir was missing from disk (e.g. running a docker-compose with a subset of volumes for RC testing), every row under it was flagged stale and deleted — recoverable only by a full rescan. v0.7.7 scopes the stale set to media_dirs that exist on disk this cycle, matching the scanner's existing `completed_paths` rule. Adds a sanity belt that aborts stale-removal if >50% of walked-dir rows would be deleted in one cycle. Loudly logs both events.

## [0.7.6] — 2026-05-21

### Fixed
- **v0.7.5 BD ISO backfill silently matched zero rows.** The selector used a SQL `LIKE '%"language":"und"%'` clause (no spaces) but `json.dumps()` defaults to `(', ', ': ')` separators, so production rows always store `"language": "und"` with a space after the colon. The sweep ran on every fresh install, pulled zero candidates, set the idempotency flag, and exited — no log line, no error, no update. v0.7.6 drops the SQL JSON LIKE clause entirely and does all language-shape filtering in Python where the parse is correct regardless of separator style. A new flag name (`iso_lang_backfilled_v076`) re-runs the sweep on installs that already had the broken v0.7.5 flag set. Adds a regression test that constructs fixtures via the production `json.dumps` serializer so this class of bug can't slip past again.

## [0.7.5] — 2026-05-21

### Added
- **One-shot startup backfill for BD ISO language metadata.** v0.7.4 added the libbluray ctypes path that finally extracts languages from UDF-only BD ISOs, but existing scan_results rows that pre-dated the fix kept their stale `und` track tags — users would have had to manually delete each affected row and let it re-discover. v0.7.5 sweeps those rows on first watcher cycle after upgrade: BD ISO rows with all-und (or empty) `audio_tracks_json` are re-probed, re-classified, and written back in-place. DVD ISOs and folder discs are unaffected (their pre-v0.7.4 path already worked); partial-coverage rows (e.g. `[eng, und]`) are left alone per scope. Idempotent via the `iso_lang_backfilled_v075` settings flag. Silent log-only signal (`[WATCHER] v0.7.5 backfill: …`).

## [0.7.4] — 2026-05-21

### Added
- **Language metadata for UDF-only Blu-ray ISOs** (closes v0.7.3's known limitation). bsdtar/libarchive 3.7.2 silently returns empty on UDF 2.50+ BD ISOs, so v0.7.1's fallback didn't actually help the discs it was added for. v0.7.4 adds a tier-3 fallback using libbluray directly via Python ctypes — the same library ffmpeg already uses for `bluray:` protocol encoding, so we know it can read these ISOs. Opens the ISO with `bd_open`, walks titles → clips → streams, reads per-track `lang[4]` from `BLURAY_STREAM_INFO`. ISO 639-2 codes normalized T→B form (`fra`→`fre`, `deu`→`ger`, etc.) to match the rest of the codebase. Adds `libbluray-bin` to both Dockerfiles. Verified against Elephant (2003) UDF-only BD ISO: `audio: [fre, eng]`, `subtitle: [fre, fre]`.

## [0.7.3] — 2026-05-21

### Fixed
- ISO disc rows showed the media_dir name (e.g. `Movies2`) instead of the movie-folder name in the scanner UI. v0.7.2 fixed `_disc_display_name` in the scanner/watcher write paths, but `file_name` is computed on-the-fly by `backend/routes/scan.py:_disc_aware_file_name` (the API enricher) and isn't stored in `scan_results`. The enricher used `parts[-3]` for all disc types, which for ISO inputs (`file_path` IS the .iso) points one level too high. Now branches on `.iso` suffix → uses `parts[-2]`.

### Known limitations
- **UDF-only Blu-ray ISO language metadata.** v0.7.1 added a bsdtar (libarchive) fallback for ISOs pycdlib can't open. Real-world testing showed libarchive 3.7.2 still can't list contents of some UDF 2.50/2.60 BD ISOs (returns empty). For those discs, language metadata falls back to `und` and the user manually selects tracks. The encoding path works fine via ffmpeg's `bluray:` protocol (libbluray reads these ISOs cleanly). v0.7.4 candidate: use `libbluray-bin` tools (`bd_info`, `mpls_dump`) which share the same libbluray that already works for encoding.

## [0.7.2] — 2026-05-21

### Fixed
- ISO disc rows showed the media_dir name (e.g. `Movies2`) instead of the movie-folder name in the scanner UI, and reported the **main-title bytes** (~19 GB) instead of the **ISO file's actual bytes** (~30 GB) for size. Two bugs from v0.7.0's ISO-folder handling: (1) `display_name` used `parent.parent.name` unconditionally, which for ISO inputs (where `file_path` IS the .iso file) points one level too high; (2) the `file_size` patch in `probe_file` only fired for folder discs via `_disc_total_size`, so ISO rows kept ffprobe's `format.size` which only counts the main title libbluray surfaces. New `_disc_display_name` helper handles both folder and ISO cases; `probe_file` now also stats the ISO file directly for accurate on-disk size.

## [0.7.1] — 2026-05-21

### Added
- **Language metadata for UDF-only Blu-ray ISOs.** Some BD ISOs ship without an ISO 9660 layer (BD-Video spec doesn't require one), which pycdlib rejects with "Valid ISO9660 filesystems must have at least one PVD". v0.7.1 adds `libarchive-tools` (`bsdtar`) to the image and a bsdtar-based fallback for sidecar extraction: when pycdlib can't open an ISO, the parsers enumerate `/BDMV/PLAYLIST/*.mpls` (or `/VIDEO_TS/VTS_NN_*.VOB`) via bsdtar, extract the relevant bytes, and feed them to the existing `_parse_bdmv_mpls_bytes` / `_parse_dvd_ifo_bytes` parsers. Real-world BD ISOs (UDF 2.50/2.60) now produce real language codes instead of `und`. DVD ISOs continue using the fast pycdlib path.

### Fixed
- Disc rows inherited stale `health_status='corrupt'` after re-discovery. When a previous conversion's post-source-handling deleted `VIDEO_TS/` or `BDMV/`, a concurrent health check would mark the row corrupt; restoring the source folder later caused the watcher to re-INSERT the row with the stale flag still attached. Scanner and watcher now explicitly clear `health_status` on successful disc re-probe (`disc_type IS NOT NULL` only — non-disc rows untouched, file-level health checks unaffected).

## [0.7.0] — 2026-05-21

### Added
- **DVD and Blu-ray ISO file support.** `.iso` files containing VIDEO_TS or BDMV structures are now first-class scan items alongside the v0.6.x unpacked folder support. ffmpeg reads the ISO directly via `-f dvdvideo -i /path.iso` (DVD) or `bluray:/path.iso` (BD) — no kernel mount, no extraction, no Dockerfile changes. Output MKV lands in the ISO's parent folder (or alongside the ISO when it's loose at a media_dir root), with the same scene-style naming + metadata-ID strip applied as folder discs. Post-conversion source-handling unlinks / trashes / backs-up the ISO file per the existing setting. Non-video ISOs (Linux installers, games) are silently ignored. **Limitation:** DVD ISO language metadata is fully extracted via pycdlib + IFO parser; BD ISO language metadata works for ISOs that carry an ISO 9660 + UDF layer but falls back to `und` for UDF-only BD ISOs (a known pycdlib limitation — Blu-Ray-spec doesn't require ISO 9660). The disc still converts correctly; the user just manually selects which audio/subtitle tracks to keep. Classification falls back to ffmpeg's `bluray:` protocol for UDF-only ISOs so they're correctly identified as BDs.

## [0.6.8] — 2026-05-21

### Fixed
- Disc conversion output filenames inherited the parent folder's metadata-ID tags — e.g. `Elephant (2003) [tt0363589] 1080p Bluray EAC3 5.1 h265.mkv` instead of the convention `Elephant (2003) 1080p Bluray EAC3 5.1 h265.mkv`. IDs belong on FOLDERS (for *arr cataloguing) but not duplicated into the file name. `build_disc_output_filename` now strips `[tt...]`, `[imdb-...]`, `[tmdb-...]`, `[tvdb-...]`, and `{tmdb-...}` / `{tvdb-...}` forms from the filename token while leaving the folder structure unchanged. Folders that don't carry an ID tag are unaffected.

## [0.6.7] — 2026-05-21

### Fixed
- Estimated savings shown in the file-detail panel (e.g. "Convert to x265 10-bit (est. save ~8.4 GB)") used a stale 0.30 reduction default while the queue-estimate modal used a CQ-calibrated empirical curve — producing wildly different numbers for the same file. Both now route through a single `backend/encoding_estimates` helper that applies the CQ curve calibrated against real NVENC results. Scan-time `estimated_savings_bytes` + new `video_conv_savings_bytes` column reflect the user's global CQ setting. Existing rows auto-backfill on first watcher cycle.

## [0.6.6] — 2026-05-20

### Fixed
- DVD IFO parser missed languages on discs where the audio/subp count byte was 0 but the attr entries were populated (a known authoring-tool quirk libdvdread has documented fallback for) — e.g. Fast-Walking (1982) surfaced its English track as `und`. Parser now falls back to scanning attrs for the first all-zero gap when the count byte is 0; positive counts are still trusted as-is.

## [0.6.5] — 2026-05-20

### Added
- **Disc track language detection.** Hand-rolled binary parsers for DVD `VTS_NN_0.IFO` and Blu-ray `.mpls` files now extract per-track language codes and patch them onto disc probe results before classify_audio_tracks runs. Discs no longer surface every audio/subtitle track as `und`; your existing `always_keep_languages` filter now selects correct tracks. Existing disc rows auto-backfill on first watcher cycle (idempotent via `settings.disc_lang_backfilled_v065`); the backfill runs the full classify pipeline so re-tagged rows carry the same schema as freshly-scanned ones. Parser failures fail open — disc still gets added with `und` tracks and a `[DISC-META]` warning logged.

## [0.6.4] — 2026-05-20

### Fixed
- Disc conversions reported as **failed despite succeeding**. After the encode landed and post-source-handling deleted the `VIDEO_TS/` (or `BDMV/`) subdirectory per the user's delete/trash setting, `rename_external_subtitles()` ran on the now-vanished parent and raised `FileNotFoundError`, which propagated up as a job failure. The encoded MKV was intact — only the job status was wrong. Helper now no-ops cleanly when the parent dir is missing (discs don't have sidecar subs anyway; their subtitles are internal PGS/VobSub streams). Also defends against any future race where the source disappears between encode and rename.

## [0.6.3] — 2026-05-20

### Fixed
- Disc rows sorted as "oldest" in date-based views because `os.path.getmtime` on the marker file (`VIDEO_TS.IFO` / `BDMV/index.bdmv`) returned the original DVD/BDMV authoring timestamp — often decades old — instead of when the user added the disc to their library. Both scanner and watcher now use the disc-root folder's mtime for `file_mtime` on disc rows, which reflects the actual "added to library" time. Regular file rows unchanged.

## [0.6.2] — 2026-05-20

### Fixed
- **DVD folder conversion (v0.6.0)** was non-functional: the design used a fictional `dvd:` ffmpeg input protocol that doesn't exist (the real libdvdread integration in ffmpeg is the `dvdvideo` demuxer, which only accepts ISO/block-device input, not folders). Every DVD probe failed with `Protocol not found`, the watcher silently shelved the path into an in-memory failure set, and no disc ever surfaced. v0.6.2 replaces it with ffmpeg's `concat:` protocol over the main-feature VOBs (identified by largest VTS_NN_1..N total size, excluding the VTS_NN_0 menu chunk). Verified end-to-end against a real disc (`Fast-Walking (1982)`, NTSC, 116 min, MPEG-2 + AC-3). Watcher now logs disc-probe failures loudly instead of dropping them on the floor. Blu-ray (`bluray:` protocol via libbluray) was already a real protocol and is unchanged.

## [0.6.1] — 2026-05-20

### Fixed
- Watcher not auto-discovering newly-dropped DVD `VIDEO_TS/` or Blu-ray `BDMV/` folders. The polling walk filtered files by video extension before the v0.6.0 disc-marker pre-pass could see them, so `.IFO` / `.VOB` / `.m2ts` were dropped at the walk stage and disc folders never registered. Watcher walk now classifies each directory for disc structure first (matching the scanner), records the marker file, and prunes recursion into the disc subdirectory. Manual rescan was unaffected.

## [0.6.0] — 2026-05-20

### Added
- **DVD and Blu-ray folder support.** Raw `VIDEO_TS/` and `BDMV/` folder structures are now scannable items: Shrinkerr detects them automatically, picks the longest title via ffmpeg's `dvd:/` / `bluray:/` protocols (libdvdread/libdvdnav/libbluray, already in the image), and transcodes that title to HEVC. Output is a scene-style MKV in the disc's parent folder; the original `VIDEO_TS/` or `BDMV/` subdirectory follows your existing post-conversion source-handling setting (delete / trash / backup). Combo discs with both VIDEO_TS and BDMV convert as Blu-ray. Main-feature only — extras and menus discarded.

## [0.5.26] — 2026-05-20

### Fixed
- Conversion failing on **10-bit H.264 sources (Hi10p, `yuv420p10le`)** with `ffmpeg exited with code 218` and "Impossible to convert between the formats supported by the filter 'graph -1 input from stream 0:0' and the filter 'auto_scale_0'". Pascal NVDEC (and older) don't support 10-bit H.264; QSV decode is 8-bit-only for H.264; most VAAPI drivers don't either. Pre-v0.5.26 our codec gate only checked the codec name (`h264` ∈ supported set → True), so the cmd builder emitted `-hwaccel cuda -hwaccel_output_format cuda` and a `scale_cuda=format=p010le` filter. ffmpeg then tried NVDEC, silently fell back to software decode (good), but the now-CPU frames couldn't bridge to the still-present CUDA filter (bad). Extended `hw_decode_supports()` to also check `source_pix_fmt`: any H.264 source with a `p10` / `10le` / `p12` / `12le` pix_fmt now takes the pure-software-decode path on all three HW backends. 10-bit HEVC is unaffected (Pascal+ NVDEC, Gen11+ QSV, and most VAAPI drivers handle it fine). Worker log now also reports the source pix_fmt alongside the codec when HW decode is skipped.

## [0.5.25] — 2026-05-19

### Fixed
- Dashboard "Source Types" pie and "Avg Reduction by Source" under-counted Remux jobs (showed only 1 even with many remuxes converted). Two compounding causes: (1) `_source_type()` checked `BluRay` before `Remux`, so scene-named files like `…1080p.BluRay.Remux.AVC…` always bucketed as Blu-ray; (2) v0.5.5's `rename_source_to_target_codec` strips the `Remux` tag post-conversion, so even with a fixed order the current `file_path` no longer carried the marker. Now categorizes Remux first, and reads the source-type from `original_file_path` when available (falling back to `file_path` for legacy jobs).

## [0.5.24] — 2026-05-19

### Fixed
- Audit pass for the v0.5.23 silent-fail pattern (unchunked SQL `IN (…)` / OR'd `LIKE` clauses): chunked the remaining at-risk sites that could exceed SQLite's variable / expression-depth limits with 1000+ items. Fixed: `queue_health_checks` folder→file expansion (same bug as v0.5.23 in the Scanner's bulk Health Check button), `rule_resolver.resolve_rules_for_batch` (called from estimate/queue add with the full user selection), `bulk job priority update`, `batch rename probe load`, `poster cache batch lookup`, watcher's `stale_paths` DELETE. Behaviour unchanged for typical selections; only adds resilience at scale.

## [0.5.23] — 2026-05-19

### Fixed
- Scanner's "Select all + Add to queue" with 1000+ folders silently returned `0 Files to process` in the estimate modal. The folder→file expansion built one OR'd LIKE clause per folder in a single SQL query — with ~1000+ folders the expression depth blew past SQLite's `SQLITE_LIMIT_EXPR_DEPTH` default of 1000 and the SELECT returned no rows. Chunked the expansion to 800 folders per query in both the estimate and add-from-scan paths.

## [0.5.22] — 2026-05-19

### Fixed
- Watcher's auto-queue path used the pre-v0.5.4 `is_x264()` codec check — only H.264 was treated as "needs conversion", so MPEG-2 / MPEG-4 / VC-1 / WMV files auto-discovered via filesystem watching never got a `convert` job even when those codecs were in the user's source_codecs list. Now uses `codec_matches_source(video_codec, source_codecs)` like the scanner and webhook paths. HEVC files were unaffected (correctly skipped either way).

## [0.5.21] — 2026-05-18

### Fixed
- "Merge external subtitles into video" and "Delete external subtitle files after merging" toggles weren't persisting — both fields existed in the UI but had zero wiring through `models.py` / `_ENCODING_DEFAULTS` / GET / PUT (same shape as the `reorder_native_audio` bug from issue #11). The worker's `_is_cleanup_enabled` missing-row fallback defaulted True, so external-sub merging happened invisibly even when the UI showed the toggles off. Wired both fields end-to-end, defaulted both off, and switched the worker's reads to `default=False` so unset DB rows match the UI's rendering. **Behaviour note**: existing installs that never explicitly toggled these were getting silent merging — after upgrade, merging stops unless the user opts in.

## [0.5.20] — 2026-05-18

### Fixed
- Subtitle tracks in the file's native language were kept by default even when they weren't in "Keep Subtitle Languages" — German subs on a German-audio movie etc. Pre-v0.5.20 audio and subs shared the `keep_native_language` rule, which makes sense for audio (keep the original audio track) but is rarely useful for subs. Split into a separate **"Auto-keep native language subtitle tracks"** toggle (Settings → Subtitles), default **off**. Existing users who relied on the old behaviour (SDH on same-language audio) can opt back in.
- Existing audio toggle label clarified from "Auto-keep native language tracks" to "Auto-keep native language **audio** tracks" — same setting, just no longer ambiguous about which media type it controls.

## [0.5.19] — 2026-05-18

### Fixed
- Conversion failing with `ffmpeg exited with code 234` / `Subtitle encoding currently only possible from text to text or bitmap to bitmap` on files with a multi-stream external sidecar subtitle (notably VobSub `.idx`/`.sub` pairs carrying multiple language streams in one file). The cmd builder emitted `-map 1:s` to pull every sub stream from each external input but only set a codec for the first output sub-stream index per file — ffmpeg picked its matroska default (`ass`/`ssa`, a text codec) for the remaining streams and tried to encode dvdsub bitmap → ass text. Added a catch-all `-c:s copy` after the per-stream specs so any unset external sub stream defaults to byte-copy; the per-stream specifiers above still win for the streams they name (webvtt → srt path preserved).

## [0.5.18] — 2026-05-18

### Fixed
- Disc-tier source tags now get normalized in the post-conversion filename: `BR-DISK` / `BD25` / `BD50` / `BD100` → `Bluray`, `DVD-R` / `DVDR` / `DVD5` / `DVD9` → `DVDRip`. The re-encoded file is no longer a disc rip, so the tag should reflect what the file actually is. Already-encoded forms (`Bluray`, `DVDRip`, `WEB-DL`, `HDTV` etc.) are left alone, as are unrelated tokens (`DVD-RW`, `DVD-RAM`). Scanner sibling-detection updated to match the new naming chain so disc-tier originals get skip-flagged when their re-encoded sibling already exists.

## [0.5.17] — 2026-05-18

### Added
- "Keep only the best track per always-keep language" toggle in Settings → Audio. Default on (preserves v0.5.16's smart-selection behaviour); turn off to keep every track in always-keep languages, pre-v0.5.16 style. Per-track overrides via the file editor still work in either mode.

## [0.5.16] — 2026-05-18

### Changed
- "Always Keep Languages" no longer locks every track in those languages. When a file has multiple tracks in the same always-keep language (e.g. 3 English tracks: EAC3 5.1, AAC 2.0, AAC commentary), only the highest-quality one — ranked by channels desc then codec quality (TrueHD > FLAC/PCM > DTS-HD MA > DTS > EAC3 > AC3 > AAC) — is kept by default; the others fall through to the standard rules. All tracks render as editable checkboxes so users can override per-track, even on always-keep languages. **Behaviour change for users with multi-track libraries**: re-scanning will mark redundant same-language tracks for removal. Issue #11.

## [0.5.15] — 2026-05-18

### Fixed
- "Reorder native language to first audio stream" toggle was entirely cosmetic — the field had no model definition, no DB default, no GET wiring, and no PUT branch, so saves were silently dropped and the worker's missing-row fallback always returned True. Now fully wired through the standard model → DEFAULTS → GET → PUT → cache-invalidation pipeline (issue #11). Default preserved as on; users who want the native track left in place can now actually turn it off.
- `keep_native_language` save did not invalidate the worker's in-memory cache, so changes only took effect after a container restart. Added to the cache-invalidation set alongside `reorder_native_audio`.

## [0.5.14] — 2026-05-14

### Fixed
- NVENC + NVDEC jobs failing with `CUDA_ERROR_INVALID_VALUE` on x264 sources with many reference frames. NVDEC's 32-surface hardware limit was exceeded because the H.264 decoder's thread count (defaulting to `nproc`) drove surface allocation past 32. Pin decoder threads to 1 whenever NVDEC is on — encoder threads still respect the `FFmpeg Threads Per Job` setting.

## [0.5.13] — 2026-05-14

### Changed
- Image hardening pass: `apt-get upgrade` at build time, NVENC base bumped from CUDA 12.3.1 / Ubuntu 22.04 to CUDA 12.6.3 / Ubuntu 24.04 (and Python 3.12). Same NVIDIA driver floor (525.60.13+). Knocks Docker Scout's High/Medium CVE counts down materially without changing runtime behaviour.

### Added
- `pip-audit` CI workflow scans `requirements.txt` weekly + on PRs that touch it. Informational (non-blocking) — surfaces vulnerable Python deps in the Actions tab.

## [0.5.12] — 2026-05-14

### Fixed
- "Saved by Library" no longer shows two rows for case-differing historical paths (e.g. `downloads` + `Downloads` from a directory rename). Fallback labels are normalized to start with a capital when entirely lowercase; mixed-case names (M2T2, TV1, user-set labels) untouched.

## [0.5.11] — 2026-05-14

### Fixed
- Dashboard "Saved by Library" now uses `MediaDir.label` (set via Settings) and falls back to the same `/media/<X>/...` volume-name logic the disk-space card uses — labels now match between the two cards. **Side effect**: nested MediaDirs (e.g. `/media/Misc/tv`) without an explicit label merge into the parent volume's row.

## [0.5.10] — 2026-05-11

### Fixed
- NVENC+NVDEC: drop `-pix_fmt` from encoder — `scale_cuda=format=X` already sets the surface format; leaving `-pix_fmt` made ffmpeg try and fail to bridge CUDA→CPU.

## [0.5.9] — 2026-05-11

### Added
- NVENC bit-depth dropdown (10-bit / 8-bit / Match source). 8-bit unblocks Maxwell cards; Match source picks per-job from probed pix_fmt.

### Fixed
- `-threads N` emitted post-encoder too — pre-input only capped decoder threads, leaving libx265 encode uncapped.

## [0.5.8] — 2026-05-11

### Fixed
- NVENC+NVDEC jobs failed on first encode. Added `scale_cuda=format=p010le` to convert nv12 CUDA surfaces to 10-bit on-GPU.

## [0.5.7] — 2026-05-11

### Added
- Hardware decode (NVDEC / QSV / VAAPI) paired with each encoder, plus opt-in libx265+NVDEC mixed mode. Native pairs default on; unsupported codecs fall back silently to software.
- Capability probe via `ffmpeg -hwaccels`; each toggle gated on matching encoder + backend availability.

### Changed
- VMAF is skipped on hardware-decoded jobs (needs software-decoded source frames). Surfaced via warning chip, per-toggle help text, and worker log.

### Fixed
- VAAPI native-pair filter chain no longer emits redundant `format=nv12,hwupload`.
- Remote workers now receive the 4 HW decode settings via the job payload.

## [0.5.6] — 2026-05-11

### Added
- `FFmpeg Threads Per Job` Settings slider (caps `-threads N`). Default `0` = ffmpeg auto.

### Changed
- Auth UX: Enable toggle moved to left side as a labelled chip, username/password fields always rendered (disabled when off), green active-auth banner.
- README troubleshooting entry with `sqlite3` one-liners to recover from auth lockout without nuking the data volume.

## [0.5.5] — 2026-05-11

### Fixed
- Filename rename after conversion now covers all non-h264 sources (MPEG-2, MPEG-4, XviD, DivX, VC-1, WMV, VP9). Was only matching x264/h264/AVC.

## [0.5.4] — 2026-05-11

### Fixed
- `scan_results.needs_conversion` recomputes when `source_codecs` setting changes (PUT handler + one-shot heal on upgrade). Existing rows scanned under a narrower source list were stuck at `needs_conversion=0`.

## [0.5.3] — 2026-05-11

### Fixed
- Date Added rule condition now appears in the rule-builder dropdown (v0.5.1 wired the backend but missed the hardcoded `<option>` list).

## [0.5.2] — 2026-05-11

### Fixed
- HEVC→HEVC audio-only re-encodes no longer leave files showing as NEW. Audio-rename path now sets `converted=1, is_new=0, new_detected_at=NULL`; one-shot heal repairs already-broken rows.

## [0.5.1] — 2026-05-08

### Added
- `date_added` rule condition (newer than / older than, hours/days/weeks). Sourced from `scan_results.new_detected_at`.

## [0.5.0] — 2026-05-08

### Added
- Auto-queue priority Settings dropdown (Normal / High / Highest). New files inherit this priority when auto-queued.

### Fixed
- `encoding_rules` now apply to auto-queued files (previously bypassed rule engine). **Behavior change**: rules you wrote thinking they only applied to manual queueing now apply to auto-queue too.

## [0.4.9] — 2026-05-08

### Fixed
- **Conversion failing with `Invalid UTF-8 in decoded subtitles text`** on whole series with non-English / older releases. Root cause: when `merge_external_subs` was enabled and the folder had a sidecar `.srt` file, the converter mapped it as a separate ffmpeg input AND used `-c:s srt` to re-encode it — forcing a decode→encode roundtrip through ffmpeg's strict UTF-8 SRT encoder. SRT files in the wild are frequently Windows-1252 / ISO-8859-1, which fail the UTF-8 validator and abort the entire encode with exit code 69 (`Conversion failed!`). Fix: byte-copy SRT/ASS/SSA external subs (`-c:s copy`) instead of re-encoding. Both `backend/converter.py` and `backend/audio.py` had the same bug; both fixed. Most MKV players handle non-UTF-8 SRT via charset auto-detection, so the byte-copy result is more compatible than what the re-encode would have produced anyway.
- **Failed jobs not showing the ffmpeg command + log in the new collapsible sections** that v0.4.8 added. Backend bug: the converter's failure return paths only included `error` (the short message) — `ffmpeg_command` and `ffmpeg_log` weren't included on failure, so the worker never wrote them to the DB. Frontend dutifully looked for fields that were always null. Fix: failure returns now include both fields, and the worker calls `update_conversion_log` on failure (with `encoding_stats=None`) parallel to the success path.

## [0.4.8] — 2026-05-08

### Added
- **Failed-job expand now shows the ffmpeg command and full ffmpeg log.** Previously expanding a failed job in the Completed tab only revealed a short `error_log` snippet — useful for "file not found" but useless for diagnosing real ffmpeg failures (codec rejections, mux errors, etc.). The expanded view now lazy-loads the same `getJobLog` payload completed jobs use and renders both the ffmpeg command and the full stderr log behind collapsible `<details>` toggles.

### Fixed
- **Real ffmpeg error survives the rolling buffer.** The converter's failure path used to take the last 10 non-progress lines from a 20-line rolling buffer to populate `error_log`. For MKVs with heavy stream metadata (e.g. 5+ subtitle streams each carrying a block of `_STATISTICS_*` tags), those 20 lines could be entirely metadata, pushing the actual error message out before it was captured. Now the converter maintains a sticky `error_lines` list that retains any line matching ffmpeg error patterns (`[error]`, `Error `, `Failed`, `Could not`, `Invalid `, etc.) as it's emitted, capped at 50 lines. The failure path prefers these matched lines over the rolling buffer when populating `error_log` — the actual error is preserved no matter how much metadata precedes it.

## [0.4.7] — 2026-05-08

### Added
- **Audio conversion details in the Completed-tab job report.** The expanded view now shows e.g. `Audio: DTS-HD MA → EAC3 640kb` whenever Shrinkerr re-encoded one or more audio tracks during conversion. Triggered by either the lossless auto-conversion path (TrueHD / DTS-HD MA / FLAC / PCM tracks → user-selected lossy codec) or the global `audio_codec != "copy"` setting. Sources are deduped + sorted; multiple distinct source codecs render as `TrueHD + DTS-HD MA → EAC3 640kb`.

### Fixed
- **Seasons now sort numerically in the Scanner file tree and file list.** Previously "Season 11" sorted before "Season 2" because of lexicographic string compare. Switched all four `localeCompare` sites (FileTree node sort, FileTree file sort, PosterGrid title sort, PosterGrid section sort) to use `numeric: true`, which makes embedded digits compare as numbers. Affects any name with a numeric suffix (Season N, Disc N, Part N, etc.).

## [0.4.6] — 2026-05-07

### Added
- **Queue page now shows a banner when stream-pause is active.** Previously the only visible signal that v0.4.5's SIGSTOP had taken effect was the progress bars freezing — the manual Pause/Start button stayed unchanged because manual pause and stream-aware pause are independent states. The banner reads "Encoding paused — active stream(s) on \<Plex / Jellyfin / Emby\>" with a count of frozen jobs, and disappears the moment the stream ends and SIGCONT fires. Surfaced via a new `stream_pause` field on `GET /api/jobs/stats`.

## [0.4.5] — 2026-05-07

### Changed
- **Pause-on-stream now actually pauses running encodes** (Plex / Jellyfin / Emby). Previously the worker only delayed *new* jobs from starting while a stream was active; currently-running ffmpeg processes were left untouched. Now the worker sends SIGSTOP to every active ffmpeg subprocess when a stream starts, freezing them at zero CPU / zero disk-IO. SIGCONT on stream-end resumes from the exact frame they stopped at — no work lost. The pause-check loop runs every 15 seconds regardless of `len(_active_tasks)`, so the freeze takes effect within one cycle of a stream starting.
- **Default for `*_pause_transcode_only` flipped from `true` to `false`** for all three media servers. Under the new SIGSTOP behavior, even direct-play streams mean "user is watching", so pausing is appropriate; the previous "transcoding only" default was a holdover from when pause-on-stream just delayed dispatch and direct-play didn't compete for transcode CPU. Existing users with this setting stored in DB are unaffected; the new default only applies to fresh installs and freshly-toggled fields.
- Updated Schedule page UI copy to reflect the real-pause behavior (mentions SIGSTOP/SIGCONT explicitly so users know it's lossless).

## [0.4.4] — 2026-05-07

### Fixed
- **Jellyfin/Emby Stream-Aware Scheduling toggles didn't persist visually.** Save toast appeared, DB write succeeded, but reloading the page showed the toggles disabled again. Root cause: the `GET /api/settings/encoding` response builder didn't include `*_pause_on_stream`, `*_pause_stream_threshold`, or `*_pause_transcode_only` for Jellyfin or Emby — only Plex's pause keys were assigned. The frontend had no value to read back, so React initialised the toggles from `undefined` and rendered them as off. v0.4.2 fixed the PUT side; v0.4.4 fixes the GET side. The `test_jellyfin_settings_round_trip` and `test_emby_settings_round_trip` tests in `test_routes.py` now assert all three pause keys round-trip correctly (would have caught this earlier).

## [0.4.3] — 2026-05-07

### Added
- **Pause-on-stream UI for Jellyfin and Emby.** The backend keys (`jellyfin_pause_on_stream`, `jellyfin_pause_stream_threshold`, `jellyfin_pause_transcode_only`, plus the Emby trio) have existed for a while but were unreachable from the Settings UI — only Plex had visible controls. The "Plex / Jellyfin / Emby Stream-Aware Scheduling" panel on the Schedule page now has three sub-sections, one per server, each with the same enable / threshold / transcode-only controls. One "Save Streaming Settings" button persists all nine keys.

### Fixed
- **`*_watched` rule conditions always returned False.** All three media-server `sync_*_metadata_cache()` functions write `watch_status` rows into the shared `plex_metadata_cache` table, but the `plex_watched`, `jellyfin_watched`, and `emby_watched` rule resolvers were stubs that ignored the cache and returned `False` unconditionally. Rules using "Watched" as a condition silently never matched. Fix: collapse all three resolvers into one shared implementation that reads the `watch_status` rows. Folders without a cached watch_status (sync hasn't run, or folder isn't in any watched library) still return False to avoid spurious matches.
- **Resume notification only fired for Plex.** When a long-running stream ended, the worker logged `[WORKER] Resuming — Plex streams ended` if it was Plex's pause flag set, but the `_jellyfin_pause_logged` and `_emby_pause_logged` flags got stuck on `True` forever — the resume log never fired and re-pausing on a subsequent stream wouldn't print "Pausing" again because the flag was still set. Fix: extend the resume branch to clear all three flags and emit a per-server resume log line.

## [0.4.2] — 2026-05-07

### Fixed
- **Test Connection always failed with "URL and API key required"** for Jellyfin (and inherited by v0.4.0 Emby). Root cause: the `PUT /api/settings/encoding` save handler only persisted `jellyfin_url` / `emby_url` to the DB. The other 8 fields per server (`*_api_key`, `*_user_id`, `*_path_mapping`, `*_scan_after_conversion`, `*_empty_trash`, `*_pause_on_stream`, `*_pause_stream_threshold`, `*_pause_transcode_only`) were silently dropped by the handler — fields existed on the model and showed up in `_ENCODING_DEFAULTS` and the GET response, but `update_encoding_settings` never wrote them. Pre-existing for Jellyfin since it was added; Emby inherited the same gap. Effect: `_get_*_settings()` always returned an empty `*_api_key`, the connection test bailed early with "URL and API key required". Fix: 16 new persist branches in `update_encoding_settings` (8 jellyfin + 8 emby), with `****` masking-roundtrip guards on the api_key fields. Added `test_jellyfin_settings_round_trip` and `test_emby_settings_round_trip` to lock the behavior in.

## [0.4.1] — 2026-05-07

### Fixed
- `parse_ffmpeg_progress` returned `None` (instead of a progress dict with `fps` populated) when ffmpeg emitted a progress line but the source file's duration couldn't be determined. Effect: corrupt or in-progress mkv files showed no fps readout updates during conversion — the worker's progress callback went silent until the encode finished. Fix: when neither time-based nor frame-based progress ratio is computable but the line carries an `fps=` field, return `{"progress": 0.0, "fps": <parsed>, "eta_seconds": None}` so the UI keeps animating. Test in `backend/tests/test_converter.py` for this case (pre-existing, never green) now passes.

## [0.4.0] — 2026-05-07

### Added
- **Emby integration** at full feature parity with Jellyfin: library refresh after each conversion, active-stream detection (pause encoding when someone's watching), watched-status sync for queue prioritization, rule-engine inputs (Emby tag + watched-status condition types; genre/library shared with the existing Plex pipeline), bulk metadata sync (`POST /api/rules/sync-emby`), path mapping, connection test. New Emby section in Settings → Integrations. Nine new settings keys (`emby_url`, `emby_api_key`, `emby_user_id`, `emby_path_mapping`, `emby_scan_after_conversion`, `emby_empty_trash`, `emby_pause_on_stream`, `emby_pause_stream_threshold`, `emby_pause_transcode_only`). New `backend/emby.py` mirrors `backend/jellyfin.py` 1:1; Emby's HTTP API is ~90% identical to Jellyfin's so the integration shares the `Authorization: MediaBrowser Token=...` header and the same `/Library/Refresh`, `/Sessions`, `/Library/VirtualFolders`, `/Users` endpoints. Emby URL is added to the SSRF allowlist and (drive-by fix) so is Jellyfin's, which was previously unprotected. Synthetic API-shape unit tests for `backend/emby.py` (`backend/tests/test_emby.py`).

## [0.3.138] — 2026-05-07

### Removed
- "Re-resolve cached posters" Settings section (both buttons), the `POST /api/posters/re-resolve` endpoint, and the `reResolvePosters` API export. The "Re-resolve all auto-matched" path was redundant with the type-impossible cache invalidation (v0.3.111+) and the per-release one-shot purge migrations (v0.3.116+); the "Re-resolve placeholders" path is replaced by an automatic 7-day TTL on `source='placeholder'` rows in `resolve_posters` — stale placeholders re-resolve on next read.

## [0.3.137] — 2026-05-07

### Fixed
- Newly-converted Shrinkerr files (Coroner, Conviction, Conversations with Friends, Cook at all Costs, …) showing as NEW with `converted=0` despite successful conversion jobs. **Root cause: the worker's late post-conversion scan_results UPDATE was destroying the early site's correct work.** Both sites use a v0.3.130 DELETE+UPDATE pattern; the early site (status='running') correctly renames h264 row → h265 with converted=1. The late site (status='completed', after `update_status`) then DELETEs the just-renamed row at h265, then UPDATEs WHERE file_path=h264 (matches 0 rows because already renamed). Net: scan_results loses the row entirely, the next watcher poll re-INSERTs it with `is_new=1, converted=0, new_detected_at=now`. v0.3.130 introduced this regression when it added watcher-race protection to both sites; pre-v0.3.130 the late site was just a no-op rename. Fix: late site now checks if a row at `current_file_path` already has `converted=1` and skips the destructive DELETE+UPDATE if so. Plus a one-shot heal that retroactively fixes all rows wiped by this bug.

## [0.3.136] — 2026-05-06

### Fixed
- Newly-converted items still showing the NEW badge despite v0.3.132/.134/.135. Root cause: the Scanner UI computes `is_new` from the `new_detected_at` timestamp (`scan.py:1221`), not from the `is_new` column the prior heals were clearing — the `is_new` column turned out to be vestigial UI-side. For Sonarr→Shrinkerr pipelines completing within 24h of the original h264 drop, `new_detected_at` was preserved through the worker's rename UPDATE and the badge stayed lit. Fix: also set `new_detected_at = NULL` in all three worker post-conversion UPDATE sites, plus a one-shot startup heal that clears `new_detected_at` on every row already flagged `converted=1`.

## [0.3.135] — 2026-05-06

### Fixed
- One-shot re-run of the v0.3.132 post-conversion heal under a new flag. Conversions completed between the v0.3.132 deploy and the v0.3.134 deploy still had `is_new=1` (watcher race was fixed but the worker still preserved is_new on rename). The v0.3.132 heal flag was already set, so the migration wouldn't re-trigger on its own. v0.3.135's flag heals those in-between rows on first boot.

## [0.3.134] — 2026-05-06

### Fixed
- Newly-converted items still showing in the new-files list (Conviction, Carmen Curlers, Conversations with Friends). v0.3.132 fixed the watcher race that was causing `converted=0` on the post-conversion row, but the worker's UPDATE preserved the original row's `is_new=1` (set by the watcher when Sonarr first dropped the h264 file). Sonarr→Shrinkerr pipelines therefore landed at `is_new=1, converted=1` instead of `is_new=0, converted=1`. Fix: add `is_new = 0` to all three worker scan_results UPDATE sites (early post-rename UPDATE, late post-rename UPDATE, in-place UPDATE for jobs without a rename). The v0.3.132 startup heal already cleared this on existing rows; v0.3.134 prevents the next conversion from re-creating the symptom.

## [0.3.133] — 2026-05-06

### Fixed
- TV folders with `[tvdb-N]` getting wrong-type matches from Plex (`Charlie's Angels (1976) [tvdb-77170]` → 2000 movie poster) or stuck at `media_type=None` (Chase, Chicago Hope). Plex was the FIRST resolver in the chain; once it returned anything, `source != "placeholder"` and the authoritative TVDB-find never ran. Plex's global title-only fallback was happily matching wrong-type/untyped entries for ambiguous show names. Fix: reject Plex's answer when it disagrees with the bracket family (TV folder + Plex says movie/None → fall through to TVDB-find). Plus extend the type-impossible cache invalidation to catch `media_type=None` on `[tvdb-N]` folders so existing stuck rows re-resolve.

## [0.3.132] — 2026-05-06

### Fixed
- Newly-converted files showing as "new" with `converted=0`. Watcher's stale-row cleanup deleted the original h264 scan_results row before the worker's post-rename UPDATE could land — UPDATE then matched 0 rows, watcher re-INSERTed at the h265 path with `is_new=1`. Fix: skip stale-deletion for paths with a pending/running job (mirrors scan.py's orphan-cleanup guard). Plus a one-shot startup heal that retroactively sets `converted=1, is_new=0` on existing rows whose file_path matches a successful convert/combined job.

## [0.3.131] — 2026-05-06

### Fixed
- `/api/posters/resolve` returning 500 with `NameError: name 'has_explicit_id' is not defined`. Pre-existing reference inside `_backfill_one` to a variable that was never defined in that scope — the dir-label fallback and TVDB-implies-TV branch both used it. Threw on every resolve call that hit media_type backfill (i.e. any folder with `media_type` still NULL), which was a lot of TV1's filtered subset. This was the actual cause of "TV1 posters don't load" — not the cache state, not network, not rate limits. The endpoint just couldn't return.

## [0.3.130] — 2026-05-05

### Fixed
- `[WORKER] ... scan_results update failed (non-fatal): UNIQUE constraint failed: scan_results.file_path` after a conversion finishes. Race with the file-watcher: the watcher tick that runs in parallel with the worker can spot the post-rename file appear on disk and insert a fresh `scan_results` row at the new path before the worker finalises. The worker's UPDATE then trips the UNIQUE constraint and the `converted=1`/`needs_conversion=0` flags never land — the file shows up as still-needs-conversion in the Scanner. Both update sites now delete any conflicting row at the new path first; worker's just-probed post-conversion state is authoritative anyway.

## [0.3.129] — 2026-05-05

### Fixed
- Sonarr/Radarr rescans intermittently failing with `ReadTimeout`. The flat 15 s timeout covered both connect and read, but the slow operation is the initial `GET /api/v3/movie` (or `/api/v3/series`) listing — large libraries on modest hardware easily take 20-40 s to serialise. Switched to a structured `httpx.Timeout(connect=5, read=60, ...)` so connect stays short (don't wait on an unreachable host) but the listing has room to complete. The list is cached for 5 minutes anyway, so the slow path only fires once per cache window.

## [0.3.128] — 2026-05-05

### Fixed
- Reverted v0.3.121's "skip backend image fetch for TMDB CDN URLs" optimisation. It traded a one-time download cost on resolve for a per-page-load browser fetch — cache reads stopped being instant because the browser had to fetch from `image.tmdb.org` every time. On filtered Scanner views with hundreds of cards, the browser's per-host connection limit + TMDB CDN variability made posters trickle in or never load. Backend now downloads + base64-caches every poster on resolve so cache reads render instantly, the way they did pre-v0.3.121.
- Lazy backfill for URL-only cache rows written during v0.3.121-127: on cache read, any row with `source` of `tmdb`/`tmdb-manual` whose `poster_url` is still a CDN URL (not a `data:` URI) gets its image downloaded inline (bounded concurrency 8) and the cache row updated. One-time-per-row hit; subsequent reads are instant.

## [0.3.127] — 2026-05-05

### Fixed
- Manual Fix-match overrides are now NEVER auto-invalidated. Pre-v0.3.127, both the v0.3.116 mass `[tvdb-N]` purge migration AND the type-impossible cache invalidation in `resolve_posters` deleted rows regardless of source — so user-set `tmdb-manual` rows got nuked alongside broken auto-resolved ones, and the next resolve wrote a (potentially wrong) auto match back. Symptom: "I manually fixed Rush Hour and it un-fixed itself." Both code paths now skip rows with `source='tmdb-manual'`. (Already-deleted manual fixes from v0.3.116's run can't be recovered — those need to be redone once.)
- `[RADARR] Rescan failed:` and `[SONARR] Rescan failed:` log lines could end with an empty error message when `str(exc)` was empty for the exception type. Now falls back to the exception class name so the line is always informative.

## [0.3.126] — 2026-05-05

### Fixed
- Posters silently failing to load on filtered Scanner views: TMDB resolver helpers (`_resolve_tmdb`, `_resolve_tmdb_tvdb`, `_resolve_tmdb_search`) treated any non-200 response as "no match" and returned a placeholder, including for 429 rate-limit responses. The frontend then re-queued the same paths on every render, hammering TMDB and getting 429'd again — visible posters stuck on placeholder forever. New `_tmdb_get` wrapper honours the `Retry-After` header (capped at 5 s) and retries up to 2 times on 429. Most likely to bite users on the bundled `SHRINKERR_TMDB_API_KEY` (shared across deployments → trips per-key rate limits more easily); setting your own TMDB key in Settings → Connections still recommended for heavy use.

## [0.3.125] — 2026-05-05

### Fixed
- "Audio removed" row in job reports showed only the track count, not languages — while "Subs removed" showed both. v0.3.123's combined-job code referenced `raw_tracks` which is only defined inside the audio-only branch; for combined jobs the variable wasn't yet in scope and the audio language list came back empty. Both paths now pull from `probe.get("audio_tracks", [])` for parity.

## [0.3.124] — 2026-05-05

### Fixed
- "Lossless → EAC3" badge fired on files where the lossless track was being *removed* — the file-level `has_lossless_audio` flag didn't know about the job's removal list, so a Bluray rip with AC3 + TrueHD where the TrueHD was the one being dropped still showed the transcode badge even though no transcode would happen. `/scan/tracks-by-path` now stamps `is_lossless` on each individual track; the queue page checks whether any *kept* track is lossless before showing the badge.

## [0.3.123] — 2026-05-05

### Added
- Combined jobs (video re-encode + inline audio/sub cleanup) now record track-removal info on `encoding_stats` and surface it in the Completed-tab expanded view: "Audio removed: N (langs)" / "Subs removed: N (langs)". v0.3.117 added these for audio-only jobs; this brings parity for combined. Frontend gate widened from `job_type === "audio"` to "presence of removal fields", so any job that performed cleanup surfaces it.

## [0.3.122] — 2026-05-05

### Fixed
- Add-to-Queue modal showed CQ 20 instead of the user's configured `nvenc_cq` (e.g., 27) on most files. Content-type detection (default-on) was classifying any non-anime/grain/animation/remux release as `"default"` and overriding the user's global with content-detect's hardcoded `default` profile (CQ 20 at 1080p). Fix: when content-detect classifies as `"default"` (no specific signal), fall through to the user's global CQ — specific classifications still use their tuned values, which is what content-detect is meant for.

## [0.3.121] — 2026-05-05

### Performance
- Posters trickling in over ~1 minute after a filter change: each uncached folder triggered a backend `_download_image` that fetched the TMDB CDN poster server-side and base64-encoded it into the response. For a few hundred uncached folders (common after v0.3.116's mass cache invalidation) this serialised everything behind a slow fetch+encode step. Fix: skip the backend fetch for TMDB CDN URLs (they're publicly accessible — the browser fetches them directly in parallel) and bump resolve concurrency 8→16. Plex posters still backend-proxy because they need the auth token attached.

## [0.3.120] — 2026-05-05

### Fixed
- Worker-capability detection (Nodes page pills, job routing) was advertising QSV and VAAPI on NVIDIA-only hosts because it just checked for any `/dev/dri/renderD*` entry — NVIDIA's `renderD128` exists but Intel/AMD encoders can't use it. Settings' encoder-caps endpoint already vendor-matched render nodes via sysfs (v0.3.90+), so the two paths disagreed. Worker detection now reuses the same `_intel_render_node()` / `_vaapi_render_node()` helpers from `encoder_caps.py`.

## [0.3.119] — 2026-05-05

### Fixed
- Worker Nodes capability pills used a binary `cap === "nvenc"` check, so a host with libx265 + QSV + VAAPI + NVENC showed four pills with three identical "CPU (x265)" labels. Now each encoder gets its own pill: `libx265 (CPU)`, `QSV (Intel)`, `VAAPI (GPU)`, `NVENC (GPU)`.

## [0.3.118] — 2026-05-05

### Fixed
- Monitor page contradicted itself when NVENC was detected via test-encode but `nvidia-smi` was unavailable (e.g., NVIDIA Container Toolkit running without the `utility` capability): the prose said "No NVIDIA GPU detected on this host — Shrinkerr will use CPU encoding (libx265)" while the chip below showed ✓ NVENC. The Encoding Capability card now branches on the local node's capabilities and explains the asymmetric state — NVENC available, GPU stats unavailable, with the toolkit-capability fix as the likely cause.

## [0.3.117] — 2026-05-05

### Added
- Audio-only cleanup jobs now produce a populated job report. `remux_audio` returns the ffmpeg command, log tail, and size/bitrate stats; the worker augments `encoding_stats` with the per-track removal info (counts + languages); the Completed-tab expanded view renders an audio-cleanup-flavoured stats block (drops codec/bitrate rows, adds "Audio removed" / "Subs removed" with language breakdown). Pre-v0.3.117 these jobs landed in the DB with empty conversion logs, so the v0.3.115 follow-up audio job spawned after a discarded combined encode showed up in the queue but had no visible report.

## [0.3.116] — 2026-05-05

### Fixed
- One-shot mass invalidation of every cached `[tvdb-N]` poster row on first read after upgrade. v0.3.111 made the resolver type-strict and v0.3.112 switched TV title-search to `/search/tv` with year filtering, but stale rows written before those fixes (placeholder rows that pre-fix search couldn't resolve, TV→TV year mismatches, etc.) stayed cached and the narrower per-read invalidation didn't catch them. Forces fresh re-resolution through the now-correct paths. Single tracked-via-settings flag so it runs exactly once.

## [0.3.115] — 2026-05-04

### Fixed
- Combined jobs (video + audio/sub cleanup) discarded for negative savings were losing their cleanup work entirely. v0.3.69 added an audio-only follow-up enqueue for this case, but `add_job`'s de-dup guard sees the still-running original job for the same file_path and silently returns its id — no new row gets inserted. Fix: `add_job` now accepts an `exclude_job_id` so the follow-up enqueue can ignore the in-flight job it's spawned from.

## [0.3.114] — 2026-05-04

### Fixed
- v0.3.110's avg-fps fix only applied to the local `progress_cb` — the remote-worker `/api/nodes/report-progress` endpoint wrote `fps` directly via raw SQL with no phase gating, so multi-node deployments still had VMAF / audio-cleanup fps overwriting the encoding fps on each job row. Daily avg-fps charts kept showing ~30 fps. Same gate added there: persist fps only when `step in ("", "converting")`.

## [0.3.113] — 2026-05-04

### Fixed
- Audio classifier was reading `always_keep_languages` from the in-memory env-only config object, not the DB-backed user setting. So adding "eng" to Always-keep via the UI had no effect on audio cleanup — English audio on a non-English-native film got marked for removal. Subtitle cleanup was unaffected (it had a parallel DB-loader). Fix: audio classifier now uses the same DB-loader pattern; existing scan rows need a re-scan to re-classify with the correct list.

## [0.3.112] — 2026-05-04

### Fixed
- Title-search fallback was using TMDB's `/search/multi` whose `year` parameter only filters movies — TV shows came back year-blind and then got dropped by our type filter, leaving newly-added TV folders like `Nashville (2012)`, `Lost Girl (2010)`, `One Day at a Time (2017)` as "no match" even though TMDB had the show. Fix: typed endpoints (`/search/tv` with `first_air_date_year`, `/search/movie` with `year`) when a type hint is set; `/search/multi` only when no hint. Same fix in the manual-search modal so its candidates match the auto-resolver's.
- Cache invalidation for type-impossible rows now also honours dir labels: a folder under a "TV Shows"-labelled dir cached as `media_type="movie"` (or vice versa) is dropped and re-resolved. Pairs with the v0.3.111 `[tvdb-N]+movie` invalidation; together they cover both bracket-driven and label-driven type strictness.

## [0.3.111] — 2026-05-04

### Fixed
- TV folders with `[tvdb-N]` brackets could match to movies (e.g., `Matador (2014) [tvdb-281467] → Matador (1986)`). Fix: TVDB-ID lookup now returns ONLY tv_results, never falling back to movie cross-references; mismatches fall through to the type-constrained title search instead.
- Stale wrong-type cache rows are now invalidated on read: a `[tvdb-N]` folder cached with `media_type="movie"` is forced to re-resolve once. One-shot self-healing for entries written by the pre-fix resolver.

### Changed
- "Fix poster match" modal title is now "Fix match".

## [0.3.110] — 2026-05-03

### Fixed
- Avg FPS chart and daily fps stats showed ~30 fps after a job completed even when the actual encoder was running at ~200. The same `progress_cb` was reused for VMAF analysis, which writes the libvmaf analyser's fps (~30) over the encoding fps just before the job finalises. Fix: `progress_cb` now only persists fps during the encoding phase (`step=None` / `"converting"`), and `update_progress(fps=None)` preserves the existing column instead of NULLing it. Daily stats heal as new jobs complete; existing rows keep the wrong number until [reseed via `backfill_daily_stats` would help, or just wait for the chart to roll forward].

## [0.3.109] — 2026-05-03

### Changed
- Dashboard disk-space breakdown now names rows by physical mount instead of user-set label. `/media/<X>/...` paths show `<X>` (the per-disk segment); other paths show their first segment. Reverts the v0.3.101 label-preference for this card — multiple disks sharing a label (e.g., several drives all labelled "Movies") was masking which physical mount was low on space.

## [0.3.108] — 2026-05-03

### Performance
- Queue Completed/Failed tabs were slow to load (~10-15 s on 18K completed jobs) because the list-fetch was doing `SELECT *` and pulling `ffmpeg_log`, `ffmpeg_command`, and `encoding_stats` — per-job detail columns that can each be tens of KB. Fix: list endpoints now SELECT a slim column set excluding those three; the per-job log endpoint still serves them on row expansion. Same payload for Failed (where 200KB of ffmpeg stderr per job adds up fast). Computed dynamically from `PRAGMA table_info` so future schema additions auto-include.

## [0.3.107] — 2026-05-03

### Changed
- VMAF re-measure global banner now shows progress only and clears on completion. The outcome summary (rescued / unchanged / skipped) renders inline next to the Re-measure button in Settings → VMAF where the click happened, instead of as a top-of-page banner the user might miss.

## [0.3.106] — 2026-05-03

### Fixed
- "Lossless → EAC3" badge missing from the Now-Converting card on jobs with DTS-HD MA audio. The frontend was running its own client-side codec/profile match that only recognised exact strings `"dts-hd ma"` / `"dts-hd hra"`, so variants like `"DTS-HD Master Audio"` or `"DTS-HD MA + DTS:X"` slipped through. Fix: backend now computes `has_lossless_audio` in `/scan/tracks-by-path` using prefix matching (`dts-hd m*` / `dts-hd h*`); frontend trusts that flag instead of duplicating the check.

## [0.3.105] — 2026-05-03

### Added
- Global VMAF re-measure progress banner: shows current file + X-of-N count + progress bar from any page while a re-measure pass is running, plus a brief "X rescued, Y unchanged, Z skipped" summary on completion. Previously the only way to see progress was to stay on Settings → VMAF.

## [0.3.104] — 2026-05-03

### Fixed
- VMAF re-measure button stuck at 0 candidates when backups were disabled. `original_file_path` was only being recorded when a backup file was created, so users with `trash_original_after_conversion=true` or `backup_original_days=0` had no way to re-measure suspect scores. Fix: record the pre-rename source path on every completed job. Going-forward fix only — legacy jobs with NULL `original_file_path` need a manual SQL update.

## [0.3.103] — 2026-05-02

### Fixed
- NVENC capability detection skipped the test encode when `nvidia-smi` failed, so hosts where NVENC works but nvidia-smi isn't exposed (e.g. NVIDIA container runtime without the `utility` capability) showed "no NVIDIA GPU detected" on Monitor while actively converting via NVENC. Fix: always run the 1-frame test encode and trust rc==0; the test correctly fails on hosts without a real GPU.

## [0.3.102] — 2026-05-02

### Fixed
- Codec-tag rename in output filenames missed scene variants with a literal separator (`H.264`, `h-264`, `h_264`) — files came out HEVC but kept the misleading H.264 in their name. Fix: regex now allows an optional `.`/`-`/`_` between the letter and the digits. Existing files keep their stale names; future conversions rename correctly.

## [0.3.101] — 2026-05-01

### Fixed
- Dashboard disk-space breakdown ignored the user-set label and derived volume names from the path's 2nd segment, e.g. `/downloads/completed` showed as "completed" instead of the UI label "Downloads". Fix: prefer `media_dirs.label` when set, fall back to path-derived name.

## [0.3.100] — 2026-05-01

### Fixed
- Empty Plex trash after scan was failing for movies. Fix: `empty_plex_trash` now waits 15 s before issuing the request, scheduled as a detached background task so batch jobs don't block.

## [0.3.99] — 2026-05-01

### Fixed
- `[NODES] 401` advisory was flooding logs on every failing worker request. Fix: throttled to once per 5 min per (node_id, failure_kind).

## [0.3.98] — 2026-05-01

### Fixed
- Add-to-Queue modal showed inconsistent savings as the CQ slider moved. Fix: response now returns the median per-file CQ that actually drove the savings, so the slider initializes to a matching value.
- Estimate now also honors `libx265_crf_override` — pre-v0.3.98 only `nvenc_cq_override` was checked, so libx265 users got no estimate update from the CRF slider.

## [0.3.97] — 2026-05-01

### Fixed
- Per-folder rescan could leave the folder permanently empty if the walk silently failed. Fix: switched from delete-then-rewrite to upsert-then-orphan-cleanup; orphan delete only runs for paths that walked without raising.

## [0.3.96] — 2026-05-01

### Fixed
- Scanner poster view showed "No files scanned yet" when search filtered down to zero matches. Fix: PosterGrid empty-state now also checks `search` and `allowedPaths` (mirrors FileTree).

## [0.3.95] — 2026-04-30

### Documentation
- Settings → Encoding help text on Intel encoders now flags three gotchas: flat QSV preset cost curve, CQ↔CRF rough cross-encoder mapping, prefer QSV over VAAPI on Intel.
- README "Intel/AMD GPU support" reflects one external tester confirming end-to-end on Ubuntu 24.04 + modern Intel iGPU.

## [0.3.94] — 2026-04-30

### Fixed
- Settings → Default Encoder dropdown was hiding NVENC when caps reported `nvenc: false`, leaving the select desynced from saved state. Fix: NVENC and libx265 always render; QSV/VAAPI also render whenever they're the saved value.

## [0.3.93] — 2026-04-30

### Added
- Optional QSV look-ahead rate control toggle in Settings → Encoding (off by default; trades ~10-20% throughput for a small quality bump).

## [0.3.92] — 2026-04-30

### Fixed
- vpl-gpu-rt build (v0.3.89's addition) was OOM-killing the GHA free runner mid-link of `mfx_common_hw`. Switched to memory-conservative cmake config (`MinSizeRel` + `IPO=OFF` + `BUILD_TESTS=OFF`) and serial build (`--parallel 1`). Build takes ~10 min instead of ~5 min on the free runners but stays under the 7 GB budget. Cache hits make subsequent releases fast.

## [0.3.91] — 2026-04-30

### Fixed
- TMDB resolver now falls through to title-search even when an explicit bracket ID was parsed but TMDB couldn't resolve it. Pre-v0.3.91 a folder like `Man on Fire (2026) [tvdb-433027]` would be left unmatched (or, if the cache row pre-dated v0.3.83, mismatched to the 2004 movie) when TMDB's TVDB cross-reference index didn't yet have that ID mapped. The bracket family still drives the title-search media_type hint (`[tvdb-N]` → TV-only filter; `[ttN]` / `[tmdb-N]` → movie-only), and v0.3.83's strict year matching still applies — so falling through is much safer now than when v0.3.56 originally added the gate.

### Performance
- GHA build cache scope consolidated from per-variant (4 scopes) to per-Dockerfile (2 scopes). The heavy apt + libva + vpl-gpu-rt build layer is byte-identical across `:latest` / `:edge` and `:nvenc` / `:edge-nvenc` pairs; sharing scope lets the second variant cache-hit the heavy work the first did. Saves ~10 minutes of CI per release pair on cache hits.

## [0.3.90] — 2026-04-30

### Fixed
- Render-node auto-detection on multi-GPU hosts. Pre-v0.3.90 the QSV / VAAPI ffmpeg commands hardcoded `/dev/dri/renderD128`; on a host with both an Intel iGPU and a discrete NVIDIA card (NUC9, etc.) the iGPU is often `renderD129` and the hardcoded `D128` would have libva try to load the iHD driver against the NVIDIA render node — guaranteed failure. `encoder_caps` now reads `/sys/class/drm/<node>/device/uevent` to identify each render node's kernel driver and picks the right one per encoder (i915 for QSV, i915 / amdgpu / radeon for VAAPI; never NVIDIA). Falls back to `/dev/dri/renderD128` only if detection fails entirely (preserves single-GPU-host behaviour).
- `/api/stats/encoder-caps` response now includes `qsv_render_node` and `vaapi_render_node` fields so the UI / debugging can show which `/dev/dri/renderD*` will be used for each encoder.

## [0.3.89] — 2026-04-30

### Fixed
- Final fix for the QSV "MFX_ERR_INCOMPATIBLE_VIDEO_PARAM (-17)" saga: both Docker images now build **vpl-gpu-rt** (Intel's modern oneVPL GPU runtime, also called `libmfx-gen1`) from source. Stock Debian 12 / Ubuntu 22.04 only ship the legacy `libmfx1` MediaSDK runtime, which rejects libva-2.22 VADisplays with -17. With `libmfx-gen.so.1.2` installed, the `libvpl2` dispatcher routes through the modern runtime and `hevc_qsv` works against current libva + iHD drivers. Adds ~3 min to image build; ~12 MB final-image impact. Build toolchain (`cmake`, `meson`, `ninja`, dev headers) purged after compile.

## [0.3.88] — 2026-04-30

### Fixed
- VAAPI failing with `libva.so.2: undefined symbol vaMapBuffer2` (libva ABI mismatch — BtbN n7.x ffmpeg expects libva ≥ 2.20, Debian 12 / Ubuntu 22.04 stock provides 2.14–2.17). Both Docker images now build libva 2.22.0 from source as part of the image layer and replace the system `libva.so.2` with it. libva 2.x is ABI-stable, so the existing iHD / mesa-va / i965 drivers (compiled against 2.17) continue to work against 2.22 without rebuilding. Build adds ~30 s to image creation; build toolchain (build-essential, meson, ninja, libdrm-dev) is purged after compile so final image size impact is negligible.

## [0.3.87] — 2026-04-29

### Fixed
- QSV encode failing with `device failed (-17)` (`MFX_ERR_INCOMPATIBLE_VIDEO_PARAM`) on Linux + iHD systems where ffmpeg's auto-init couldn't get a hardware device handle. ffmpeg command builder now does the two-step Linux QSV init: create a VAAPI device pinned to `/dev/dri/renderD128`, then create a QSV context that adopts it (`qsv=qsv@va`). Standard Intel-on-Linux pattern.
- Documented the VAAPI `libva.so.2: undefined symbol vaMapBuffer2` failure for users on Debian 12 / Ubuntu 22.04 (system libva 2.14–2.17 vs the BtbN ffmpeg's libva 2.20+ ABI expectation). Workaround in `docs/installation.md`: use QSV instead, or build with `--build-arg FFMPEG_BUILD=n6.1`. Bundling a newer libva in the image is on the roadmap.

## [0.3.86] — 2026-04-28

### Added
- Settings → Connections → TMDB section gets two new buttons for retroactively re-running poster auto-resolution: **Re-resolve placeholders** (cheap retry on entries that previously failed) and **Re-resolve all auto-matched** (with confirm dialog — wipes every TMDB/Plex auto-resolved entry and re-fetches from scratch). Manual fixes are always preserved. Useful after the resolver-logic improvements in v0.3.81–v0.3.85 — apply the new logic to entries cached by older code without manually clicking through each one.

## [0.3.85] — 2026-04-28

### Added
- Media-ID detection (TMDB / TVDB / IMDb) now recognises file-level tagging in addition to folder-level. When a folder name carries no ID, `parse_folder_name` walks the files inside and uses the first ID it finds. The Scanner type-filter classifier searches the full file path the same way.
- Bracket-less and curly-brace ID forms now also recognised: `tt1234567` (bare, ≥7 digits), `tvdb-12345` / `tmdb-12345` (bare with separator), `{tmdb-12345}` (Plex), `[tvdbid-12345]` / `[tmdbid-12345]` (Jellyfin) — alongside the existing `[ttN]` / `[tvdb-N]` / `[tmdb-N]`.

## [0.3.84] — 2026-04-28

### Fixed
- `hevc_qsv` failing with `Error creating a MFX session: -9` on hosts that have a working iHD VA-API driver. The QSV encoder requires Intel's oneVPL / MediaSDK runtime in addition to iHD; v0.3.67 only baked iHD. Both Docker images now also install `libvpl2` (oneVPL dispatcher) and `libmfx1` (legacy MediaSDK runtime, ships the HEVC encoder hardware module). VAAPI was always a working fallback for users on the affected versions — added a docs note pointing at it.

## [0.3.83] — 2026-04-28

### Fixed
- TMDB title-search resolver no longer silently picks a wrong-year match when the user's folder has a year. Old order ran "exact title + exact year" → "exact title (any year)" → "partial title + exact year"; the middle pass would happily admit the 2006 *See No Evil* when the user asked for 2014, even though the 2014 sequel ("See No Evil 2") would have matched the year-aware partial pass that ran *next*. Reordered to year-aware partial-match before any year-blind pass; if no year-aware pass matches, the resolver returns a placeholder rather than fabricating a wrong-year result. Added a ±1-year fuzzy pass for metadata drift.
- Cases this fixes: *See No Evil (2014)* now resolves to "See No Evil 2" (2014) instead of "See No Evil" (2006); *Odyssey (2025)* now resolves to "The Odyssey" (2025) via partial-title + exact-year matching.
- Manual "Fix poster match" search re-ranker mirrors the same priority order so the modal's first card matches what auto would pick.

## [0.3.82] — 2026-04-28

### Fixed
- TMDB resolution and the manual "Fix poster match" search now also use the containing media-dir's label as a media-type hint when the folder name has no bracket ID. A "Movies"-labelled directory's folders no longer accidentally match same-titled TV shows during auto-resolution, and vice versa. Bracket IDs (`[ttN]`/`[tvdb-N]`/`[tmdb-N]`) still take precedence when present; "Other" / unset labels stay unconstrained.
- Auto-resolution's `media_type` backfill now uses the dir-label as a final fallback when TMDB title-search returns nothing — so a movie in a `Movies`-labelled dir gets `media_type=movie` even with zero TMDB metadata.

## [0.3.81] — 2026-04-28

### Fixed
- "Fix poster match" modal now uses bracket IDs from the folder name (`[ttN]`/`[tvdb-N]`/`[tmdb-N]`) to fetch the exact TMDB record and pin it as the first result. Pre-v0.3.81 the modal was title-search only, so an obscure-but-correct match could fail to surface even when the IMDb ID was sitting right there in the folder name.
- Manual search now also filters candidates to the right media_type when the bracket tells us (movies for `[ttN]`/`[tmdb-N]`, TV for `[tvdb-N]`) — no more TV shows mixed in when fixing a movie.
- Title-search candidates re-ranked: exact title + exact year matches sort first regardless of TMDB's popularity ranking.
- Manual poster override now also writes the TMDB-authoritative `original_language` to `scan_results.native_language` for every file in the folder, so audio-cleanup rules that key off native language pick up the corrected value automatically.

### Added
- Folder-name parser now recognises Radarr 5+ default `[tmdb-N]` token alongside the existing `[ttN]` and `[tvdb-N]`.

## [0.3.80] — 2026-04-28

### Added
- "Cleanup only — no video conversion" checkbox in the Add-to-queue modal. Runs only the audio/sub cleanup pass (track removal, native-language reorder, codec transcode) on the original file. Mutually exclusive with **Force re-encode**.

## [0.3.79] — 2026-04-28

### Fixed
- TV Shows / Movies / Other filters in the Scanner grid did nothing — applying or removing them returned the same set (regression from v0.3.76 when the type filter was moved out of SQL push-down). The tree endpoint's hand-rolled per-filter loop didn't have a case for the type filters, so they passed through silently. Now wired in.

## [0.3.78] — 2026-04-28

### Added
- Scanner filter pills persist across page navigations and browser restarts (localStorage). The existing **Clear** pill resets to "all".

## [0.3.77] — 2026-04-28

### Fixed
- Movies type filter now also matches `[tmdb-N]` bracket tokens (Radarr 5+ default), in addition to the legacy `[tt…]` (IMDb) form. No rescan needed — classification runs at read time.

## [0.3.76] — 2026-04-28

### Fixed
- Type filters (Movies / TV Shows / Other) showed 0 for users whose folders aren't Sonarr/Radarr-style with `[tvdb-N]` / `[ttN]` brackets — every file fell through to "Other" because the classifier ignored the directory labels set in Settings → Directories. Now combines bracket markers (still win when present, most specific) with the containing media-dir's user-set label as the fallback.

## [0.3.75] — 2026-04-28

### Fixed
- "Regenerate API key" button on Settings → System → Authentication did nothing on plain-HTTP LAN access (the client-side `crypto.randomUUID()` it relied on is undefined outside a secure context). Now generates server-side via `secrets.token_hex` and persists atomically — works in any browser context and takes effect immediately.

## [0.3.74] — 2026-04-28

### Fixed
- Multi-arch CPU image build (the v0.3.67 → v0.3.73 saga) finally diagnosed and fixed: `python:3.11-slim-bookworm` uses the DEB822 sources format with `Signed-By:` set, so dropping a second `.list` for the same `bookworm` repo without a Signed-By directive made apt fail with `Conflicting values set for option Signed-By`. Now edits the existing `debian.sources` to add `non-free` and `non-free-firmware` to the existing `Components:` line instead of adding a parallel sources file. Verified the install end-to-end locally before pushing.

## [0.3.73] — 2026-04-28

### Fixed
- Frontend `tsc -b` build failed with TS1005 / TS1381 because a JSX comment in `SettingsPage.tsx` contained `qsv_*/` — `*/` inside a JSX comment terminates the block early. Reworded.
- arm64 leg of the multi-arch CPU image still failing on apt-get exit 100. Reverted the arm64 build's apt step to its pre-v0.3.67 minimal install (curl + xz-utils only) — VA-API packages now ship only on amd64. arm64 hosts rarely have AMD/Intel GPUs reachable anyway; users on those edge cases still get libx265 + (theoretically) NVENC if they pull the `:nvenc` image (which is amd64-only).

## [0.3.72] — 2026-04-28

### Fixed
- arm64 leg of the multi-arch CPU image build still failing on the apt-get step (v0.3.71 gated the Intel-only packages but kept the non-free repo enabled for both arches). Moved the non-free repo enablement inside the amd64 conditional so the arm64 build never touches non-free metadata. Added `set -x` to surface which command fails in future build logs.

## [0.3.71] — 2026-04-28

### Fixed
- Multi-arch CI build failing on the arm64 leg of the CPU image since v0.3.67 (`intel-media-va-driver-non-free` and `i965-va-driver` are AMD64-only in Debian; the apt-get step now only requests them when `TARGETARCH=amd64`). arm64 builds keep the arch-agnostic VA-API runtime (libva, mesa-va-drivers, vainfo) so AMD GPU users on arm64 still get VAAPI.

## [0.3.70] — 2026-04-28

### Added
- Dispatcher capability gating: QSV and VAAPI jobs only schedule on nodes that advertise the matching encoder capability (no NVENC↔QSV translation — those are vendor-specific hardware paths).
- README and `docs/installation.md` now have an "Intel/AMD GPU support (experimental)" section with the compose passthrough snippet, verification steps, and troubleshooting for the most common `vainfo` failures. Help wanted from anyone who can run it end-to-end.

## [0.3.69] — 2026-04-28

### Added
- Estimate modal and rule editor encoder pickers now include Intel QSV and VAAPI options when the host supports them.
- When a combined (convert + cleanup) encode is discarded for being larger than the source, the audio/sub cleanup is now retried as a follow-up audio-only job — the cleanup the user wanted gets applied to the original instead of being silently lost.
- Worker nodes now advertise QSV and VAAPI alongside NVENC / libx265 in the capabilities list.

### Changed
- Encoded-but-larger files are no longer kept "for the cleanup's sake" — they're discarded and the cleanup is requeued separately. Net result: same cleanup, smaller file.

## [0.3.68] — 2026-04-28

### Added
- Settings → Encoding now exposes Intel QSV and Intel/AMD VAAPI as encoder options (auto-detected; only shown when the host can run them). Per-encoder preset and quality controls follow the NVENC / libx265 pattern. Compose-passthrough hint and a "Re-detect" button included.

## [0.3.67] — 2026-04-28

### Added
- Foundation for Intel QSV and Intel/AMD VAAPI hardware encoding — VA-API runtime baked into both Docker images, encoder detection, ffmpeg command builder, settings defaults, completed-job report labels. Settings UI / rule overrides / multi-node advertisement land in v0.3.68 and v0.3.69.

## [0.3.66] — 2026-04-28

### Fixed
- "Update available" modal now shows the actual new release notes from GitHub instead of re-rendering your installed CHANGELOG.md (which only goes up to your installed version).
- Modal force-refreshes the version check on open so the "latest is vX.Y.Z" header reflects the freshest GitHub data, not a 30-min-stale cache value.
- "LATEST" badge now lights up on the entry whose version actually matches the upstream latest, instead of always tagging the topmost local entry.

## [0.3.65] — 2026-04-28

### Fixed
- Fresh-install Docker containers now land on the setup wizard instead of a login screen with no obvious key (auth starts disabled; loud `[SECURITY]` banner prompts you to enable it in Settings before exposing the port).

## [0.3.64] — 2026-04-27

### Added
- Scanner now updates live when the watcher discovers new files — the NEW filter (and the rest of the file tree) refreshes without you having to navigate away and back.

## [0.3.63] — 2026-04-27

### Fixed
- Add-to-queue toast no longer says "all already queued" when new items were actually added (`cursor.lastrowid` is `None` after `executemany` per Python sqlite3 — read `MAX(id)` after the insert instead).

### Performance
- First-time poster resolution for a batch of new items now runs 8 paths in parallel instead of one-at-a-time, cutting initial render of ~30 new items from 30+ s to a few seconds.

## [0.3.62] — 2026-04-27

### Fixed
- "Update available" button no longer disappears on the local-network URL when it shows on the remote URL — `/stats/version` is now `Cache-Control: no-store` on both server and client.

## [0.3.61] — 2026-04-27

### Fixed
- Bulk-added items now land in the queue in alphabetical order by file path so episodes group by show/season instead of arriving in random order.

## [0.3.60] — 2026-04-27

### Fixed
- "No items added" toast appearing after the bulk add even when items were successfully queued (regression from v0.3.57's executemany refactor).
- Toast now says "All N items were already in the queue" when every selection was a dup, and "Added N (M already queued)" when some were and some weren't.

## [0.3.59] — 2026-04-27

### Documentation
- Trimmed recent changelog entries to one-liners and adopted that style going forward.

## [0.3.58] — 2026-04-27

### Documentation
- Updated the VMAF analysis settings copy to match reality.

## [0.3.57] — 2026-04-27

### Added
- Loading overlay while adding many items to the queue.

### Fixed
- Bulk queue add is now much faster.

## [0.3.56] — 2026-04-27

### Fixed
- TMDB matching now honors `[tvdb-N]`/`[tt…]` IDs even when the show has no poster on TMDB.
- Title-search fallback no longer runs when an explicit ID is present, preventing wrong-show guesses.

## [0.3.55] — 2026-04-27

### Removed
- Disabled the subtitle-prestrip pre-pass that caused a long wait before encoding multi-sub files.

### Added
- Completed-job report now shows the original-vs-encoded comparison for "ignored — encode was larger" outcomes.

## [0.3.54] — 2026-04-26

### Fixed
- Removed trailing `0` after the VMAF tier label in the completed-job report.

## [0.3.53] — 2026-04-26

### Fixed
- Hidden `Scan=off` media directories from the Scanner dropdown.

## [0.3.52] — 2026-04-25

### Documentation
- Documented the NZBGet/SABnzbd setup prerequisites in the Settings UI and `docs/rules-and-automation.md`.

## [0.3.51] — 2026-04-26

### Fixed
- API error toasts now show the backend's actual reason instead of `API error: 400`.

## [0.3.50] — 2026-04-26

### Fixed
- "+ Add" on Settings → Media directories now shows an error toast on failure instead of doing nothing.

## [0.3.49] — 2026-04-26

### Added
- Per-media-dir "Scan" toggle — webhook-eligible without the scanner crawling it (for NZBGet/SABnzbd landing zones).

## [0.3.48] — 2026-04-26

### Fixed
- **Renaming section's "Save" button moved to the left** to match every other section in Settings. Encoding, audio, lossless, etc. all use `alignSelf: "flex-start"` for their save buttons; only `RenamingSettings.tsx` had `justifyContent: "flex-end"`. Now consistent.

## [0.3.47] — 2026-04-26

### Fixed
- **Completed-job report showed `x265 (NVENC)` instead of `h265 (NVENC)`.** The codec label in the expanded job details was hardcoded as `x265` regardless of the encoder, even though v0.3.30's rename rule already distinguished between them: libx265 → `x265` (the specific encoder), NVENC → `h265` (the codec spec, encoder-agnostic). The same rule now applies to the report so the on-screen label matches the renamed output filename.

## [0.3.46] — 2026-04-26

### Fixed
- **Conversion failure on files with orphan VobSub `.sub` external subtitles** (`ffmpeg exited with code 254`, `[vobsub @ ...] Unable to open <name>.sub as MPEG subtitles` followed by `Error opening input file <name>.idx: No such file or directory`). VobSub external subs are a paired format — `.idx` (index/metadata) + `.sub` (bitmap data) — and ffmpeg's vobsub demuxer auto-resolves the partner from disk. If only one half of the pair exists, the demuxer fails and the whole encode aborts. `detect_external_subtitles` was including every `.sub` and `.idx` it found, so an orphan crashed the job. Now: `.sub` files are represented via their `.idx` partner (ffmpeg picks up the `.sub` from the same stem), and an orphan `.sub` *or* orphan `.idx` is skipped with a clear log line. Plain text-format `.sub` files (subviewer) are also skipped — rare in the wild compared to vobsub, and skipping one text track is much less harmful than failing the entire encode.

## [0.3.45] — 2026-04-25

### Fixed
- **Settings → Encoding → Parallel jobs now syncs to the local node's `max_jobs`.** Pre-v0.3.45 the two settings represented the same thing (capacity for the in-process worker queue) but lived in separate DB rows, with the per-node value silently winning. Changing the global slider from the Settings page didn't propagate, so users editing parallel jobs there saw their change ignored at runtime. Both sides now stay in sync: changing the global slider on Settings → Encoding updates the local node's `max_jobs`, and changing the local node's max from Nodes → Settings updates the global setting. Remote nodes are still configured per-node — they reflect per-host hardware.

## [0.3.37–0.3.44] — 2026-04-25

### Progress-reporting overhaul (eight iterations, several reverts)

Long debugging chain to fix progress bars stalling on certain WEBDLs (Brotherhood, Borgen, Breathless). Recorded as a single entry rather than eight because most of the intermediate releases were misdiagnoses that got rolled back when they didn't help.

**Net effect vs v0.3.36 — what shipped and stayed:**

- **Frame-counter fallback in `parse_ffmpeg_progress`** (added v0.3.43, finalized v0.3.44). The actual root cause: ffmpeg's `time=` field reflects the muxer's *committed-output* position, which on some files stalls behind the encoder for the entire encode (most commonly when `-c:a copy` passes through audio with non-monotonic PTS that the muxer can't commit). The parser was time-only and ignored `frame=`, so when the muxer's clock was stuck or `N/A`, progress was stuck even though the encoder was happily producing frames. Now parses both fields every progress line and uses whichever yields *higher* progress — `frame=` is always honest, `time=` can lag arbitrarily. Confirmed in the field: same Borgen file went from "stuck at 6.43% with fps climbing to 397" to smooth advancement.

- **Pre-strip pass for files with ≥6 subtitle removals** (v0.3.39). Two-pass workflow: a fast `-c copy` remux drops unwanted subs first, then the main encode runs on a clean 5-7 stream file. Originally diagnosed as the cause of the stall (it wasn't — the parser was) but kept because it's independently useful for the genuine "32+ subtitle streams in a WEBDL" case where the muxer's per-stream interleave queue does fill up.

- **`backend.scanner.probe_file`** now exposes `video_fps` so the converter can compute total expected frames as the divisor for the parser's frame-count fallback.

**Reverted along the way** — kept here for archaeology since several were active in published images:

- `-fflags +flush_packets` (added v0.3.37, removed v0.3.38) — forced per-packet flushes, ~20% throughput cost.
- `-max_muxing_queue_size 9999` (added v0.3.37, removed v0.3.42) — bigger muxer queue weakened ffmpeg's natural back-pressure between encoder and muxer.
- Fire-and-forget DB writes via `asyncio.create_task` in `progress_cb` (added v0.3.40, removed v0.3.41) — same back-pressure problem; removed the await that mediated concurrent NVENC sessions politely sharing the GPU.

**Lessons noted for next time:** when only *some* files stall, the cause is usually in parsing/reporting, not the encoder. A single-job-at-a-time test is the cleanest way to rule out concurrency before chasing GPU contention or I/O hypotheses. Files where `frame=` advances but `time=` parks behind it are surprisingly common in WEBDL → `-c:a copy` flows.

## [0.3.36] — 2026-04-25

### Fixed
- **Per-job progress bars stuck for ~60s at a time, then jumping in big increments — even after the v0.3.35 WebSocket fix.** Inverting the ETA formula on stuck jobs showed both jobs' last DB-persisted progress was always ~57 seconds old simultaneously, while ffmpeg was happily burning ~380% CPU on each. Diagnosis: every ffmpeg progress line triggered an `aiosqlite.connect → UPDATE → commit → close` cycle for that job's row in `jobs`. Under contention from any other transaction holding the WAL write lock (some 60-second periodic loop in the codebase), all four progress writers (2 jobs × 2 lines/sec) queued behind it, blocked the converter's progress callback, and back-pressured ffmpeg's stderr buffer until the lock released and the queued events flushed in a burst. Fix: decouple DB persistence from live-UI updates. WebSocket broadcast keeps firing on every progress line (already throttled to 500ms per job server-side); the DB write is now throttled to once every 3 seconds per job, with a guaranteed flush on terminal progress (≥99.99) so the persisted final value is never off. Same throttle applied to the remote-worker `report_progress` HTTP path (2-second interval) so busy nodes don't flood the server. Smoke-tested: 25 WS broadcasts vs 5 DB writes over 12 seconds, terminal flush guaranteed.

## [0.3.35] — 2026-04-25

### Fixed
- **Progress bars stuck for minutes, then jumping in big increments.** WebSocket `broadcast()` was awaiting `send_json` *serially* for each connected client. Any slow / half-dead client (background browser tab, mobile on weak signal, Tailscale tunnel with packet loss, stale connection from a tab that didn't close cleanly) blocked every other client for as long as that one connection took to time out at the TCP layer. While blocked, every job's progress callback queued behind it; when the slow connection finally drained, the queued events flushed at once, manifesting as a progress bar jumping from e.g. 2% to 26% after several minutes of standstill. Fix: send to all connections in parallel via `asyncio.gather`, with a 2-second per-connection timeout. A sluggish client gets dropped after 2 seconds and the rest of the broadcasts complete uninterrupted. Smoke-tested with one 10-second-stalled connection alongside two healthy ones — the broadcast now returns in 2.0s (was 10s) and the healthy clients receive every message.

## [0.3.34] — 2026-04-25

### Fixed
- **Watcher log spam from AppleDouble companion files.** On Mac-formatted volumes (HFS+/APFS shared via SMB/AFP), every `.mkv` has a sibling `._<name>.mkv` resource fork that carries the same extension but contains HFS metadata, not video. The watcher walk used to include them, ffprobe rightly failed on them, and `[WATCHER] Skipped: 0 ignored, 200 probe failed, 0 AV1` would fire every cycle. Watcher's directory walk now skips dotfiles (`name.startswith(".")`), matching `scanner.py`'s pre-existing filter. Files already cached in the in-memory `_probe_failures` set fall out naturally on the next cycle since they're no longer in `disk_files`.
- **`[WATCHER] Pre-filtered: …` log deduplicated.** A stable backlog (e.g., 600 always-failing files plus zero new content) used to repeat the exact same numbers in the log every 5 minutes — non-actionable noise. Now only emits when the `(ignored, probe_failures, to_process)` tuple changes since last cycle.

## [0.3.33] — 2026-04-25

### Fixed
- **Dropdown arrows missing on Activity / Logs / Schedule pages and a few modals.** Inline `style={{ background: "..." }}` shorthand on `<select>` elements was wiping the global `background-image: <chevron>` rule from `theme.css`, so the carat indicator never rendered. Switched the affected callsites to `backgroundColor:` instead so the global chevron survives. Same root cause as the v0.3.22 checkbox-checkmark fix.
- **Type=Other media directories no longer trigger TMDB lookups.** When you add a folder and pick "Other" from the type dropdown, the scanner / watcher / metadata-refresh / poster-resolution paths now skip TMDB matching for files inside it. Previously a folder of home videos labelled "Other" would still get matched against TMDB's catalogue, producing spurious posters and original-language tags. New helper `backend.media_paths.is_other_typed_dir(path)` is the single gate; case-insensitive on the label.

## [0.3.32] — 2026-04-25

VMAF measurement reliability + activity log readability.

### Fixed
- **VMAF "bimodal desync" measurement bug.** libvmaf occasionally desynced its frame-pair iteration partway through the 30-second analysis window — half the frames scored ~100, half scored ~0, the recorded mean landed somewhere in between (e.g. 39.5 or 61.6) and a visually-fine encode was reported as "Poor". Diagnosis: see `min=0.0 max=100.0` in the converter log. Fix: detect the bimodal signature (min < 20 ∧ max ≥ 90) and re-run VMAF at an alternate seek (66% / 33% of duration, ≥60s away from the primary). Take the higher of the two scores. If every pass came back bimodal, persist the score but flag the new `jobs.vmaf_uncertain` column so the UI can surface a ⚠ glyph next to the score and a tooltip explaining "measurement-suspect — encode is almost certainly visually fine."
- **eac3 (Dolby Digital Plus) decoder warnings** (`expacc N is out-of-range`, `error decoding the audio block`) added to the health-check benign allow-list. These trip on streaming-service rips (HBO Max / Apple TV+ / etc.) but the audio plays fine in any real player. Files that previously got flagged "Corrupt (quick)" purely on these messages are now classified "warnings" and stay healthy for queue/auto-ignore.

### Added
- **Re-measure suspect VMAF scores** button in Settings → Encoding → VMAF. Iterates completed jobs whose recorded score landed below the "Excellent" tier or got flagged uncertain, and re-runs VMAF (with the same bimodal-aware retry path used for fresh encodes). Skips jobs whose original pre-rename source no longer exists on disk (typical when "delete original after conversion" is on). Live progress streams over the WebSocket; existing 30+ bad scores can be cleaned up without re-encoding anything.
- **Activity log + History tab now colour-code outcomes** rather than always-green:
    - `Health check: corrupt` → red, `warnings` → amber, `healthy` → green.
    - `VMAF: <score>` → green / amber / red based on the canonical 3-tier table; uncertain measurements get amber regardless of the underlying score.
- **Canonical 3-tier VMAF table everywhere** (FilterBar, JobListItem, EstimateModal, FileDetail, DashboardPage donut, EventTimeline, SettingsPage threshold). Excellent (93+) → green, Good (87–93) → amber, Poor (<87) → red. Backend mirrors the same cuts in `backend/queue.py`, `backend/test_encode.py`, `backend/routes/stats.py`. The previous "Fair" tier (80–87) was inconsistent across components — folded into Poor. `frontend/src/utils/vmaf.ts` is the new single source of truth.

### Changed
- `convert_file`'s VMAF block extracted into `_run_libvmaf_pass()` and `remeasure_vmaf()` helpers in `backend/converter.py`. Cuts ~150 lines of inline ffmpeg-spawning duplication and lets the remeasure endpoint share the exact same filter pipeline + bimodal-detection logic as fresh encodes.
- Cross-check (SSIM/PSNR) now runs against the same window that produced the chosen VMAF score, not always the primary seek — so a rescued retry-window score is sanity-checked at *its* window, not the bimodal one.

## [0.3.31] — 2026-04-24

Follow-up release cleaning up rough edges from the v0.3.30 migration.

### Added
- **Path mappings editor in the Node Settings modal.** Admin-editable override that takes precedence over the worker's `PATH_MAPPINGS` env var. Translation stays server-side so no worker restart is needed — change a mapping, save, next job dispatch uses the new value. The worker's env-var mappings are still shown in the modal for reference, and clearing the override reverts to them.
- **`SHRINKERR_DISABLE_NODE_TOKENS=true` escape hatch** for heterogeneous upgrades. When set on the server, per-node token enforcement is bypassed entirely, letting a v0.3.31 server talk to pre-v0.3.30 workers that haven't been updated yet. Prints a loud `[SECURITY] WARNING` on every startup so it can't silently stay on past the migration.
- **Diagnostic logs in server output** when a worker sends a `/api/nodes/*` call with no `X-Node-Token` but the server has one on file (`[NODES] 401 for node 'X': server has a stored token but the request sent no X-Node-Token...`). Surfaces the pre-v0.3.30-worker-vs-v0.3.30-server mismatch from the server side, so admins don't have to tail worker logs to diagnose it.

### Changed
- **Settings → Metadata APIs** now shows the green "TMDB is already connected" banner when using either the bundled key or a user-configured key. Previously only the bundled case got the banner, so admins who'd saved their own key saw no visible change in v0.3.30. Banner subtext differs per source.
- **docs/remote-workers.md** — path mappings section rewritten to cover the new UI override, the `PATH_MAPPINGS` env var fallback, and their precedence. Authentication section adds an "Upgrading server before workers" subsection documenting the escape hatch.

## [0.3.30] — 2026-04-23

### Security

Closes the last deferred item from the v0.3.28/0.3.29 hardening pass.

#### Added
- **Per-node worker tokens.** Remote workers now carry a per-node auth secret on top of the shared `X-Api-Key`. On first heartbeat the server generates a token with `secrets.token_hex(24)`, persists it in the `worker_nodes` table, and returns it in the response body. The worker writes it to `/app/data/worker_token` (mode 0600) and sends it on every subsequent call as `X-Node-Token`. The server compares with `hmac.compare_digest` and returns 401 on mismatch. Even if the shared API key leaks, an attacker who registers a fresh node can't impersonate an existing one.
- **Admin rotation** (Nodes → [node] → Settings → **Rotate token**): invalidates the stored token immediately. The worker drops its cached copy on the next 401 and re-bootstraps on its next heartbeat — no worker restart needed.
- `docs/remote-workers.md` documents the bootstrap + rotation flow; `docs/security.md` moves per-node tokens out of "deferred" into shipped defences.

#### Fixed
- `GET /api/nodes` no longer returns the `token` column to the frontend. The read surface now exposes a `has_token` boolean + `token_issued_at` ISO timestamp instead — the token itself never leaves the server.

### Changed
- **Encoder-aware rename.** Files encoded with NVENC now rename to `*.h265.*` instead of `*.x265.*` — `x265` is a specific libx265 binary, `h265` (a.k.a. HEVC) is the codec. libx265 jobs keep the `x265` tag. The scan dedup logic considers both siblings so existing `x265`-named NVENC outputs are still recognised on rescan. `rename_x264_to_x265` is kept as a back-compat alias.
- **Settings → Video → Conversion Guide** now has full libx265 preset + CRF tables and a recommended-combinations table alongside the existing NVENC ones, so CPU-only users see matching guidance. Expanded the tips section with NVENC-vs-libx265 quality equivalence, preset scaling, and CRF semantics.
- **Settings → Metadata APIs** shows a green "TMDB is already connected" banner when the bundled key is active, so users on fresh installs see that posters / native-language detection work out of the box and the TMDB input is strictly optional polish.

## [0.3.29] — 2026-04-23

### Security

Phases 2 + 3 of the security hardening pass. New [`docs/security.md`](docs/security.md) documents the threat model and a hardening checklist.

#### Fixed
- **Passwords** now hashed with bcrypt (cost 12). Legacy SHA-256 hashes from older installs are transparently upgraded on first successful login.
- **Secret comparisons** (API key, password hash, session signature) all use `hmac.compare_digest` — no more timing oracles.
- **Session signing** fails closed when `session_secret` is empty; the old code fell back to a literal `"default-secret"` constant that made every un-configured install's sessions forgeable. Startup now auto-generates `session_secret` on any DB that's missing it, not only on fresh installs.
- **Login rate-limited** to 8 attempts/minute/IP.
- **`post_conversion_script` setting** refuses to save a non-empty value when `auth_enabled=false`. Changing this setting runs arbitrary commands after every encode — now it requires the password-auth gate, not just an API key.
- **SSRF protection** on user-configured outbound URLs (Plex, Sonarr, Radarr, Discord webhook, generic webhook). Link-local (`169.254.0.0/16` — covers AWS / Azure / GCP / Alibaba metadata endpoints), IPv6 link-local, and IPv6 site-local ranges are rejected at save time.
- **Session cookies** set `Secure` flag when the request arrived over HTTPS (detected via scheme + `X-Forwarded-Proto`).
- **Settings export** now strips every secret row (`api_key`, `session_secret`, `auth_password_hash`, integration API keys, SMTP password, path-tokened URLs). Previously a leaked export file handed over every stored credential.
- **`/api/settings/browse`** refuses to list the filesystem root or any system directory (`/etc`, `/root`, `/proc`, `/sys`, `/boot`, `/dev`). The old picker could be used as an unauthenticated filesystem enumerator before v0.3.28's auth middleware fixes; now it's locked regardless of auth state.
- **LIKE search patterns** escape `%` and `_` metacharacters with `ESCAPE '\\'`. Stops user-supplied search strings from enumerating scan_results via wildcard patterns.
- **Symlink guards** on rename / backup write destinations. Refuses to follow a pre-existing symlink at the target path — closes the window where an attacker with write access to the backup folder could redirect the rename.
- **TMDB API key** passed as `params={...}` rather than interpolated into the URL string — httpx exception messages no longer carry the raw key.
- **python-multipart bumped** to 0.0.18 (CVE-2024-53981 DoS).
- **`.env` / `.env.*`** added to `.dockerignore` so local secrets never leak into image layers.

#### Added
- **Baseline security headers** on every response: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`, `Referrer-Policy: strict-origin-when-cross-origin`.
- **`backend/ssrf_guard.py`** — reusable URL validator; can also block RFC 1918 private ranges when `block_private=True` for cloud deployments.
- **`docs/security.md`** — threat model, list of in-app defences, hardening checklist for production deployments.

#### Remaining (tracked for the next release)
- **Per-node worker tokens.** Shipped in [0.3.30].

## [0.3.28] — 2026-04-23

### Security

Phase 1 of a security hardening pass — closes the most severe findings from the internal audit. No breaking changes for existing installs; a few previously permissive defaults tighten up on fresh installs.

#### Fixed
- Auth middleware no longer fails **open** when the settings DB read raises — returns 503. A transient SQLite lock could previously disable auth for the whole process.
- Auth middleware now enforces when a non-empty `api_key` is configured, independent of the `auth_enabled` flag. The old behaviour gated only when password auth was toggled on, so setting a key without flipping the toggle left the app wide open.
- `api_key` is masked (`****xxxx`) in `/api/settings/encoding` GET, matching every other stored secret. Dedicated `GET /api/settings/api-key` returns the unmasked key on demand for the Settings → System page and the copy-to-clipboard button.
- Integration endpoints (`/api/webhooks/*`, `/api/nodes/*`, `/api/settings/backup/{download,restore}`, `/api/settings/nzbget-config`, `/api/settings/{nzbget,sabnzbd}-script`) always require an API key — even when `auth_enabled=false`. Previously a LAN-exposed install handed out RCE-adjacent primitives to anyone who could reach port 6680.
- `/api/settings/dirs` POST now validates the path: must be absolute, must be an existing directory, must not be the filesystem root or under `/etc`, `/root`, `/proc`, `/sys`, `/boot`, `/dev`, `/app/data`. Stops an attacker bypassing every downstream containment check by adding `"/"` as a media directory.
- `backup_folder` setting validated the same way. Stops the conversion pipeline from being coerced into renaming originals into privileged directories.
- `/api/scan/delete-file` containment check rewritten with `Path.resolve()` + `os.path.commonpath` — the old `startswith` check was defeatable by `"/media/../etc/hostname"` (literally starts with `/media/`).
- `/api/webhooks/{scan,queue}` and `/api/jobs/add-by-path` now verify every supplied path resolves inside a configured media directory before running ffprobe/ffmpeg.

#### Added
- Fresh installs auto-generate a strong `api_key` + `session_secret` and enable password auth on first startup. The generated key is printed once, prominently, to the container logs.
- Existing installs with both `api_key=""` and `auth_enabled=false` now get a loud `[SECURITY]` warning banner on every startup so the operator knows they're running unauthenticated.

## [0.3.27] — 2026-04-23

### Changed
- Setup wizard: reordered to Add directories → Scan library → Customize setup (optional) → Start converting, now that the bundled TMDB key means scanning gives posters + native-language detection on the first run without any prior configuration.
- Setup wizard: renamed the "Add connections" step to "Customize your setup" — reframes it as optional polish (linking Plex / Jellyfin / Sonarr / Radarr, encoder tuning, your own TMDB key) rather than a required connections step.
- Installation doc updated to match the new step order and to explain bundled-vs-own-TMDB-key trade-offs.

## [0.3.26] — 2026-04-23

### Changed
- Settings → Metadata APIs reframed now that TMDB ships with a bundled key. When a bundled key is active the copy explains TMDB already works and the input is labeled optional; the connection indicator distinguishes "using bundled key" vs "using your key".

## [0.3.25] — 2026-04-23

### Added
- TMDB non-commercial API key now baked into the published images (`:latest` / `:nvenc` / `:edge` / `:edge-nvenc`). Fresh installs get poster artwork and native-language detection without the user having to register with TMDB first — user-saved keys in Settings still win. Key comes from the `TMDB_API_KEY` GitHub secret at build time via a `--build-arg` into both Dockerfiles; local self-builds without the secret behave as before.
- TMDB attribution in Settings → Support, per TMDB's non-commercial API terms of use.

## [0.3.24] — 2026-04-23

### Changed
- Setup wizard step order: Add directories → **Add connections** → Scan library → Start converting. TMDB connection now comes before scanning so posters and native-language detection are populated on the first scan instead of missing until the next full rescan.

### Added
- `SHRINKERR_TMDB_API_KEY` environment variable — acts as a bundled fallback TMDB key when no user key is configured. Lets image maintainers (or self-builders) ship a non-commercial key so fresh installs get posters / metadata lookups without the user having to register with TMDB first. User-saved key always wins. `tmdb_key_source` is now returned on the settings GET so the UI can distinguish user-supplied vs bundled vs absent.

## [0.3.23] — 2026-04-23

### Added
- Support section in Settings with links to the documentation, GitHub repo, and issue tracker. Surfaces the new `/docs/` tree from inside the app so users don't have to go hunting for the README.

## [0.3.22] — 2026-04-23

### Fixed
- Checkmark finally renders on every checkbox (was missing in the Settings page and in the File tree). Two scoped selectors (`.settings-page input[type="checkbox"]` and `.poster-accordion input[type="checkbox"]:checked`) were setting `background:` shorthand at higher specificity than the base `:checked` rule, silently clearing the SVG tick image. Removed the scoped background overrides — base rule now styles every checkbox uniformly.

## [0.3.21] — 2026-04-23

### Fixed
- Checked checkboxes in light mode now show their tick. The old `::after` rotated-border checkmark was brittle; replaced with an inline-SVG background image. Also fixed the light-mode `:checked` override using the `background` shorthand (which was clearing the SVG) in favour of `background-color`.

## [0.3.20] — 2026-04-23

### Changed
- Light mode overhauled. New palette tuned for WCAG AA contrast (muted text, accent and status colors darkened), buttons / filter pills / sort pills / job-type badges now render as light grey instead of stark white, codec badges use pale-tinted backgrounds with colored text instead of heavy solid blocks, and a safety rule catches components with hardcoded `color: white` inline so section labels no longer disappear. Logo wordmark swaps to a dark variant in light mode.

## [0.3.19] — 2026-04-23

### Added
- Symmetric "GPU fallback" preset + CQ fields in Settings → Video (libx265 section), matching the "CPU fallback" pair. Lets libx265-first users pin specific NVENC settings for when a GPU-capable worker picks up a libx265 job. Worker now also only forwards main NVENC defaults when `default_encoder` is nvenc — mirror of the libx265 side.

## [0.3.18] — 2026-04-23

### Added
- "CPU fallback" preset + CRF fields in Settings → Video (NVENC section). When set, they override the NVENC→libx265 translation for CPU workers — lets NVENC-first users pin a specific libx265 profile for CPU fallback without changing their primary encoder.

## [0.3.17] — 2026-04-23

### Fixed
- Server now only ships its `libx265_preset` / `libx265_crf` values to a remote worker when libx265 is the configured default encoder. NVENC-first servers were previously leaking the shipped hardcoded libx265 defaults (`medium / CRF 20`) to CPU workers, short-circuiting the NVENC→libx265 translation of the user's actual NVENC settings.

## [0.3.16] — 2026-04-23

### Fixed
- Queue page's "Starting…" placeholder cards no longer appear for paused / offline worker nodes. Capacity is now summed from nodes that can actually pick up work, instead of blindly using the global `parallel_jobs` setting.

## [0.3.15] — 2026-04-23

### Fixed
- Remote CPU worker translating an NVENC job with no per-job encoder settings now uses the server's global NVENC defaults (e.g. `p3 / CQ 27`) instead of the old hardcoded `p6 / CQ 20` fallback — the translated libx265 output matches the user's actual quality target.

## [0.3.14] — 2026-04-23

### Fixed
- "Add a remote worker" snippet on the Nodes page now references the published GHCR images (`:nvenc` for GPU, `:latest` for CPU) instead of the non-existent `shrinkerr:latest` local build tag, so the copy-paste command actually pulls something.

## [0.3.13] — 2026-04-23

### Changed
- Node settings → "NVENC ↔ libx265 comparison table" now reflects the quality-matched translation (CRF = CQ, presets capped at `slow`) so the UI matches the actual worker behaviour.

## [0.3.12] — 2026-04-23

### Changed
- NVENC→libx265 translation on CPU workers now targets similar perceptual quality instead of similar file size: CRF matches the NVENC CQ value 1:1, so libx265's extra per-bit efficiency shows up as a smaller file rather than a quality drop. A typical `p3 / CQ 27` job now runs as `libx265 veryfast / CRF 27`.

## [0.3.11] — 2026-04-23

### Fixed
- Remote CPU workers handed an NVENC job no longer pick catastrophic libx265 settings. The old translation mapped `nvenc p6 / CQ 20` to `libx265 slower / CRF 16` — a near-lossless preset 40× slower than `fast`. Workers now prefer the server's configured libx265 defaults, fall back to a conservative translation capped at `slow`, and use `CRF = CQ + 3` (matching libx265's higher per-bit efficiency, instead of the inverted sign the old code had).

## [0.3.10] — 2026-04-23

### Fixed
- VMAF analysis now handles filenames with apostrophes, spaces or brackets (e.g. "Grey's Anatomy - S01E01"). The derived log path was previously inlined raw into ffmpeg's `-filter_complex` and the apostrophe opened an unbalanced quoted region, so libvmaf exited non-zero and the score was silently dropped.

### Changed
- Remote worker nodes now honour the server's `vmaf_analysis_enabled` / `vmaf_min_score` settings (previously hardcoded off).
- VMAF failures are now surfaced on the Activity page with the ffmpeg error, not just the successful scores.
- Converter always logs its VMAF decision (`enabled=… raw setting=…`) and the exception traceback on failure, so "VMAF didn't run" is diagnosable from docker logs without rerunning.

## [0.3.9] — 2026-04-22

### Changed
- Default encoder is now auto-picked from detected hardware on first launch — CPU-only boxes land on libx265 instead of NVENC.
- Always-keep audio/subtitle languages and NZBGet/SABnzbd tags & categories no longer come pre-populated.
- Settings → Updates card now uses the logomark instead of the shrunk full logo.
- Scanner empty state no longer shows an endless "Loading files..." spinner on fresh installs.
- Setup wizard: larger logomark, reworded intro, square icon tiles, and Plex step broadened to "Add connections" (TMDB / Plex / Jellyfin / Sonarr / Radarr) deep-linking to Settings → Connections.
- Queue page's video-preset dropdown now follows the selected default encoder (libx265 preset / CRF vs NVENC preset / CQ).

### Fixed
- Dashboard no longer hangs on the loading spinner on fresh installs, so the setup wizard renders as intended.
- Loose files directly in a media root now each get their own poster card and are individually selectable, instead of being collapsed under the root folder.
- Estimate modal's "Auto" encoder now respects the saved default encoder — previously it always routed CPU/libx265 installs through the NVENC UI.
- libx265 preset override from the estimate modal is now actually applied to queued jobs (the field was being dropped).

## [0.3.8] — 2026-04-22

### Fixed
- Settings page no longer spawns a page-wide scrollbar on short viewports.

## [0.3.7] — 2026-04-22

### Changed
- README hero is now an animated WebP instead of a WebM video, so it autoplays on GitHub.

## [0.3.6] — 2026-04-22

### Fixed
- README hero video now renders for unauthenticated visitors (the previous URL was session-gated).

## [0.3.5] — 2026-04-22

### Fixed
- Attempted fix for the README hero video that didn't land (see 0.3.6).

## [0.3.4] — 2026-04-22

### Changed
- VMAF analysis is much faster on TV episodes — reverted to a 30-second sample now that 0.3.3's normalisation fixed the accuracy problem.

### Fixed
- VMAF progress bar no longer hangs at 100%; now shows fps + ETA during analysis.

## [0.3.3] — 2026-04-22

### Changed
- VMAF filter graph now normalises frame rate and colour range on both streams, so scores are accurate regardless of VFR/CFR mix or range-tag drift.

### Added
- SSIM + PSNR cross-check runs automatically on any VMAF score below 80, so you can tell a real quality regression from a VMAF measurement artefact (common on animation / flat-coloured content).

### Fixed
- VMAF no longer produces bimodal scores (e.g. sibling TV episodes scoring 49 and 96 at identical settings).
- History tab no longer labels VMAF-rejected jobs as "Converted (no savings)" — now reads "Kept original — VMAF below threshold".

## [0.3.2] — 2026-04-21

VMAF-focused release. Fixes three real-world VMAF bugs observed in
production (bimodal scores on sibling episodes, score/event lost on
rename, no in-app way to verify the score per file) and closes a
UX gap in the update-notification system so new releases surface on
running containers without a manual image pull.

### Changed
- File-detail **History** tab now always shows the VMAF score when the file has one in `scan_results`, synthesising an entry if the original VMAF file-event is missing (older conversions pre-dating the logging feature, or events logged against a pre-rename path). Makes it easy to spot-check individual files surfaced by the VMAF filters without opening the job's full encoding log.
- Update-available notification now surfaces on the running container within ~30 minutes of a new GitHub release, no manual `docker compose pull` required. Previously the server-side cache window was 6 hours AND there was no background refresher, so the "Update available" pill could take most of a day to appear — or never, if the container happened to check just before the release. Matches how Sonarr / Radarr / Plex advertise updates. The Settings → Updates "Check for updates" button now also bypasses the cache so a manual click always reflects live GitHub state.

### Fixed
- VMAF analysis: reported suspiciously low scores (e.g. 43 on visibly near-transparent encodes; back-to-back same-show episodes at 57.7 and 97.7) due to pixel-format or resolution mismatch between the reference and encoded streams inside the libvmaf filter graph. The filter now explicitly normalises both sides to 8-bit `yuv420p` and uses `scale2ref` so resolution drift can't silently break the comparison. Also caps on the shortest stream with `shortest=1` so a trailing-frame discrepancy no longer inflates error. Logs now include min / max / harmonic-mean alongside the mean score, and emit a "distribution looks bimodal" warning when a sub-80 mean coexists with a ≥90 max — a signature of measurement artefacts rather than genuine quality loss.
- VMAF score / event no longer lost on files whose name changes during conversion (e.g. `x264` → `x265` rename): the backend was writing the score + event against the pre-rename path, so the updated `scan_results` row and the file-history query — both keyed on the post-rename path — never saw them. Both writes now use the post-rename path.

## [0.3.1] — 2026-04-21

A small quality-of-life release: one visual polish item and three bug
fixes, including a real-world VMAF-threshold regression that slipped
through in 0.3.0.

### Changed
- "Update available" sidebar button now uses the designer-drawn gift icon from the Figma design-system file instead of a hand-rolled lucide-style placeholder.

### Fixed
- Scanner page: the "Advanced" search button disappeared when the filter panel was expanded. It now renders in both collapsed and expanded layouts so you can open the advanced-query panel regardless of filter state.
- Scanner page: clicking a poster card in the `Corrupt` filter view no longer shows "No files found" when the card reports a non-zero file count. Files flagged corrupt by the health check (rather than by an ffprobe failure) were missing from the file-list response due to a dropped field in the backend row-enrichment step.
- VMAF threshold rejection: the minimum-score setting in Settings → Video was being saved correctly but never read by the encoder at encode time, so encodes with scores far below the threshold (even 43 vs a threshold of 85) would be accepted. The threshold is now honoured on every encode.

## [0.3.0] — 2026-04-21

First public tagged release. Focus areas: multi-platform Docker images, GPU
detection that's actually honest about what the host can do, a real VMAF
threshold feature, and serious UI performance wins during encoding.

### Added
- **Multi-arch Docker images.** `shrinkerr:latest` and `:edge` now build for
  both `linux/amd64` and `linux/arm64` — run Shrinkerr on a Mac, Raspberry Pi,
  ARM cloud VM, Windows WSL2, or any x86 Linux host. Ships with ffmpeg n7.1
  (stable) or master (bleeding edge).
- **NVENC image variants** (`:nvenc`, `:edge-nvenc`). Separate x86_64-only
  lineage built on the CUDA base image for NVIDIA hosts that want hardware
  HEVC encoding. Documented driver requirements (525.60.13+ for `:nvenc`,
  570+ for `:edge-nvenc`) and the Monitor page surfaces your running driver
  so any mismatch is obvious.
- **VMAF minimum-score threshold.** Settings → Video → "Reject encodes below
  a minimum VMAF score". When enabled, any encode whose measured score is
  below the threshold is discarded and the original is kept. Rejected jobs
  get a distinct amber "VMAF rejected" badge on the Completed tab with the
  exact score and threshold in the expanded detail.
- **Monitor page encoding-capability strip.** Each node card now shows what
  the node can actually encode (NVENC ✓ / libx265 ✓) and, when NVENC is
  unavailable, a specific human-readable reason (missing GPU, old driver,
  ffmpeg build has no hevc_nvenc, etc.).
- **Driver version reporting.** Shows your NVIDIA driver version alongside
  the GPU name so you can correlate with the ffmpeg SDK's driver floor.
- **`runtime: nvidia` compose docs.** The portainer compose template now
  documents the Linux vs Windows/Docker-Desktop GPU-passthrough differences
  inline, with copy-pasteable instructions for both.
- **Build variants via `FFMPEG_BUILD` arg.** Power users can rebuild the
  image pinned to a specific BtbN release (`n7.1`, `n8.1`, `master`, …).
- **`scripts/build-images.sh`** — one command to build all four image tags
  locally, with optional `MULTIARCH=1` for buildx cross-compile builds.
- **`.github/workflows/build-images.yml`** — automatically publishes all
  four tags to GHCR on every push to `main` and on `v*` tag pushes.
- **Remote worker nodes** — run a second container on any host with
  `SHRINKERR_MODE=worker`, advertise its capabilities (nvenc / libx265),
  and jobs get dispatched to it with capability-aware routing.
- **Autoplaying hero video in the README** — five-screen UI tour that
  loops seamlessly.

### Changed
- **Rebrand from Squeezarr to Shrinkerr.** All user-facing strings, DB
  filename (`squeezarr.db` → `shrinkerr.db` with transparent startup
  migration), backup folder name (`.squeezarr_backup` → `.shrinkerr_backup`
  with fallback reads), environment variable prefix (`SQUEEZARR_*` →
  `SHRINKERR_*` with fallback parsing), session cookie, download filenames,
  and notification subject lines. Backward-compat shims everywhere so
  existing deployments upgrade with zero config changes.
- **Default ffmpeg pinned to BtbN n7.1** (NVENC SDK 12.2, driver 525.60.13+)
  instead of rolling `master`. Covers a much wider driver range out of
  the box. Override via `--build-arg FFMPEG_BUILD=master` for bleeding edge.
- **README rewrite** with a proper features matrix, platform compatibility
  table, workflow walkthroughs, environment variable reference, and
  troubleshooting guide.
- **Apache 2.0 license** added.

### Fixed
- **NVENC falsely detected on CPU-only hosts.** A bug in the detection code
  let `hevc_nvenc` pass the capability check on machines with no NVIDIA GPU
  at all (macOS in particular). Detection now requires `nvidia-smi` to
  succeed *before* even attempting the NVENC test encode, and trusts ffmpeg's
  return code rather than parsing stderr for failure strings.
- **Queue page at 116% CPU + 1.9 GB RAM in Chrome during active encoding.**
  Four compounding issues: a document-wide `MutationObserver`, un-throttled
  WebSocket progress broadcasts, missing `React.memo` on list rows, and
  never-paused polling. Fixed all four; typical idle Queue page is now in
  the single-digit CPU range.
- **Dashboard page at 60% CPU during encoding.** Similar pattern: every
  WebSocket progress tick forced the four Recharts SVG surfaces to redraw
  from scratch. Extracted the live-status card into a memoized child,
  memoized `chartData`, and added visibility-aware polling.
- **VMAF score missing on half of concurrent encodes.** Two jobs running in
  parallel on files that shared a 20-character filename prefix (same-series
  TV episodes, same-franchise movies) both wrote libvmaf output to the
  same `/tmp/shrinkerr_vmaf/*_vmaf.json` path, clobbering each other. Now
  suffixed with a UUID fragment — zero collisions possible.
- **"Output file missing or empty after conversion" false failures** on
  flaky NFS / SMB mounts. Added a retry loop with directory-scan fallback
  for late-landed temp files, and clear diagnostic output when the source
  is intact so a failed job doesn't trigger downstream NZBGet/Sonarr
  cascades that blocklist the release.
- **NZBGet post-processing script exiting with code 1 on unhandled Python
  exceptions.** Wrapped `main()` in a try/except that exits `POSTPROCESS_NONE`
  instead — prevents NZBGet from marking a healthy download as broken.
- **NZBGet script download endpoint 404.** Endpoint was pointing at a
  non-existent `nzbget-extension/Squeezarr/main.py` path with the wrong
  placeholder tokens. Now correctly serves the real `Shrinkerr.py` template
  with matching `__SHRINKERR_URL__` / `__SHRINKERR_API_KEY__` substitutions.
- **`runtime: nvidia` missing from production compose** caused NVENC
  passthrough to silently no-op on many Linux hosts even when the Container
  Toolkit was correctly installed. Compose templates now document both
  `runtime: nvidia` AND `deploy.resources.reservations.devices` as the
  belt-and-suspenders Linux pattern.
- **Scanner "Quick check" and "Thorough check" buttons collapsed into one
  "Health check" dropdown** — same two modes, less button clutter.

### Security
- No user-facing security fixes in this release.

---

[0.3.51]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.51
[0.3.50]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.50
[0.3.49]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.49
[0.3.48]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.48
[0.3.47]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.47
[0.3.46]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.46
[0.3.45]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.45
[0.3.37–0.3.44]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.44
[0.3.36]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.36
[0.3.35]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.35
[0.3.34]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.34
[0.3.33]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.33
[0.3.32]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.32
[0.3.31]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.31
[0.3.30]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.30
[0.3.29]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.29
[0.3.28]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.28
[0.3.27]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.27
[0.3.26]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.26
[0.3.25]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.25
[0.3.24]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.24
[0.3.23]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.23
[0.3.22]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.22
[0.3.21]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.21
[0.3.20]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.20
[0.3.19]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.19
[0.3.18]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.18
[0.3.17]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.17
[0.3.16]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.16
[0.3.15]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.15
[0.3.14]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.14
[0.3.13]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.13
[0.3.12]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.12
[0.3.11]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.11
[0.3.10]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.10
[0.3.9]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.9
[0.3.8]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.8
[0.3.7]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.7
[0.3.6]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.6
[0.3.5]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.5
[0.3.4]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.4
[0.3.3]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.3
[0.3.2]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.2
[0.3.1]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.1
[0.3.0]: https://github.com/I-IAL9000/shrinkerr/releases/tag/v0.3.0
