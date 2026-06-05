# Running Shrinkerr natively on macOS

Docker on macOS runs Linux containers inside a VM, which cuts you off
from Apple's hardware video encoder (VideoToolbox). Benchmark numbers
on Apple Silicon: ~1 fps under Docker for x265 software encoding vs.
~30–100 fps native with VideoToolbox. If you want anything resembling
real-time conversion on a Mac, **don't use Docker** — run the Python
backend directly on the host so ffmpeg can talk to the GPU.

This guide assumes you're comfortable on the command line. Tested on
macOS 14+ (Sonoma) on both Apple Silicon (M1/M2/M3/M4) and Intel.

## Contents

- [What you get](#what-you-get)
- [What's NOT supported yet](#whats-not-supported-yet)
- [Prerequisites](#prerequisites)
- [Install](#install)
- [Configuration](#configuration)
- [Using VideoToolbox today (workaround)](#using-videotoolbox-today-workaround)
- [Running as a launchd service](#running-as-a-launchd-service)
- [Updating](#updating)
- [Troubleshooting](#troubleshooting)

## What you get

Running natively gives you:

- **Full disk performance.** No Docker FUSE / 9p / virtiofs overhead. The
  scanner can stat hundreds of files per second instead of dozens.
- **Access to VideoToolbox** via ffmpeg's `hevc_videotoolbox` /
  `h264_videotoolbox` encoders, the only fast-encode path on Mac.
- **Lower idle resource use.** No always-running Linux VM.
- **Easier debugging.** Logs go straight to stdout; `lsof`/`fs_usage`
  see the real processes.

## What's NOT supported yet

- **VideoToolbox is not a first-class encoder option in the UI.** The
  Settings → Video → Encoding dropdown lists `libx265` / `nvenc` /
  `qsv` / `vaapi`. There's no VideoToolbox toggle. You can still use
  VideoToolbox via the `custom_ffmpeg_flags` setting — see [Using
  VideoToolbox today](#using-videotoolbox-today-workaround) below —
  but the encoder-specific quality knobs, preset menus, and auto-
  selection don't know about it. A proper VideoToolbox encoder is on
  the roadmap; if you want it sooner, file an issue with your
  benchmark numbers and ffmpeg config.
- **NVENC / QSV / VAAPI are Linux-only.** Mac users default to
  `libx265` (slow without VideoToolbox) or use the workaround below.
- **Disc-folder + ISO conversion** still works on Mac (uses libdvdread
  / libbluray bundled with brew's ffmpeg).

## Prerequisites

```sh
# Homebrew if you don't have it
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Python, ffmpeg, node — all from brew
brew install python@3.11 ffmpeg node

# Confirm ffmpeg has VideoToolbox compiled in (brew's default does)
ffmpeg -encoders 2>/dev/null | grep -i videotoolbox
# Expected: hevc_videotoolbox, h264_videotoolbox listed
```

If you don't see videotoolbox in the encoder list, brew's ffmpeg has
gone weird — try `brew reinstall ffmpeg` or build from source with
`./configure --enable-videotoolbox`.

## Install

```sh
# Clone (or download a release tarball)
git clone https://github.com/I-IAL9000/shrinkerr.git
cd shrinkerr

# Python venv (keeps deps off your system Python)
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Build the frontend (one-time; rebuild only on UI changes)
cd frontend
npm ci
npm run build
cd ..
```

That's it — the backend will serve the built frontend out of
`frontend/dist/` automatically.

## Configuration

Shrinkerr reads config from environment variables. The defaults are
sensible; the only required setup is pointing it at your media. Create
`.env` in the repo root:

```sh
# .env (example)
SHRINKERR_DB_PATH=/Users/<you>/shrinkerr/shrinkerr.db
SHRINKERR_MEDIA_ROOT=/Volumes/Media
SHRINKERR_PORT=6680
# Optional — caps scanner parallelism (v0.7.9+). Default 4.
SHRINKERR_SCAN_CONCURRENCY=4
```

Start the app:

```sh
source .venv/bin/activate
python3 -m backend.main
```

Visit http://localhost:6680 — first-run walkthrough takes you through
adding media directories and encoding settings, same as the Docker
flow.

## Using VideoToolbox today (workaround)

Until VideoToolbox is a first-class encoder option, you can get the
speedup via the `custom_ffmpeg_flags` setting:

1. Settings → Video → Encoding → set encoder to **libx265** (this is
   the dropdown choice that lets the rest of the pipeline work; we'll
   override the actual encoder via custom flags).
2. Settings → Video → Advanced → `custom_ffmpeg_flags`, set to:
   ```
   -c:v hevc_videotoolbox -b:v 8000k -tag:v hvc1
   ```
   These flags get appended AFTER libx265's encoder args, and ffmpeg
   honours the last `-c:v` it sees — so the libx265 stanza is
   effectively replaced.

Tune the bitrate (`-b:v 8000k` → 8 Mbps) to your taste. VideoToolbox
is bitrate-controlled, not CRF/CQ, so the UI's "Constant Quality"
slider doesn't apply — set it to whatever and override here.

Caveats:

- The estimated-savings numbers in the UI assume libx265 compression
  ratios. VideoToolbox typically produces larger files at the same
  visual quality; expect the actual savings to be smaller than the
  estimate.
- VMAF analysis still runs and gives useful numbers.
- The `x265`/`h265` filename tag is still applied by Shrinkerr's
  renamer — the converted file IS HEVC, just from a different encoder,
  so the tag is technically accurate.

## Running as a launchd service

To auto-start on login:

```xml
<!-- ~/Library/LaunchAgents/io.shrinkerr.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>io.shrinkerr</string>
  <key>WorkingDirectory</key>
  <string>/Users/YOU/shrinkerr</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/shrinkerr/.venv/bin/python3</string>
    <string>-m</string>
    <string>backend.main</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SHRINKERR_DB_PATH</key>
    <string>/Users/YOU/shrinkerr/shrinkerr.db</string>
    <key>SHRINKERR_MEDIA_ROOT</key>
    <string>/Volumes/Media</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key>
  <string>/Users/YOU/shrinkerr/stdout.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/YOU/shrinkerr/stderr.log</string>
</dict>
</plist>
```

Load + start:

```sh
launchctl load ~/Library/LaunchAgents/io.shrinkerr.plist
launchctl start io.shrinkerr
```

## Updating

```sh
cd /Users/YOU/shrinkerr
git pull
source .venv/bin/activate
pip install -r requirements.txt
cd frontend && npm ci && npm run build && cd ..
# If using launchd:
launchctl stop  io.shrinkerr
launchctl start io.shrinkerr
```

## Troubleshooting

**`ModuleNotFoundError: No module named 'backend'`** — you're not in the
repo root or the venv isn't activated. `cd` to the cloned repo and
`source .venv/bin/activate`.

**Frontend shows 404** — the React build wasn't run, or you started
backend from a different directory. From the repo root, confirm
`frontend/dist/index.html` exists; if not, `cd frontend && npm run
build`.

**`hevc_videotoolbox` returns "Unknown encoder"** — brew's ffmpeg
doesn't have VT compiled in (rare; possibly a custom build). Run
`ffmpeg -version` and confirm `--enable-videotoolbox` is in the
configure line.

**Conversion crashes with `Function not implemented` mid-stream** — the
libx265 + VideoToolbox-via-custom-flags combination occasionally
confuses ffmpeg's filter graph. Try removing `-tag:v hvc1` from the
custom flags as a first cut.

**Anything else** — open an issue with the output of `ffmpeg
-version`, your custom_ffmpeg_flags string, and the failing job's
"View ffmpeg log" expand from the Completed tab.
