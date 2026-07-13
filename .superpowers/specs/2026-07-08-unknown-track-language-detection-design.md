# Unknown-Track Language Detection Design Spec

**Version target:** v0.8.0 (new feature — first minor bump of the 0.8 line)
**Status:** Design — awaiting implementation plan
**Decisions locked via Q&A:** audio + text subtitles (no image-sub OCR); on-demand + pre-conversion triggers; faster-whisper lib bundled with model downloaded on first use; auto-apply above confidence threshold; pre-conversion detection gated by a default-on setting

---

## Background

Tracks whose language Shrinkerr can't determine from container tags / IFO /
mpls / libbluray are marked `und` (ISO 639-2 "undetermined"). Today `und`
tracks stay `und` forever — the user's `always_keep_languages` filter can't
match them, so they either get dropped at conversion or force manual track
selection. The v0.7.29/30 work made disc conversions *write* detected
languages to output, but only for languages Shrinkerr already knew; genuinely
unknown tracks remained unaddressed.

This feature detects the actual language of `und` tracks:
- **Text subtitles** (srt/ass/subrip): read the text, run a language detector.
- **Audio**: extract a short clip, run spoken-language ID (Whisper-class model).

**Deferred to v0.8.1 (fast-follow): image-based subtitles** (PGS / VobSub /
dvd_subtitle). Detecting those needs OCR (tesseract + per-language packs) and a
bitmap-extraction pipeline (PGS → PNG frames → OCR → aggregate; VobSub is a
separate pipeline). It's the heaviest and finickiest of the three track types,
and its marginal value is lower — an und image-sub almost always sits on a disc
whose audio + TMDB `original_language` already reveal the languages (both
covered by v0.8.0). v0.8.1 will add it as an **additive module reusing the exact
detect→gate→apply→trigger infrastructure built here**: an OCR front-end produces
noisy text (good enough for langdetect, which tolerates OCR noise given volume),
then the existing `detect_subtitle_language` + confidence gate + apply path
handle the rest. Until v0.8.1 ships, image subs stay `und`. Designing v0.8.0's
subtitle path to be codec-agnostic at the apply layer (it already keys on
`language_source="detected"`, not codec) means v0.8.1 is purely additive.

## Approach summary

| Track type | Detector | Cost | Runs at scan? |
|---|---|---|---|
| Text subtitle | `langdetect` (pure-Python) | ~ms/track | Yes (cheap) |
| Audio | `faster-whisper` tiny, language-ID only | ~2-4s/track | No (on-demand + pre-conversion) |
| Image subtitle | — (out of scope) | — | Never |

## Module layout

New file `backend/language_detection.py` — two isolated, independently-testable
entry points plus small helpers:

```python
def detect_subtitle_language(text: str) -> tuple[str | None, float]:
    """Detect language of subtitle text. Returns (ISO 639-2 code, confidence
    0-1), or (None, 0.0) if detection fails or confidence is below threshold.
    langdetect returns ISO 639-1; map to 639-2 via the existing
    scanner._ISO639_1_TO_2 table."""

async def detect_audio_language(file_path: str, stream_index: int) -> tuple[str | None, float]:
    """Extract a 30s clip from ~33% into the file (mirrors the VMAF sampling
    pattern) via ffmpeg to 16 kHz mono PCM, feed to faster-whisper's
    language-ID (no full transcription). Returns (ISO 639-2 code,
    probability), or (None, 0.0) on failure / low confidence."""
```

The module owns: the ffmpeg clip-extraction command, the faster-whisper model
load (lazy, cached module-global so the model loads once per process), the
confidence gating, and the 639-1→639-2 mapping for the subtitle path.

## Confidence gating

Detection returns a confidence/probability. Below a threshold, the track stays
`und` rather than getting a wrong guess written to it (a wrong language is worse
than `und` — it makes `always_keep_languages` keep/drop the wrong track).

- Audio threshold: `0.6` (faster-whisper language-token probability).
- Subtitle threshold: `0.7` (langdetect confidence ratio).

Both tunable via env vars `SHRINKERR_LANG_DETECT_AUDIO_MIN` /
`SHRINKERR_LANG_DETECT_SUB_MIN`. Above threshold → apply; below → leave `und`.

## Applying results

When detection produces a language above threshold, it is **auto-applied**
(no confirmation step): the track's `language` in `audio_tracks_json` /
`subtitle_tracks_json` is overwritten, `native_language` is re-derived, and
`language_source` is set to a new value **`"detected"`** (alongside the existing
`"heuristic"` / `"api"`). Persisted to `scan_results` so the UI shows the
detected language and track-selection filters work afterward. Same columns the
v0.6.5 / v0.7.6 backfills already update.

## Triggers

### 1. On-demand (manual)

- **Per-file button** — "Detect languages" on any file with ≥1 `und` track.
  Runs subtitle + audio detection for that file's `und` tracks, applies
  results, returns the updated track list.
- **Batch action** — "Detect all unknown" over the current filtered set.
  Iterates files with `und` tracks, one at a time (audio detection is
  sequential — no parallel model inference), with progress via the existing
  websocket channel.

### 2. Pre-conversion hook (default-on setting)

New setting **`auto_detect_languages`** (boolean, **default `true`**), surfaced
in Settings → Video (or Encoding) as **"Auto-detect languages for unknown
tracks before converting"**.

When enabled, `convert_file` — before building the ffmpeg command — checks the
file's tracks for `und`. If any, it runs detection on those tracks and patches
the languages in-memory (and persists to `scan_results`). The detected
languages then flow into the v0.7.29/30 output-metadata injection, so the
converted file carries correct `-metadata:s:a/s:N language=` tags and the
track-keep/drop logic selects correctly. You're already paying the encode cost,
so detection here is a small marginal add on the files you actually convert —
no wasted compute scanning the whole library.

When disabled, conversion behaves as today (und tracks stay und).

### 3. Text subtitles at scan (automatic, cheap)

The scanner's existing subtitle classification path calls
`detect_subtitle_language` for any `und` **text** subtitle it encounters
(codec in the text-sub set: subrip/srt/ass/ssa/webvtt). Milliseconds per track,
so it runs inline during the normal scan with no user action. Audio is NOT
touched at scan time — too expensive.

## Surfacing

New column `has_und_tracks_flag` (INTEGER, default 0) on `scan_results`, set at
write time when any audio or subtitle track is `und`. A filter surfaces these
titles so unknown-language content is findable — folded into the existing
audio-cleanup filter view (which already uses `has_removable_tracks_flag`), so
titles needing attention (removable tracks OR unknown languages) appear
together. Existing rows backfill the flag on the next scan / watcher cycle.

## Packaging & dependencies

- Add to `requirements.txt`: `faster-whisper` and `langdetect`.
- The Whisper **tiny** model (~75 MB, sufficient for language ID) is NOT bundled
  in the image. It downloads on first audio-detection use and caches in the data
  volume (`/app/data/models/` or faster-whisper's default cache pointed there).
- CUDA-accelerated automatically on the NVENC images (CUDA runtime present);
  CPU elsewhere. A 30s clip is ~2-4s to language-ID either way with tiny.
- No new apt packages; ffmpeg (already present) does the clip extraction.

## Error handling

- Detection is **fail-open**: any exception (ffmpeg extract fails, model load
  fails, no network for first download, corrupt clip) logs a `[LANG-DETECT]`
  warning and leaves the track `und`. Never blocks a scan or conversion.
- The pre-conversion hook wraps detection in try/except so a detection failure
  never fails the conversion — it proceeds with whatever languages it has.
- Model download failure (offline first-use) logs once and disables audio
  detection for the session; subtitle detection (no model) still works.

## Testing

- **Subtitle detection**: unit tests with real multi-language sample strings
  (English, German, Spanish, French, Japanese) asserting correct 639-2 output
  and that low-confidence/garbage input returns `(None, 0.0)`.
- **639-1→639-2 mapping**: unit test the mapping helper.
- **Audio detection**: unit test with `faster-whisper` mocked — assert the
  ffmpeg clip-extraction command is well-formed (correct `-ss`/`-t`/`-ac 1`
  /`-ar 16000`), that the model result is mapped to 639-2, and that the
  confidence gate leaves low-probability results as `und`. No model download
  in CI.
- **Confidence gating**: unit tests at/above/below both thresholds.
- **Fail-open**: unit test that an exception in detection returns `und` and
  doesn't propagate.

## File changes (anticipated)

- Create: `backend/language_detection.py`
- Modify: `backend/scanner.py` — call subtitle detection for und text subs in
  the classification path; set `has_und_tracks_flag` at write.
- Modify: `backend/converter.py` — pre-conversion detection hook gated on
  `auto_detect_languages`.
- Modify: `backend/database.py` — `has_und_tracks_flag` column migration.
- Modify: `backend/routes/scan.py` (or jobs.py) — on-demand + batch endpoints;
  fold `has_und_tracks_flag` into the audio-cleanup filter.
- Modify: `backend/routes/settings.py` + frontend — the `auto_detect_languages`
  toggle.
- Frontend: "Detect languages" button + batch action + filter surfacing.
- `requirements.txt`: `faster-whisper`, `langdetect`.
- Tests: `backend/tests/test_language_detection.py`.

## Acceptance criteria

1. A file with an `und` English srt, after scan, shows the subtitle as `eng`
   with `language_source="detected"`.
2. Clicking "Detect languages" on a file with an `und` audio track extracts a
   clip, runs the model, and (above threshold) sets the real language.
3. With `auto_detect_languages` on, converting a file with `und` tracks
   produces output whose audio/subtitle streams carry the detected language
   tags (via the v0.7.29/30 injection).
4. With `auto_detect_languages` off, conversion leaves und tracks und.
5. A low-confidence / garbage detection leaves the track `und` (no wrong guess).
6. Detection failure (ffmpeg error, offline model download) never fails the
   scan or conversion — track stays `und`, warning logged.
7. Image-based subtitles (PGS/VobSub) are never touched in v0.8.0 — stay `und`
   (added in the v0.8.1 fast-follow).
8. Titles with any `und` track appear in the audio-cleanup filter view.

## Open questions / deferred

- **Whisper model size**: tiny is the default (fast, sufficient for LID). A
  future setting could allow base/small for higher accuracy on hard cases —
  deferred, not in v0.8.0.
- **Parallel audio detection** in the batch action: sequential for v0.8.0
  (single model instance, avoid VRAM/RAM contention). Could parallelize later.
- **Re-detecting existing und rows in bulk**: a one-shot backfill (like v0.6.5)
  could sweep existing und text subs. Deferred — the pre-conversion hook and
  on-demand action cover the immediate need; a backfill can come later if users
  want their whole library re-tagged at once.
- **Image-sub OCR (v0.8.1)**: see the "Deferred to v0.8.1" note in Background.
  v0.8.0's apply layer is codec-agnostic (keys on detection result, not track
  codec) specifically so v0.8.1 is purely additive — an OCR front-end feeding
  the existing subtitle detection path. Gets its own spec when v0.8.0 lands.
