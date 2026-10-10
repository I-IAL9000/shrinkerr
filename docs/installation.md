# Installation

The README covers the two-line docker-compose case. This page covers every
other scenario people actually hit.

## Contents
- [Picking an image variant](#picking-an-image-variant)
- [Docker Compose (recommended)](#docker-compose-recommended)
- [`docker run` (one-shot)](#docker-run-one-shot)
- [NVIDIA GPU setup](#nvidia-gpu-setup)
  - [Linux host](#linux-host)
  - [Windows + WSL2](#windows--wsl2)
- [macOS — native install for VideoToolbox](#macos--native-install-for-videotoolbox)
- [Reverse proxy setups](#reverse-proxy-setups)
  - [Traefik](#traefik)
  - [Caddy](#caddy)
  - [Nginx](#nginx)
- [Portainer / Unraid](#portainer--unraid)
- [First-run walkthrough](#first-run-walkthrough)
- [Upgrading](#upgrading)
- [Uninstall / reset](#uninstall--reset)

## Picking an image variant

Four tags are published to two registries simultaneously — pick whichever
you prefer:

- [`ghcr.io/i-ial9000/shrinkerr`](https://github.com/I-IAL9000/shrinkerr/pkgs/container/shrinkerr) — GitHub Container Registry. No anonymous-pull rate limits.
- [`pal9000/shrinkerr`](https://hub.docker.com/r/pal9000/shrinkerr) — Docker Hub mirror. Familiar default for many users. Docker Hub limits anonymous pulls; if you hit the limit, pull from GHCR or `docker login` first.

Image content is identical on both. The compose snippets in this guide
use `ghcr.io/...`; substitute `pal9000/shrinkerr:<tag>` if you'd rather
pull from Docker Hub.

| Tag | Platforms | Encoders | Use when |
|---|---|---|---|
| `:latest` | amd64, arm64 | CPU, Intel / AMD ([Hardware](hardware.md)) | Default for any host. Mac, Raspberry Pi, ARM cloud, Windows without GPU. |
| `:nvenc` | amd64 | NVENC + libx265 | NVIDIA GPU host, driver 570+. |
| `:edge` | amd64, arm64 | as `:latest`, ffmpeg master | You want bleeding-edge ffmpeg features. (Not a pre-release of Shrinkerr: that's `:develop`.) |
| `:edge-nvenc` | amd64 | NVENC + libx265, ffmpeg master | GPU + latest ffmpeg + driver 570+. |

All four share the same DB schema and settings format — moving between them
is `image: …`-line edit + `docker compose pull && docker compose up -d`.

Each comes in these flavours (shown for `:latest`; add `-nvenc`, `-edge` or
`-edge-nvenc`):

| Tag | Changes when | For |
|---|---|---|
| `:latest` | a new release, every few weeks | most people |
| `:0.10` | a patch to 0.10 (security and data-loss fixes) | patches without new features |
| `:v0.10.0` | never | pinning a version |
| `:develop` | every change | testers (the version shows `-dev`) |
| `:v0.11.0-rc.1` | never | trying a release before it's out |

## Docker Compose (recommended)

The minimal working compose:

```yaml
# /opt/shrinkerr/docker-compose.yml
services:
  shrinkerr:
    image: ghcr.io/i-ial9000/shrinkerr:latest
    container_name: shrinkerr
    ports:
      - "6680:6680"
    volumes:
      - ./data:/app/data        # SQLite DB, history, cached posters
      - /srv/media:/media       # your library (rw)
    restart: unless-stopped
```

Start it:

```bash
docker compose up -d
docker compose logs -f shrinkerr
```

**Directory-level tips**
- Put `docker-compose.yml` somewhere stable like `/opt/shrinkerr/` or `/srv/docker/shrinkerr/`. `./data` will be relative to that.
- Mount the media dir **read-write** — Shrinkerr replaces files in place (a backup folder only keeps the originals). If the user running Docker can't write to it, jobs will fail at the rename step.
- Mount the SAME library path as the server writes it. If Plex sees `/mnt/media/TV`, mount that path. If you can't, set a path mapping on that app's card in Settings → Integrations (Plex, Jellyfin, Emby, Sonarr / Radarr).

**More than one media root:**

```yaml
    volumes:
      - /mnt/tv:/media/tv
      - /mnt/movies:/media/movies
      - /mnt/other:/media/other
```

Then add each `/media/*` path in Settings → Media → Media Directories.

**Running as your own user (optional):**

```yaml
    environment:
      - PUID=1000    # the user that owns your media: `id -u`
      - PGID=1000    # its group: `id -g`
      - UMASK=002    # optional: new files are group-writable
```

Without `PUID` / `PGID` Shrinkerr runs as root, as before. With them it
runs as that user, so converted files and backups are owned like the ones
Sonarr / Radarr / Plex write. That user needs write access to the media;
Shrinkerr hands its data folder to it when the container starts. `UMASK`
also works on its own. On a NAS, see [Unraid, TrueNAS and Synology](nas.md).

## `docker run` (one-shot)

If you prefer not to use Compose:

```bash
docker run -d --name shrinkerr \
  -p 6680:6680 \
  -v /opt/shrinkerr/data:/app/data \
  -v /srv/media:/media \
  --restart unless-stopped \
  ghcr.io/i-ial9000/shrinkerr:latest
```

For NVENC replace the image with `:nvenc` and add `--runtime=nvidia --gpus all`.
To run as your own user, add `-e PUID=1000 -e PGID=1000` (see above).

## NVIDIA GPU setup

### Linux host

1. Install recent NVIDIA driver — `nvidia-smi` should report the GPU and driver version.
2. Install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
   Ubuntu summary:

   ```bash
   distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
   curl -s -L https://nvidia.github.io/libnvidia-container/gpgkey | sudo apt-key add -
   curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list \
     | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
   sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
   sudo nvidia-ctk runtime configure --runtime=docker
   sudo systemctl restart docker
   ```
3. Sanity-check:
   ```bash
   docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi
   ```
   If that prints your GPU, Shrinkerr's `:nvenc` image will too.
4. Update your compose file:

   ```yaml
   services:
     shrinkerr:
       image: ghcr.io/i-ial9000/shrinkerr:nvenc
       # ...
       deploy:
         resources:
           reservations:
             devices:
               - driver: nvidia
                 count: all
                 capabilities: [gpu]
   ```

   Some older compose setups prefer the shorthand:
   ```yaml
       runtime: nvidia
       environment:
         - NVIDIA_VISIBLE_DEVICES=all
   ```

5. Verify inside the running container:
   ```bash
   docker compose exec shrinkerr nvidia-smi
   ```
   You should see the GPU listed. In the UI, the Nodes page's **THIS SERVER**
   card should show the GPU name and the **NVENC (GPU)** and **libx265 (CPU)**
   pills; Monitor shows the driver version.

### Windows + WSL2

1. Install Docker Desktop.
2. Install a recent NVIDIA driver from NVIDIA's site (not Windows Update).
3. Use `:nvenc` image — nothing else to install. Docker Desktop 4.x+ bridges
   the GPU into WSL2 automatically when you set `--gpus all`.
4. Same compose snippet as Linux.

### What "NVENC not advertised" means

If Monitor shows **NVENC off** with a reason under it (and the Nodes page
shows only **libx265 (CPU)**), the detection ran and rejected NVENC. Common
reasons:
- **no NVIDIA GPU detected** — `nvidia-smi` didn't work inside the container.
  Your `--gpus`/`runtime: nvidia` wiring is missing.
- **ffmpeg build has no hevc_nvenc encoder** — you're on the `:latest` image
  (CPU-only). Switch to `:nvenc`.
- **ffmpeg exited …** — the detection test encode failed. Usually a driver
  version mismatch; see Troubleshooting.

## Intel/AMD GPU setup (experimental)

> ⚠️ **Experimental.** The QSV and VAAPI encoders ship in both Docker
> images and are wired through the queue, settings, rule engine, and
> worker capability advertisement. They're confirmed end-to-end on one
> tester's modern Intel iGPU, but not yet on enough hardware to call
> them stable. **Please [open an issue](https://github.com/I-IAL9000/shrinkerr/issues)
> with results (working or failing) so we can promote them out of
> experimental.**

### Activate

Both images (`:latest` and `:nvenc`) carry the VA-API runtime libraries
(on amd64).
The host needs to pass through `/dev/dri` and let the container's user
into the render group:

```yaml
services:
  shrinkerr:
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - video
      # numeric GID of the host group that owns /dev/dri/renderD128.
      # Find with: stat -c '%g' /dev/dri/renderD128
      - "110"
```

`group_add` accepts the group **name** (`render` on most distros) but
many distros leave the group nameless inside the container, in which
case you'll see `unable to find group render` and have to use the
numeric GID instead. With `PUID` / `PGID` set
([Docker Compose](#docker-compose-recommended)), Shrinkerr joins the
groups that own `/dev/dri` itself, so `group_add` isn't needed.

Then `docker compose down && docker compose up -d`, open Settings →
Video, and click **Re-detect** next to the encoder dropdown. QSV
and VAAPI options appear if `/dev/dri/renderD*` is exposed and the
ffmpeg build has the encoders (BtbN GPL builds always do).

### Verify

```bash
docker exec shrinkerr vainfo
```

Expected output is a header reporting `VA-API version 1.x`, then a
list of profiles. For HEVC encoding you want at least one of
`VAProfileHEVCMain` (8-bit) or `VAProfileHEVCMain10` (10-bit) in the
**VAEntrypointEncSlice** column.

### Troubleshooting

**`vaInitialize failed with error code 18 (invalid parameter)`** with
`DRM_IOCTL_I915_GEM_APERTURE failed: Invalid argument` — the render
node libva is targeting isn't an Intel i915 device (typical on hosts
with both NVIDIA and Intel hardware where the iGPU is disabled in
BIOS, leaving only the NVIDIA card's render node visible). Verify on
the host:

```bash
ls -la /dev/dri/by-path/
cat /sys/class/drm/renderD128/device/uevent | grep DRIVER
```

If `DRIVER=nvidia` appears for `renderD128`, enable the iGPU in BIOS
(Advanced → System Agent → Internal Graphics → Enabled, or similar)
and reboot. After that a second `renderD129` node appears bound to
`i915` and VAAPI/QSV will work.

**`Failed to initialize` / no profiles listed** — driver mismatch.
Try setting `LIBVA_DRIVER_NAME=iHD` (Intel Gen9+) or `i965` (Intel
Gen8 and older) as an env var on the service. Older AMD GPUs with
the radeon (not amdgpu) driver use `radeonsi`.

**`encoding failed: ... vaapi: ...`** at job-run time — usually the
render node is correct but the kernel driver is too old to support
HEVC encoding. `vainfo` will show no `VAProfileHEVCMain*` rows. Update
the host kernel + driver, or fall back to libx265 / NVENC.

**`Error creating a MFX session: -9` / `device failed (-17)` /
`Error setting child device handle: -17` with QSV** — all variants
of the same root cause: the bundled MediaSDK runtime didn't match
the modern ffmpeg / libva combination shipped in the image. Track:

  - v0.3.84 added `libvpl2` + `libmfx1` (legacy MediaSDK runtime).
  - v0.3.87 added explicit `-init_hw_device qsv=qsv@va` device init.
  - v0.3.89 builds **`vpl-gpu-rt`** (libmfx-gen) from source — Intel's
    modern oneVPL GPU runtime, not in stock Debian 12 / Ubuntu 22.04
    repos. The `libvpl2` dispatcher prefers it over the legacy
    `libmfx1`, so QSV operations actually go through a runtime
    capable of accepting libva-2.22 VADisplays.

Pull v0.3.89+ for QSV to work on stock images.

**`libva.so.2: undefined symbol vaMapBuffer2`** with VAAPI — fixed in
v0.3.88. The BtbN n7.x ffmpeg static build expects libva ≥ 2.20
(`vaMapBuffer2` was added there); Debian 12 / Ubuntu 22.04 stock libva
is 2.14–2.17. v0.3.88+ images build libva 2.22.0 from source during
image creation and replace the system `libva.so.2` with it. The
existing iHD / mesa-va drivers (compiled against the older libva)
continue working since libva 2.x is ABI-stable. Pull v0.3.88+ to fix.

## macOS — native install for VideoToolbox

Docker on macOS runs Linux containers inside a VM that can't reach
Apple's hardware video encoder (VideoToolbox). The `:latest` image
works on Mac but only via software encoding (libx265), which clocks at
roughly **1 fps** on Apple Silicon — usable for a one-off conversion,
useless for a library.

Native install (no Docker) gives you direct access to VideoToolbox via
brew's ffmpeg, reaching ~30–100 fps for h265 encoding. VideoToolbox is a
regular encoder choice in Settings (selected by default on a fresh
native install); the trade-off is slightly more setup than `docker
compose up -d`.

Full walkthrough: [Running Shrinkerr natively on macOS](native-install-mac.md).
Covers brew prereqs, Python venv setup, the frontend build, VideoToolbox
settings, running a Mac as a remote worker, a launchd service file for
auto-start, and troubleshooting.

## Reverse proxy setups

Shrinkerr speaks plain HTTP on `:6680` and does not terminate TLS. Front it
with a proxy if you want HTTPS, auth, or virtual hosts. The app uses
WebSockets (`/ws`) for live progress, so make sure your proxy forwards
those.

If the proxy signs people in (Authelia, Authentik, …), choose **External**
in Settings → System → Authentication, and Shrinkerr asks nobody. Use it
only when Shrinkerr can't be reached except through the proxy. With the
sign-in page and "Not from local addresses", requests through a proxy
still sign in: they never count as local. See [Security](security.md).

### Traefik

```yaml
services:
  shrinkerr:
    image: ghcr.io/i-ial9000/shrinkerr:nvenc
    labels:
      - traefik.enable=true
      - traefik.http.routers.shrinkerr.rule=Host(`shrinkerr.example.com`)
      - traefik.http.routers.shrinkerr.entrypoints=websecure
      - traefik.http.routers.shrinkerr.tls=true
      - traefik.http.routers.shrinkerr.tls.certresolver=le
      - traefik.http.services.shrinkerr.loadbalancer.server.port=6680
    networks: [proxy]
    volumes:
      - ./data:/app/data
      - /srv/media:/media
networks:
  proxy:
    external: true
```

Traefik routes WebSockets automatically when the backend supports
`Upgrade: websocket`.

### Caddy

```caddy
shrinkerr.example.com {
    reverse_proxy localhost:6680
}
```

Caddy handles TLS + WebSockets out of the box.

### Nginx

```nginx
server {
    listen 443 ssl http2;
    server_name shrinkerr.example.com;

    ssl_certificate     /etc/ssl/certs/fullchain.pem;
    ssl_certificate_key /etc/ssl/private/privkey.pem;

    location / {
        proxy_pass              http://127.0.0.1:6680;
        proxy_http_version      1.1;
        proxy_set_header        Host              $host;
        proxy_set_header        X-Real-IP         $remote_addr;
        proxy_set_header        X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header        X-Forwarded-Proto $scheme;

        # WebSockets for live job progress
        proxy_set_header        Upgrade           $http_upgrade;
        proxy_set_header        Connection        "upgrade";
        proxy_read_timeout      1d;
    }
}
```

The `proxy_read_timeout` bump keeps long-running encode WebSocket connections
alive; the default 60s will disconnect during any job longer than a minute.

## Portainer / Unraid

For these platforms, edit the stack/template to use the compose snippets
above. There's nothing Shrinkerr-specific — it behaves like any other
containerized web app.

On Unraid, map the webUI port in the template to `6680` and the two volumes
(`/app/data` and `/media`). Unraid's "Add container" UI prompts for each.
More for Unraid, TrueNAS and Synology: [nas.md](nas.md).

## First-run walkthrough

1. Browse to `http://<host>:6680`. Shrinkerr first asks how people sign in
   (step 6), then the setup wizard greets you.
2. **Add media directories** — the wizard's first step: enter one or more
   absolute paths that match your compose volumes. Each directory gets a
   label (e.g. "Movies", "TV") used in the UI. The wizard then asks for the
   encoder, a quality preset, the languages to keep and what happens to
   originals.
3. **Scan your library** — the wizard's last step starts the scan and opens
   the Scanner (later: Scanner page, pick a directory, hit Scan). First
   scan on a 10TB library typically takes 10–40 minutes
   depending on disk speed; subsequent scans are incremental. Posters
   and original-language metadata are auto-fetched using the bundled
   TMDB non-commercial key — nothing to configure.
4. **Customize (optional)** — Settings → Integrations to link
   Plex / Jellyfin / Sonarr / Radarr for label-based rules and library
   sync. Settings → Video to fine-tune encoder presets. Everything here
   is optional; the app works out of the box.
5. **Encoder choice** — the wizard's Hardware step, or Settings → Video. On
   first start the default encoder is set from what the host supports:
   NVENC if an NVIDIA GPU is detected, Quick Sync or VAAPI on Intel / AMD,
   otherwise libx265 (VideoToolbox on a native Mac). See the
   [Encoding guide](encoding-guide.md) for preset tuning.
6. **Sign-in** — the first visit asks how people sign in: a username and
   password, or External if a reverse proxy signs them in. Change it later
   in Settings → System → Authentication. See [Security](security.md).

New installs start with safer defaults (an updated install keeps its
settings):

- originals are kept 7 days (Settings → Automation → Originals & Backups);
- encodes scoring below VMAF 88 are rejected (Settings → Video → Smart
  Encoding);
- subtitles are left alone until you choose languages to keep (the wizard,
  or Settings → Audio & subtitles → Subtitles);
- files that are still hardlinked (e.g. seeding) are skipped
  (Settings → Automation → Conversion Filters);
- content type detection is on (Settings → Video → Smart Encoding).

What happens to a file when it's converted: [What happens to your files](what-happens-to-your-files.md).

Also worth knowing:

- **Doctor** (sidebar) checks the media folders, the data folder, ffmpeg and
  your encoder, the connections, remote workers and the password. Its
  **Download diagnostics** makes a file for bug reports, with passwords, API
  keys and tokens taken out.
- **Nodes → Add a remote worker** writes the command to run a worker on
  another machine ([Remote workers](remote-workers.md#setting-up-a-worker)).
- A read-only **demo** with a made-up library: [demo.md](demo.md).
- Coming from Tdarr or Unmanic: [migrating.md](migrating.md).

### Bundled vs. your own TMDB key

The official images (`:latest` / `:nvenc` / `:edge` / `:edge-nvenc`)
ship with a bundled non-commercial TMDB key, so posters and metadata
work immediately. Reasons you might want your own key:

- **Higher rate-limit quota** — the bundled key is shared across all
  installs; your own key gets its own quota.
- **Rotation safety** — if the bundled key is ever rate-limited or
  rotated, a user-saved key keeps working.
- **Self-builds** — if you build your own image without setting
  `TMDB_API_KEY` as a build arg, the bundled slot is empty.

Apply at <https://www.themoviedb.org/settings/api> and paste into
Settings → Integrations → Metadata APIs. User-saved keys always win over the
bundled one.

If you're running a self-build and want to bundle your own key at
build time:

```yaml
services:
  shrinkerr:
    build:
      context: .
      args:
        TMDB_API_KEY: your_key_here
```

Or pass at runtime via an env var on the container:

```yaml
services:
  shrinkerr:
    environment:
      - SHRINKERR_TMDB_API_KEY=your_key_here
```

TMDB's terms require attribution; Shrinkerr already renders it in
Settings → System → Support.

The wizard stays visible in the Dashboard until those steps are done
(or you dismiss it with "Skip setup").

## Upgrading

```bash
docker compose pull
docker compose up -d
```

Database migrations run automatically on startup. If you want to be cautious,
back up `./data/shrinkerr.db` first — the DB is the only stateful thing.
Read the release's **Upgrade notes** (on the
[Releases](https://github.com/I-IAL9000/shrinkerr/releases) page; Shrinkerr
also shows them once after an update) — they say what to do, if anything.

Updating to 0.10 from an older version:

- Shrinkerr asks how people sign in before anything else, unless password
  login was already on. Download clients, Sonarr / Radarr and remote
  workers keep using the API key, now only in the `X-Api-Key` header (no
  longer `?api_key=` in a URL).
- Update remote workers to 0.10 as well: older ones don't know the new
  per-job settings.
- The first full scan takes longer: it reads every file once more.
- Your settings stay as they were; the safer defaults
  ([above](#first-run-walkthrough)) are for new installs.

Switching between `:latest` ↔ `:nvenc` ↔ `:edge` is safe — same schema,
same settings format, capability detection re-runs on startup.

### Knowing when there's an update

The sidebar shows **Update available** when a new release is out — or, on a
`:develop` image, a newer develop build (with a link to what changed).
Shrinkerr asks GitHub every 30 minutes; turn that off in Settings → System →
Updates. It never updates itself: the container is replaced with
`docker compose pull && docker compose up -d`, by you or a tool:

- **[Diun](https://crazymax.dev/diun/)** tells you (Discord, email, ntfy…)
  when a new image is out, and changes nothing.
- **[What's Up Docker](https://getwud.github.io/wud/)** does the same, with a
  web page, and can update a container when you click.
- **Watchtower** updates containers by itself. On `:latest` that means
  releases arrive without you reading their upgrade notes; if you use it,
  point it at a minor line (`:0.10`) so only patches arrive unattended.

### Going back to an older version

Migrations only ever add tables and columns, so an older version still starts
on a database a newer one has used, and ignores what it doesn't know (new
settings, new job states). Take a backup first (Settings → System →
Backups), then change the `image:` line to the older tag. Moving from
`:develop` back to `:latest` works the same way.

One thing to know: versions before 0.10 don't know **External** sign-in, so
going back to one of them from a 0.10 install that uses it turns sign-in off
there (or asks for the API key, if one is set) until you set a password.

## Uninstall / reset

```bash
docker compose down
rm -rf ./data            # wipes the DB, cached posters
docker image rm ghcr.io/i-ial9000/shrinkerr:nvenc
```

Your media is untouched — Shrinkerr only writes into `/app/data`, converts
files in place, and (when it keeps originals, as new installs do for 7 days)
into a `.shrinkerr_backup` folder next to each source file, or the backup
folder you set. With review before replace on (Settings → Automation →
Originals & Backups), converted files waiting in Queue → Review sit in a
hidden `.shrinkerr-review` folder beside the original.
