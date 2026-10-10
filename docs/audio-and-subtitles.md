# Audio and subtitles

What Shrinkerr can do to a file's audio and subtitle tracks besides keeping
or removing them by language — converting lossless audio, adding a stereo
track for devices that can't play surround, and turning image subtitles
into text. All of it is in **Settings → Audio & subtitles** and happens
while a file is converted or cleaned up, in the same pass.

Choosing which languages to keep, and detecting the language of unlabelled
tracks, are covered in [Language detection](language-detection.md).

## Contents
- [Lossless audio](#lossless-audio)
- [A stereo compatibility track](#a-stereo-compatibility-track)
- [Image subtitles to text (SRT)](#image-subtitles-to-text-srt)
- [Sidecar subtitles](#sidecar-subtitles)
- [Fix for direct play](#fix-for-direct-play)
- [Remote workers](#remote-workers)

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
track in the same language, titled "OCR", next to each one.

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
(`Movie.eng.srt`, `Movie.en.forced.srt`). **Delete external subtitles
after merge** removes the sidecars once they're in.

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

Select files (or folders) and click **Fix for direct play**. Each file with
a problem gets one job: Hi10P files are converted, the others are remuxed
to MKV without re-encoding the video. Either way the job reads image
subtitles into SRT and adds the compatibility track where the audio needs
one, whatever the settings above say. Files with no problem are left alone.

Rules and the conversion filters (minimum size, bitrate limits) don't stop
a fix. A file still hardlinked by a torrent client, or in a Maintainerr
collection, is left out unless you override them.

## Remote workers

Workers get all of these settings with each job from the server, and the
worker images include the same OCR tools and language packs.
