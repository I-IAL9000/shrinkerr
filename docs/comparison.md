# Shrinkerr compared with Tdarr, Unmanic and FileFlows

All four convert a media library to save space. They differ in how you set
them up, what's built in, and what they cost. This page is as of October
2026; the other projects change too, so check their sites.

## At a glance

| | Shrinkerr | Tdarr | Unmanic | FileFlows |
|---|---|---|---|---|
| **Source code** | Open ([Apache-2.0](../LICENSE)) | Closed (V2) | Open (GPL-3.0) | Closed |
| **Cost** | Free, no paid tier | Free; some node features in the paid Pro tier ([docs](https://docs.tdarr.io/docs/nodes/nodes/)) | Free; supporters get more libraries and linked installs ([docs](https://docs.unmanic.app/docs/using_unmanic/supporter_features)) | Free tier with one node; paid plans for more ([pricing](https://fileflows.com/pricing)) |
| **How you set it up** | Settings for everything, rules for what differs by folder or file | Flows or plugin stacks | A plugin pipeline per library | Visual flows |

## How Shrinkerr is different

**One pipeline instead of building your own.** Tdarr's flows, Unmanic's
plugins and FileFlows' flows can do almost anything, and you assemble what
they do. Shrinkerr does one job, and the steps a conversion needs are built
in and always run in the same order: encode, remove tracks, check, replace.
You choose settings, and rules change them for some files. Less to set up,
less you can change.

**Checks before anything is replaced.** A new file replaces the original
only if it's complete, still HDR when the original was, smaller, and (with
VMAF on) good enough. Originals can be kept for a number of days, and Undo
puts one back ([What happens to your files](what-happens-to-your-files.md)).

**Languages.** Shrinkerr knows each title's original language from TMDB, so
"keep English and the original language" keeps the Japanese audio of a
Japanese film. Tracks tagged with no language are identified, by listening
to the audio (Whisper), reading image subtitles (OCR) or the subtitle text,
and the language is written into the file.

**Built in, without plugins:**

- Sonarr, Radarr and Bazarr are told about converted and renamed files;
  Plex, Jellyfin and Emby rescan.
- Disc rips (`BDMV`, `VIDEO_TS`, `.iso`) convert to one file.
- Dolby Vision profiles 7 and 8 keep their Dolby Vision.
- Hardlinked files (still seeding) are skipped.
- Image subtitles convert to SRT.
- Encodes pause while Plex, Jellyfin or Emby is streaming.
- A savings estimate before you queue anything.
- Remote workers without limits.

## When another tool may suit you better

- **You want to build your own pipeline**, with steps Shrinkerr doesn't
  have: Tdarr and FileFlows, whose flows can do things Shrinkerr's fixed
  steps can't.
- **Files other than video**: FileFlows works on any kind of file.
- **Encoding nodes that run natively on Windows**: Tdarr's do. Shrinkerr's
  remote workers run in Docker (on Windows, with WSL2).
- **A large, long-standing community** with years of plugins and answers:
  Tdarr and Unmanic.

Coming from one of them? [Coming from Tdarr or Unmanic](migrating.md) maps
their concepts to Shrinkerr's.
