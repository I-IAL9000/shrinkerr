# Language detection

Media files often carry audio or subtitle tracks tagged `und`
(undetermined) — no language metadata. That breaks track cleanup: your
"keep English + Icelandic" rule can't match a track it can't identify, so
`und` tracks get dropped, or you have to hand-pick every track. Shrinkerr
detects the real language of `und` tracks and can write the correct tag
back into the file.

## Contents
- [What gets detected](#what-gets-detected)
- [How to run it](#how-to-run-it)
- [Writing tags back to the file](#writing-tags-back-to-the-file)
- [Settings](#settings)
- [The "Unknown language" filter](#the-unknown-language-filter)
- [How detection works](#how-detection-works)
- [Confidence and fail-open](#confidence-and-fail-open)
- [Dependencies and image size](#dependencies-and-image-size)
- [Troubleshooting](#troubleshooting)

## What gets detected

| Track type | Method | Speed | Runs automatically? |
|---|---|---|---|
| **Text subtitles** (SRT/ASS/…) | Text analysis (langdetect) | milliseconds | **Yes, at scan time** |
| **External subtitles** (sidecar `.srt`/`.ass`) | Text analysis (langdetect) | milliseconds | On demand |
| **External VobSub** (sidecar `.idx`/`.sub`) | OCR (subtile-ocr) | seconds/track | On demand |
| **Audio** | Spoken-language ID (faster-whisper) | seconds/track | On demand; optionally before conversion |
| **Image subtitles** (PGS, VobSub) | OCR (tesseract) | minutes/track | On demand only |

Text-subtitle detection includes **charset detection**, so subtitles in
legacy non-Unicode encodings — GB2312 / GBK / Big5 (Chinese), Shift-JIS
(Japanese), EUC-KR (Korean), Windows-1252 / Latin-1 (Western) — are decoded
correctly instead of turning into gibberish the detector can't read.

Image-subtitle OCR is multi-script: it detects Latin, Han (Chinese),
Japanese, Korean, Cyrillic, and Arabic scripts and OCRs with the matching
model.

## How to run it

**Automatic (text subtitles):** happens during a normal scan. An `und` text
subtitle gets its language filled in with no action from you.

**On demand (any track):** open a file's detail panel and click **Detect
languages**. This detects every `und` audio and subtitle track on that file
— including the slow image-sub OCR — applies the results, and (by default)
writes the tags into the file. Image-sub OCR takes minutes; a live status
line shows the current stage (extracting → OCR passes).

**Batch:** from the scanner list (e.g. filtered to
[Unknown language](#the-unknown-language-filter)), use **Detect all
unknown** to run detection across the visible files, one at a time.

**Before conversion (audio):** when the **Auto-detect languages** setting is
on (default), converting a file with `und` audio detects the language first,
so the converted output carries the correct tag. Image-sub OCR is *not* run
before conversion — it's too slow to gate an encode on.

## Writing tags back to the file

By default, on-demand detection doesn't just update Shrinkerr's database —
it writes the corrected language tags **into the file**, so the fix is real
on disk and every other tool (Plex, Jellyfin, mpv, …) sees it too. No
re-encode is involved:

- **mkv** — edited **in place** with `mkvpropedit`: instant, no rewrite, no
  extra disk space, even on a 50 GB file.
- **other containers** (mp4, …) — a fast `ffmpeg -c copy` remux to a temp
  file, then an atomic replace. No quality loss (streams are copied, not
  re-encoded), but it does rewrite the container.
- **external sidecar subs** — the language lives in the *filename*, so the
  sidecar is renamed to embed the ISO-639-2 code (`Movie.srt` →
  `Movie.eng.srt`) — the convention Plex/Jellyfin/Bazarr read. For VobSub
  the `.idx` **and** `.sub` are renamed together so the pair stays matched.
  If a same-named file already exists, the rename is skipped (no clobber).

Only tracks that were upgraded from `und` are touched; already-tagged tracks
are left alone. If the write fails for any reason, the original file is kept
and the detected language is still recorded in Shrinkerr.

If Plex is configured, a library refresh is triggered afterward so Plex
re-reads the corrected languages (see the setting below).

## Settings

Both are in **Settings → Video** (shown in the audio and subtitle sections):

- **Auto-detect languages for unknown tracks before converting**
  (default **on**) — when converting a file with `und` tracks, detect audio
  language first so the output is tagged correctly. Turn off to leave `und`
  tracks untouched through conversion.
- **Notify Plex on track-language change** (default **on**) — after tags are
  written to a file, refresh the file's Plex library section. No effect if
  Plex isn't configured.

Advanced tuning via environment variables:

| Env var | Default | Meaning |
|---|---|---|
| `SHRINKERR_LANG_DETECT_AUDIO_MIN` | `0.6` | Min confidence to accept an audio detection |
| `SHRINKERR_LANG_DETECT_SUB_MIN` | `0.7` | Min confidence to accept a subtitle detection |
| `SHRINKERR_WHISPER_CACHE` | `/app/data/models` | Where the faster-whisper model is cached |
| `SHRINKERR_WHISPER_MODEL` | `tiny` | faster-whisper model for audio ID. `tiny` is under-confident on non-English speech (Nordic tracks land just under the gate); set `base` or `small` for better accuracy at the cost of size/speed. |
| `SHRINKERR_PGS_SAMPLE_SECONDS` | `1200` | Seconds of a PGS track to OCR for detection (0 = whole track). OCR of a full movie is slow; a leading sample is enough to ID the language. Falls back to the full track if the sample has no text. |

## The "Unknown language" filter

The scanner has a dedicated **Unknown language** filter listing every title
that still has at least one `und` audio or subtitle track, with a count.
Use it to find what needs attention, then Detect-all-unknown across the
results.

## How detection works

- **Text subtitles** — the subtitle text is extracted (raw, without forcing
  a decode that would reject non-UTF-8), the real charset is detected, and
  `langdetect` identifies the language. Runs inline during scanning.
- **Audio** — a 30-second clip is extracted (16 kHz mono) and run through a
  faster-whisper *tiny* model's language identification. Several positions
  in the track are sampled and the most confident wins, so a track whose
  first sampled window is music or silence still gets identified from a
  better window.
- **Image subtitles** — the track is extracted with `mkvextract`, OCR'd with
  `pgsrip`/tesseract, and the recognized text is fed to the same
  `langdetect` step. It OCRs with the Latin model first (covers all
  Latin-script languages cleanly); if the result can't be identified — the
  usual sign the sub is non-Latin — it re-OCRs with the CJK / Cyrillic /
  Arabic models.

Before any of the above, if the track's **title** names a language
("Traditional Chinese", "Romanian", "English SDH"), that's used directly —
it's free and reliable, and it rescues **forced/SDH** tracks that carry too
little text for content detection to work. Applies to audio and subtitle
tracks alike.

In every case the recognized/identified language flows through one shared
path: confidence gate → apply to the DB → write to the file → optional Plex
refresh.

## Confidence and fail-open

Detection is deliberately cautious. A result below the confidence threshold
(see the env vars above) is discarded and the track stays `und` — a wrong
language tag is worse than none, because it makes your keep/drop rules act
on the wrong track. Every step is **fail-open**: a missing model, an OCR
failure, an extraction error, or an offline model download leaves the track
`und`, logs a warning, and never blocks a scan or conversion.

## Dependencies and image size

The detection tooling is bundled in every image:

- `langdetect` + `charset-normalizer` — text-subtitle detection (tiny).
- `faster-whisper` + `pytesseract` + `pgsrip` — audio + image-sub detection.
- `tesseract-ocr` and the language packs (`eng`, `chi-sim`, `chi-tra`,
  `jpn`, `kor`, `rus`, `ara`) plus `mkvtoolnix` and OpenCV's runtime libs.
- `pgsrip` handles PGS (Blu-ray). VobSub (DVD) OCR uses `subtile-ocr`, which
  is bundled in the **NVENC** images; on the portable/CPU images VobSub
  tracks fall back to `und` (PGS works everywhere). Detection is fail-open,
  so a missing tool never breaks a scan or conversion.

These add on the order of tens of MB to the image. The faster-whisper
**tiny** model (~75 MB) is *not* baked in — it downloads on the first audio
detection and caches in the data volume (`/app/data/models`), so it needs
outbound network the first time and a writable data volume.

## Troubleshooting

**"No new languages detected" immediately** — audio detection needs the
faster-whisper model. If it returns instantly with nothing, the model
probably failed to load or download. Check the logs for `[LANG-DETECT]`
warnings. First use needs network + a writable `/app/data/models`.

**An image sub stays `und`** — OCR of a heavily-styled or low-resolution
bitmap sub can come back below the confidence threshold; that's the gate
working (better `und` than wrong). PGS is well-supported; VobSub is more
variable and its OCR (`subtile-ocr`) is only bundled in the NVENC images —
on the portable/CPU images VobSub tracks stay `und`.

**A detected language didn't reach the file** — the write is fail-open; if
`mkvpropedit`/the remux failed, Shrinkerr keeps the DB detection but the
file is unchanged. Check the logs for `[LANG-DETECT] file write failed`.

**An audio track won't detect** — some tracks (music-only, commentary beds,
very short) genuinely don't yield a confident spoken-language result across
any sampled window, and stay `und`. Lower `SHRINKERR_LANG_DETECT_AUDIO_MIN`
if you want to accept weaker guesses (at the risk of wrong tags).

**Image-based subtitles only** support Latin/CJK/Cyrillic/Arabic scripts.
Other scripts, or non-video subtitle formats, fall back to `und`.
