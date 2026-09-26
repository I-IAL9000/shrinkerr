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
- [Using VideoToolbox](#using-videotoolbox)
- [Using a Mac as a remote worker](#using-a-mac-as-a-remote-worker)
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

- **NVENC / QSV / VAAPI are Linux-only.** On a Mac the choices are
  VideoToolbox (hardware) and libx265 (software).
- **VMAF** needs an ffmpeg built with libvmaf, which Homebrew's isn't.
  VMAF is also skipped for hardware-decoded jobs on every encoder.
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

Shrinkerr reads config from environment variables (it does not load a
`.env` file itself). The only required setup is where to keep the
database and where your media lives. Caches such as the IMDb ratings file
go in the same folder as the database:

```sh
export SHRINKERR_DB_PATH=/Users/<you>/shrinkerr/data/shrinkerr.db
export SHRINKERR_MEDIA_ROOT=/Volumes/Media
# Optional — caps scanner parallelism (v0.7.9+). Default 4.
export SHRINKERR_SCAN_CONCURRENCY=4
```

Start the app (it listens on port 6680):

```sh
source .venv/bin/activate
python3 -m backend.main
```

Visit http://localhost:6680 — first-run walkthrough takes you through
adding media directories and encoding settings, same as the Docker
flow.

## Using VideoToolbox

When Shrinkerr runs natively on macOS with a VideoToolbox-enabled ffmpeg,
**VideoToolbox (Apple Silicon / Mac)** appears in Settings → Video →
Default Encoder. On a fresh install it's selected for you.

- **Quality** is a constant-quality value from 1 to 100 where **higher
  means better quality and larger files** (the reverse of CQ/CRF).
  The default, 55, came out roughly equal to libx265 CRF 22 in size and
  SSIM on an M1 Pro. Use 60–65 for near-transparent output and 45–50 for
  bigger savings. There are no presets; speed is set by the hardware.
- **Use VideoToolbox for decode** (on by default) decodes H.264 and HEVC
  sources on the Mac's media engine. Other codecs (MPEG-2, VC-1, …) are
  decoded in software automatically.
- Output is 10-bit HEVC for 10-bit sources and 8-bit for 8-bit sources.
  HDR10 colour tags and mastering/light-level metadata are kept.
- Rules and the Queue dialog can pick VideoToolbox as the encoder; its
  quality always comes from Settings.

On an M1 Pro, 1080p encodes run at roughly 120 fps with hardware decode.

## Using a Mac as a remote worker

A Mac can also help a Linux/NVENC server as a remote worker. Run it
natively (not in Docker) so it can reach VideoToolbox:

```sh
source .venv/bin/activate
export SHRINKERR_MODE=worker
export SERVER_URL=http://your-server:6680
export API_KEY=<the server's API key>
export WORKER_NAME=MacBook
export SHRINKERR_DATA_DIR=/Users/<you>/shrinkerr-worker   # worker id + token
python3 -m backend.main
```

The worker shows up on the Nodes page with a **VideoToolbox (Mac)**
capability. NVENC jobs it picks up are encoded with VideoToolbox, at the
VideoToolbox quality configured on the server. Turn off *translate
encoder* for the node if it should only take jobs it can run natively.

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

**VideoToolbox missing from the encoder dropdown** — Shrinkerr is running
in Docker (a Linux VM can't reach VideoToolbox), or the ffmpeg on `PATH`
isn't Homebrew's. Check `which ffmpeg` and the command above, then press
**Re-detect** next to the encoder dropdown.

**If you used the old `custom_ffmpeg_flags` workaround** (`-c:v
hevc_videotoolbox …`) — clear those flags and pick VideoToolbox as the
encoder instead.

**`pip install` fails building `av`** — use Python 3.11 as shown above;
the pinned dependencies don't build on the newest Python releases.

**Anything else** — open an issue with the output of `ffmpeg
-version` and the failing job's "View ffmpeg log" expand from the
Completed tab.
