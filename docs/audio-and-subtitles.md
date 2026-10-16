# Audio and subtitles

Which audio and subtitle tracks Shrinkerr keeps, by language, and what else
it can do to them — converting lossless audio, adding a stereo track for
devices that can't play surround, and turning image subtitles into text.
All of it is in **Settings → Audio & subtitles**. Tracks are removed when a
file is converted or cleaned up; the other options apply while a file is
converted (the subtitle options also when it's cleaned up), in the same
pass.

Detecting the language of unlabelled tracks is covered in
[Language detection](language-detection.md).

## Contents
- [Choosing the languages to keep](#choosing-the-languages-to-keep)
- [Lossless audio](#lossless-audio)
- [A stereo compatibility track](#a-stereo-compatibility-track)
- [Image subtitles to text (SRT)](#image-subtitles-to-text-srt)
- [Sidecar subtitles](#sidecar-subtitles)
- [Fix for direct play](#fix-for-direct-play)
- [Remote workers](#remote-workers)

## Choosing the languages to keep

A conversion or cleanup removes the tracks your languages don't keep. The
Scanner shows each file's tracks and which ones go; its **Audio cleanup**
and **Subtitle cleanup** filters list the files with something to remove.

**Audio** is the **Audio Track Rules** card. **Remove unwanted audio
tracks** is on by default; with it off, every audio track stays and the
first three options below are hidden (reordering, language detection and
Audio Conversion stay available).

- **Always Keep Languages** — audio in these languages stays.
- **Auto-keep native language audio tracks** (on) — the original language
  of the film or show stays too. It comes from TMDB (by an IMDb, TVDB or
  TMDB id in the file or folder name, else by title and year) or from a
  **Fix match**. With no match, Shrinkerr guesses: the track marked
  original, else the first audio track.
- **Keep only the best track per always-keep language** (on) — of several
  tracks in one of those languages, the one with the most channels, then
  the best codec, stays. The original language's tracks all stay while
  auto-keep native is on.
- **Reorder native language to first audio stream** (on) — a conversion or
  cleanup moves the original-language audio to the first audio track. A
  file that needs only this counts as audio cleanup.
- Audio with no language (`und`) always stays — often it's the main
  track, just untagged; [language detection](language-detection.md) can
  identify it — and so does the
  [stereo compatibility track](#a-stereo-compatibility-track). A file
  always keeps at least one audio track.

**Subtitles** is the card below it:

- **Remove unwanted subtitle tracks** is off on new installs, so every
  subtitle stays until you choose languages; the setup wizard turns it on
  when you pick subtitle languages there. While it's off, the options
  below are hidden.
- **Keep Subtitle Languages** — subtitles in these languages stay. With
  none chosen (and auto-keep native off), every subtitle with a language
  goes, forced ones included.
- **Keep unknown/undefined subtitles** (on) and **Auto-keep native
  language subtitle tracks** (off).
- Forced subtitles stay only in a keep language, the original language
  (with that switch on), or with no language. Empty placeholder subtitles
  (forced or unknown, one line at most) go.

**Changing these settings** updates the files already scanned, in the
background (after a running scan), with no rescan; pending jobs follow.
Tracks you tick or untick by hand — in a file's panel in the Scanner, or a
pending job's panel in the Queue — are marked **your choice** and stay as
you set them, through language changes, rescans and
[language detection](language-detection.md).

An encoding rule can set its own audio languages and subtitle policy (keep
all, remove all, or these languages): rule form → **Languages, originals
and more** ([Rules and automation](rules-and-automation.md#actions-and-overrides)).
They apply when files are queued; the Scanner keeps showing the global
choice.

Language codes are compared in one spelling: `de`, `deu`, `de-DE` and `ger`
are all German, so Bazarr's `.is.srt` and TMDB's 2-letter codes match your
languages.

Scanner filters for tracks: **Audio cleanup**, **Subtitle cleanup**,
**Unknown language**, **Missing my language** (no audio or subtitle track
in your languages), **Atmos / DTS:X**, **7.1**, **Commentary**, **Image
subtitles**, **External**, **Forced**, **SDH** and **Hi10P**.

## Lossless audio

**Auto-convert lossless audio** turns lossless tracks — DTS-HD MA,
TrueHD, PCM, FLAC — into a lossy codec (EAC3 by default, at the bitrate
you pick) while the video is converted. They're often the largest part of
a Blu-ray remux after the video. Lossy tracks are left alone.

- **Keep Atmos and DTS:X tracks lossless** (on by default). Converting
  them keeps the 5.1 / 7.1 channels but loses the height and object
  information, so they stay as they are unless you turn this off.
- **Also convert lossy DTS.** Plain DTS (core) is lossy, but many TVs and
  streaming clients can't play it and transcode it. With this on it's
  converted along with the lossless tracks.

## A stereo compatibility track

**Add a stereo compatibility track** keeps the original audio and adds a
2-channel track made from the main one — for TVs, phones and browsers that
can't play surround or DTS / TrueHD and would otherwise make Plex or
Jellyfin transcode.

- It's only added when the main track is surround and there isn't already
  a stereo track in its language.
- Codec AAC, EAC3 or AC3, at the bitrate you choose; it's titled
  "Stereo (compatibility)" and isn't the default track.
- **Even out loudness (EBU R128)** brings quiet dialogue and loud effects
  closer together on that track — useful for late-night or TV-speaker
  listening. The original track is never changed.
- Rescans always keep the compatibility track, whatever the language rules
  say.

## Image subtitles to text (SRT)

Blu-ray (PGS) and DVD (VobSub) subtitles are pictures. Many players can't
show them and transcode the video to burn them in. **Convert image
subtitles to text (SRT)** reads them with OCR (tesseract) and adds an SRT
track in the same language, titled "OCR" (or the image track's title plus
"(OCR)"), next to each one.

- **Which tracks:** the image subtitle tracks the file keeps, unless it
  already has a text track in that language (forced and full subtitles are
  told apart). Converting a file again doesn't read them twice.
- **When:** while a file is converted, or cleaned up. A file that needs
  nothing else can still be done: filter the Scanner by **Image
  subtitles**, select and **Add to Queue**.
- **Keep the image subtitles too** is on by default — OCR isn't perfect.
  Turn it off to replace them with the SRT tracks.
- **Languages:** each language needs its OCR language pack. The images
  include English, French, German, Spanish, Italian, Portuguese, Dutch,
  Swedish, Danish, Norwegian, Finnish, Icelandic, Polish, Czech,
  Hungarian, Turkish, Greek, Hebrew, Russian, Arabic, Chinese (simplified
  and traditional), Japanese and Korean. A track in another language
  stays as it is (the log says so); an unlabelled one is read in English.
- **Time:** a few minutes per track — the whole track is read. The job
  shows "Reading image subtitles (OCR)…" meanwhile.
- Discs (VIDEO_TS / BDMV folders and ISOs) aren't included.

## Sidecar subtitles

**Merge external subtitles** puts `.srt`, `.ass`, `.ssa`, `.sub` and
`.vtt` files beside the video into the MKV during a conversion or cleanup,
with the language and forced flag from the file name
(`Movie.eng.srt`, `Movie.en.forced.srt`). **Delete external subtitle files
after merge** removes the sidecars once they're in.

Full language names (`Movie.English.srt`), region or script tags
(`Movie.pt-BR.srt`, `Movie.zh-Hans.srt`) and a flag before the language
(`Movie.forced.en.srt`) are read too.

With Bazarr, **Wait after an import** (Settings → Automation, 0 minutes by
default) lets its subtitles arrive before a new file converts, and
Shrinkerr asks Bazarr to re-sync after it rewrites a file, so Bazarr doesn't
download merged subtitles again
([Bazarr and Maintainerr](rules-and-automation.md#bazarr-and-maintainerr)).

## Fix for direct play

Some files make Plex, Jellyfin and Emby transcode on many TVs and
streamers, even when the server could just send them. The Scanner's
**Direct play** filters find them:

| Filter | What it finds | The fix |
|---|---|---|
| Hi10P | 10-bit H.264, which almost no hardware decoder plays | converted (to HEVC) |
| Image subtitles only | a PGS or VobSub subtitle with no text subtitle in its language; shown, it's burned into the picture | read into SRT with OCR, next to the original |
| No widely supported audio | no AAC, AC3, E-AC3 or MP3 track in the main audio language (DTS or TrueHD only, say) | a stereo compatibility track is added |
| Old container | AVI, WMV, MPEG, TS and other old containers | remuxed to MKV |

Select files (or folders), click **Add to queue** and tick **Fix for direct
play**. It shows how many of the selected files have each problem; untick a
fix to leave it out (all four are ticked to start with). Each file that
needs a chosen fix gets it in its job: Hi10P files are converted, the others
are remuxed to MKV without re-encoding the video, and the job reads image
subtitles into SRT or adds the compatibility track where those were chosen,
whatever the settings above say. The other files are queued as usual.
Fixing Hi10P means converting, so it's left out with **Cleanup only**.

**Fix for direct play when converting** (Settings → Video) ticks it in Add
to Queue, and conversions queued another way (new downloads, policies) also
make the fixes their files need.

Rules and the conversion filters (minimum size, bitrate limits) don't stop
a fix. A file still hardlinked by a torrent client, or in a Maintainerr
collection, is left out while **Skip files that are still hardlinked**
(Settings → Automation) or **Don't queue titles Maintainerr is about to
remove** (Settings → Integrations) is on.

## Remote workers

Workers get all of these settings with each job from the server, and the
worker images include the same OCR tools and language packs.
