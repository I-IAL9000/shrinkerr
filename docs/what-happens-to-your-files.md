# What happens to your files

Shrinkerr rewrites files in your library, so this page says exactly what it
does to them, in what order, and what it does when something goes wrong.

## While a file converts

The new file is written beside the original, as `<name>.converting.mkv`
(`<name>.remuxing.mkv` for jobs that only remove tracks). A file with many
subtitle tracks to remove first gets a `<name>.stripped.mkv` copy without
them, deleted after the conversion. The original isn't touched. If Shrinkerr, the container or the machine stops halfway, the
original is as it was, and the unfinished file beside it can be deleted.

## The checks before anything is replaced

The new file replaces the original only if it passes all of these:

1. **ffmpeg finished without an error**, and the new file isn't empty.
2. **It's complete**: its video is as long as the original's. A file cut short
   (a read error on a network share, a full disk) is thrown away.
3. **The picture is there.** A video track far too small for its length (a
   hardware decoder can produce blank or green frames without reporting an
   error) is retried with software decoding.
4. **HDR is still HDR.** An HDR10 or HLG original that came out as SDR is
   thrown away. Dolby Vision profiles 7 and 8 keep their Dolby Vision, or the
   conversion fails ([Encoding guide](encoding-guide.md#hdr-and-dolby-vision)).
5. **It's smaller.** A new file larger than the original is thrown away, and
   the file is marked ignored so it isn't tried again. Tracks you asked to
   remove are still removed, from the original.
6. **The quality is good enough.** With VMAF on, a new file scoring below
   your minimum is thrown away (Settings → Video; new installs reject below
   88, and a rule can set its own).

A file that fails a check keeps its original, untouched. The job's details
(Queue → Completed or Failed) say which check it failed.

**Review before replace** (Settings → Automation → Originals & Backups)
adds one more step: the new file waits in a hidden `.shrinkerr-review/`
folder beside its destination until you approve it in Queue → Review.
Rejecting it deletes the new file and keeps
the original. If the original changed in the meantime (Sonarr or Radarr
upgraded it), the waiting file is deleted instead of replacing the new
release.

## Replacing the original

The new file is put in place **first**, and only then is the original dealt
with. The new file's name usually changes: the codec tag (`x264` → `x265`, or
`h265` for hardware encoders), a disc source tag (`BR-DISK` → `Bluray`), the
resolution if it was scaled, and the extension, which is always `.mkv`.

When the new file has the same name as the original, the original is moved
into a hidden `.shrinkerr-replacing/` folder in the same directory for the
swap. If placing the new file fails, the original is moved back.

Then the original is handled the way Settings → Automation → Originals &
Backups says, or the way a matching rule says:

- **Keep for N days** (new installs: 7). The original moves into a
  `.shrinkerr_backup/` folder in the same directory, or into the backup
  folder you set (in a subfolder named after the file's folder). Originals
  older than N days are deleted after each job.
- **Move to the trash.** Shrinkerr uses the system trash: on Linux, a
  `.Trash-<uid>` folder at the top of the drive or share. If the trash can't
  take a file, the original goes to the backup folder instead (kept until you
  delete backups), and Undo works for it.
- **Delete** once the new file is in place.

If the original can't be removed (a permission error, say), it stays where
it is and the conversion still counts as done.

## Undo

Queue → Completed → **Undo** puts the original back while it's still kept
(the first option above). The original is restored first; the converted file
is removed only after that worked. If another file is now where the original
goes, Undo stops and changes nothing.

## Discs and ISOs

A disc folder (`BDMV/` or `VIDEO_TS/`) or an `.iso` converts to one `.mkv`
of the main title. It's written in the folder holding the disc, or, when
Radarr imported the disc as a release folder inside the movie folder, in the
movie folder, named after the movie.

The disc is then kept, trashed or deleted like any original. Only the disc
itself counts: `BDMV/` with `CERTIFICATE/` and `AACS/`, or `VIDEO_TS/` with an
empty `AUDIO_TS/` and `JACKET_P/`. Other files beside it stay, and a release
folder is removed only if that left it empty.

## Other changes to files

- **Removing tracks** (audio or subtitle cleanup without converting the video)
  writes a new file and goes through the same checks and the same
  replacement as a conversion, originals setting and Undo included.
- **Language detection** writes the detected language into the file's track
  tags. MKV files are edited in place (only the header changes). MP4 and MOV
  files are copied into a new file with the tags, without converting
  anything. Containers that can't hold language tags, such as AVI, aren't
  touched: the language waits until you use **Remux to MKV (apply language)**
  in the file's panel.
- **External subtitles** (`.srt`, `.ass`…) merged into a conversion are
  deleted afterwards only if you turned that on (Settings → Audio &
  subtitles).
- **Renaming** (Scanner → Rename, or after each conversion if you turned it
  on) renames the file and the subtitle files named after it, and its
  folders if you turned that on. It never renames onto an existing file.
- **Scanner → Trash files** moves the files to the trash. Shrinkerr can't
  restore them; your system's trash can.
- **Sonarr / Radarr replace** (Scanner, or a corrupt file's details) asks
  Sonarr or Radarr to blocklist the release, delete the file and search for
  another. Sonarr or Radarr deletes the file, not Shrinkerr, and only after
  you confirm.

## Files Shrinkerr leaves alone

- **Ignored files** aren't converted until you un-ignore them; their audio
  and subtitle clean-up still runs.
- **Hardlinked files**, such as downloads that are still seeding: on new
  installs Settings → Automation skips them, since converting one leaves the
  torrent client's copy behind and uses more space, not less.
- **Dolby Vision profile 5**, and HDR on encoders that can't keep it
  (Intel QSV and VAAPI).
- **Files newer than a few minutes**, if you turned that on, so a file still
  being copied isn't picked up.

## Who's told about the change

After a conversion Shrinkerr asks Sonarr or Radarr to rescan the series or
movie (not for files still in a download client's post-processing: those are
imported anyway), then Bazarr, and Plex, Jellyfin or Emby to scan the library,
each if it's set up.

## Where Shrinkerr writes

Only in your media folders (the files themselves and the `.converting`,
`.remuxing`, `.stripped`, `.shrinkerr-replacing`, `.shrinkerr-review` and
`.shrinkerr_backup` entries beside them), in the backup folder if you set
one, in the trash if you chose it, and in its own data folder (`/app/data`:
the database and the before / after frames). VMAF checks and text recognition
for image subtitles work in the system's temporary folder.
