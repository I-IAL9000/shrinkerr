# Coming from Tdarr or Unmanic

Shrinkerr does one job — make a library smaller by converting video to
HEVC (or AV1) and removing tracks you don't want — with the steps most
Tdarr flows and Unmanic plugin stacks are built from already built in. This
page maps what you have to where it lives in Shrinkerr.

## Before you start

- **Stop the other tool processing the same folders.** Two tools converting
  the same files will fight over them. Pause its libraries (or its nodes /
  workers) before Shrinkerr's first scan.
- **Nothing is imported.** Shrinkerr doesn't read Tdarr's or Unmanic's
  settings or history; you set it up from scratch, which takes a few
  minutes.
- Files the other tool already converted to HEVC aren't converted again,
  unless HEVC is among the codecs you convert from (Settings → Video).
- **Torrents still seeding** are hardlinks: converting one frees nothing.
  New installs skip them (**Skip files that are still hardlinked**,
  Settings → Automation), and the Scanner's **Hardlinked (seeding)** filter
  shows them.

## Where things are

| Tdarr | Unmanic | Shrinkerr |
|---|---|---|
| Libraries | Libraries | **Media folders** (Settings → Media). Narrow them further with **saved views** (Scanner → Views). |
| Flows, transcode plugins | Plugins | **Settings** for what applies everywhere (encoder, quality preset, audio and subtitle languages, lossless audio, subtitles to SRT), and **encoding rules** for what differs by folder, source, resolution, content type or tag (Settings → Automation → Rules; start from the **rule library**). |
| Filters ("skip if HEVC") | "Ignore files with these codecs" | **Convert from** (Settings → Video), and the rule actions **Skip entirely** and **Skip conversion (audio/sub cleanup only)**. |
| Health check | Health check plugins | **Health check** (Scanner → select → Health check), and an optional check after each conversion. |
| Nodes | Workers, remote installations | The built-in worker plus **remote workers** (Nodes page). |
| Transcode cache | Cache path | None: the output is written next to the file (`.converting.mkv`) and swapped in when it passes the checks. |
| Staging / approve | — | **Review before replace** (Settings → Automation). |
| Scheduler | Scheduler | **Schedule** (run hours, quiet hours) and **policies** (e.g. "queue up to 200 GB each night"). |
| Folder watching | Library scanning, event monitor | The **watcher** (new files every few minutes), Sonarr / Radarr's **Connect → Webhook**, and the NZBGet / SABnzbd scripts. |

## Setting up

1. **Install** Shrinkerr with the same media paths mounted
   ([Installation](installation.md)).
2. **Set up sign-in, then run the setup wizard.** Shrinkerr first asks how
   people sign in: a username and password, or your reverse proxy
   (External). Download clients, Sonarr / Radarr and remote workers use the
   API key. The wizard then covers media folders, hardware, a quality
   preset, the languages to keep, and what happens to originals. Keep
   originals for a few days at first. **Doctor** (in the sidebar) then
   checks the media folders, ffmpeg and your encoder, and the connections.
3. **Recreate per-library differences as rules**: an anime folder in AV1,
   higher quality for 4K, leave remuxes' originals for 30 days… The rule
   library has a few to start from.
4. **Scan**, then look at the Scanner's estimate. **Queue a small batch**
   and check the results: the VMAF score and the before / after frames in
   each job's details (Queue → Completed).
5. **Then automate**: auto-queue new files, Sonarr / Radarr's webhook,
   policies for the backlog.

## What works differently

- **One pipeline, not a plugin chain.** Every conversion does the same
  steps in one pass: the video encode, track removal, lossless audio
  conversion, subtitle merging and OCR, a stereo compatibility track if you
  want one. Rules change the settings, not the steps.
- **Quality is checked.** With VMAF on, a conversion that scores below your
  minimum is thrown away and the original kept; an output that came out
  larger is thrown away too. Originals can be kept for a number of days
  ([Best practices](best-practices.md)).
- **Sonarr, Radarr and Bazarr are told** about renamed and rewritten files,
  so they don't search for them again.
- **HDR and Dolby Vision**: HDR10 and HLG stay HDR; Dolby Vision profiles 7
  and 8 keep their Dolby Vision, and profile 5 isn't converted
  ([Encoding guide](encoding-guide.md#hdr-and-dolby-vision)).
- **Files that play everywhere**: the Scanner's Direct play filters find
  what makes media servers transcode, and **Fix for direct play** sorts it
  out ([Audio and subtitles](audio-and-subtitles.md#fix-for-direct-play)).
