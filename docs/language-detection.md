# Language detection

Media files often carry audio or subtitle tracks tagged `und`
(undetermined) — no language metadata. That breaks track cleanup: your
"keep English + Icelandic" rule can't match a track it can't identify, so
`und` tracks stay whatever their language, or you have to hand-pick every
track. Shrinkerr
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

Image-subtitle OCR is multi-script: it reads the track with the English
(Latin) model first and, when that text isn't a recognisable language,
again with the Chinese, Japanese, Korean, Russian (Cyrillic) and Arabic
models. (Before v0.10.0 the second pass of a Blu-ray PGS track still used
the English model, so those tracks stayed unknown.)

## How to run it

**Automatic (text subtitles):** happens during a normal scan. An `und` text
subtitle gets its language filled in with no action from you.

**On demand (any track):** open a file's detail panel and click **Detect
languages**. This detects every `und` audio and subtitle track on that file
— including the slow image-sub OCR — applies the results, and writes the
tags into the file. Image-sub OCR takes minutes; a live status
line shows the current stage (extracting → OCR passes).

**Batch:** from the scanner list filtered to
[Unknown language](#the-unknown-language-filter), use **Detect all
unknown**, or select titles and use **Detect selected (N)**. The batch runs
server-side with a live **N/total** progress indicator that survives
navigating away and back, and a **Cancel** button (stops after the current
file).

**Before conversion (audio):** when the **Auto-detect languages** setting is
on (default), converting a file with `und` audio detects the language first,
so the converted output carries the correct tag. Image-sub OCR is *not* run
before conversion — it's too slow to gate an encode on.

**Blu-ray / DVD folders and ISOs:** spoken-language detection reads them
too (since v0.10.0). A disc can't be tagged, so the language is applied
when it's converted: a disc folder's panel offers **Convert to MKV (apply
language)**.

## Writing tags back to the file

On-demand detection doesn't just update Shrinkerr's database —
it writes the corrected language tags **into the file**, so the fix is real
on disk and every other tool (Plex, Jellyfin, mpv, …) sees it too. No
re-encode is involved:

- **mkv** — edited **in place** with `mkvpropedit`: instant, no rewrite, no
  extra disk space, even on a 50 GB file.
- **mp4 / mov** — a fast `ffmpeg -c copy` remux to a temp file, then
  swapped in for the original. No quality loss (streams are copied, not
  re-encoded), but it does rewrite the container. The tag is verified present
  in the output before the replace.
- **containers with no per-track language field** (avi, mpg, wmv, flv, …;
  m4v too, which ffmpeg can't rewrite reliably) — these *cannot* be tagged
  in place, so the write is skipped and the track stays flagged as unknown,
  with the detected language kept as pending. The only real fix is
  remuxing to mkv: the file's panel offers **Remux to MKV (apply
  language)**, a stream copy (no re-encode).
- **ts / m2ts / mts** — on-demand detection skips these; convert them to
  MKV first.
- **external sidecar subs** — the language lives in the *filename*, so the
  sidecar is renamed to embed the ISO-639-2 code (`Movie.srt` →
  `Movie.eng.srt`) — the convention Plex/Jellyfin/Bazarr read. For VobSub
  the `.idx` **and** `.sub` are renamed together so the pair stays matched.
  If a same-named file already exists, the rename is skipped (no clobber).

Only tracks that were upgraded from `und` are touched; already-tagged tracks
are left alone. **A track leaves the Unknown-language filter only once the tag
is verified in the file** — if the write can't persist (unsupported container,
read-only mount, error), the track stays flagged so you never lose track of it
while the file and Plex still read `und`.

The file's tracks are then sorted again with the new languages; tracks you
kept or removed by hand stay as you set them
([Choosing the languages to keep](audio-and-subtitles.md#choosing-the-languages-to-keep)).

If Plex is configured, a library refresh is triggered afterward so Plex
re-reads the corrected languages. A **batch** run coalesces this into one full
refresh per affected library section at the end (see the setting below).

## Settings

These are in **Settings → Audio & subtitles**, in the **Audio Track Rules**
card (the auto-detect toggle is also shown under Subtitles):

- **Auto-detect languages for unknown tracks before converting**
  (default **on**) — when converting a file with `und` tracks, detect audio
  language first so the output is tagged correctly. Turn off to leave `und`
  tracks untouched through conversion.
- **Audio detection model** (`tiny` / `base` / `small` / `medium` /
  `large-v3`, default `tiny`) — the faster-whisper model for spoken-language
  ID. Larger is more accurate on non-English speech at the cost of download
  size, RAM, and speed; `medium` and `large-v3` are practical only with an
  NVIDIA GPU (the NVENC image). Takes effect on the next detection (no
  restart).
- **Audio / Subtitle confidence** (defaults `0.6` / `0.7`) — minimum
  confidence to accept a detected language. Lower tags more tracks but risks
  wrong tags.

The Plex refresh after tags are written (default **on**; no effect if Plex
isn't configured) has no switch in Settings: it's the
`plex_notify_on_lang_change` setting (`PUT /api/settings/encoding`).

The env vars below still work and **override** the Settings values (handy for
headless/compose-only deploys):

| Env var | Default | Meaning |
|---|---|---|
| `SHRINKERR_LANG_DETECT_AUDIO_MIN` | `0.6` | Min confidence to accept an audio detection |
| `SHRINKERR_LANG_DETECT_SUB_MIN` | `0.7` | Min confidence to accept a subtitle detection |
| `SHRINKERR_WHISPER_CACHE` | `/app/data/models` | Where the faster-whisper model is cached |
| `SHRINKERR_WHISPER_MODEL` | `tiny` | faster-whisper model for audio ID. `tiny` is under-confident on non-English speech (Nordic tracks land just under the gate); set `base` or `small` for better accuracy at the cost of size/speed. |
| `SHRINKERR_WHISPER_IDLE_SECONDS` | `300` | Seconds the audio-detection model stays loaded after its last use; then it's unloaded to free RAM (and GPU memory) until the next detection. |
| `SHRINKERR_PGS_SAMPLE_SECONDS` | `1200` | Seconds of a PGS track to OCR for detection (0 = whole track). OCR of a full movie is slow; a leading sample is enough to ID the language. Falls back to the full track if the sample has no text. |

## The "Unknown language" filter

The scanner has a dedicated **Unknown language** filter listing every title
that still has at least one `und` audio or subtitle track, with a count.
Use it to find what needs attention, then Detect-all-unknown across the
results. **Ignored titles are included** — an ignore rule means "don't
convert", not "hide that the audio is untagged" — so bulk detection reaches
them too.

## How detection works

- **Text subtitles** — the subtitle text is extracted (raw, without forcing
  a decode that would reject non-UTF-8), the real charset is detected, and
  `langdetect` identifies the language. Runs inline during scanning, on new
  and changed files: a full scan reuses what it read last time for a file
  whose size and date haven't changed (a folder rescan reads everything
  again).
- **Audio** — a 30-second clip is extracted (16 kHz mono) and run through
  the faster-whisper model's language identification (*tiny* unless you
  pick another). Several positions
  in the track are sampled and the most confident wins, so a track whose
  first sampled window is music or silence still gets identified from a
  better window. In the NVENC image it runs on the NVIDIA GPU (on the CPU
  without one); the other images use the CPU.
- **Image subtitles** — the track is extracted (`ffmpeg` / `mkvextract`),
  OCR'd with `pgsrip`/tesseract (PGS) or `subtile-ocr` (VobSub), and the
  recognized text is fed to the same
  `langdetect` step. It OCRs with the Latin model first (covers all
  Latin-script languages cleanly); if the result can't be identified — the
  usual sign the sub is non-Latin — it re-OCRs with the CJK / Cyrillic /
  Arabic models.

On demand, before any of the above, if the track's **title** names a language
("Traditional Chinese", "Romanian", "English SDH"), that's used directly —
it's free and reliable, and it rescues **forced/SDH** tracks that carry too
little text for content detection to work. Applies to audio and subtitle
tracks alike.

On demand, the recognized/identified language then flows through one shared
path: confidence gate → write to the file → apply to the DB → optional Plex
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
- `tesseract-ocr` and the language packs detection uses (`eng`, `chi-sim`,
  `chi-tra`, `jpn`, `kor`, `rus`, `ara`; the images include more, for
  [image subtitles to text](audio-and-subtitles.md#image-subtitles-to-text-srt))
  plus `mkvtoolnix` and OpenCV's runtime libs.
- `pgsrip` handles PGS (Blu-ray). VobSub (DVD) OCR uses `subtile-ocr`, which
  is bundled in every image too (before v0.10.0, only in the NVENC images).
  Detection is fail-open, so a missing tool never breaks a scan or
  conversion.

These add on the order of tens of MB to the image. The faster-whisper
**tiny** model (~75 MB) is *not* baked in — it downloads on the first audio
detection and caches in the data volume (`/app/data/models`), so it needs
outbound network the first time and a writable data volume.

## Troubleshooting

**"No new languages resolved" immediately** — audio detection needs the
faster-whisper model. If it returns instantly with nothing, the model
probably failed to load or download. Check the logs for `[LANG-DETECT]`
warnings. First use needs network + a writable `/app/data/models`.

**An image sub stays `und`** — OCR of a heavily-styled or low-resolution
bitmap sub can come back below the confidence threshold; that's the gate
working (better `und` than wrong). PGS is well-supported; VobSub is more
variable.

**A detected language didn't reach the file** — the write is fail-open; if
`mkvpropedit`/the remux failed, the file is unchanged and the track stays
`und`, with the detected language kept as pending (the file's panel offers
**Remux to MKV (apply language)**). Check the logs for `[LANG-DETECT]`
lines about `mkvpropedit` or the remux.

**An audio track won't detect** — some tracks (music-only, commentary beds,
very short) genuinely don't yield a confident spoken-language result across
any sampled window, and stay `und`. Lower `SHRINKERR_LANG_DETECT_AUDIO_MIN`
if you want to accept weaker guesses (at the risk of wrong tags).

**Image-based subtitles only** support Latin/CJK/Cyrillic/Arabic scripts.
Other scripts, or non-video subtitle formats, fall back to `und`.
