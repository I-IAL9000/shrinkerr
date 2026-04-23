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
- [Reverse proxy setups](#reverse-proxy-setups)
  - [Traefik](#traefik)
  - [Caddy](#caddy)
  - [Nginx](#nginx)
- [Portainer / Unraid](#portainer--unraid)
- [First-run walkthrough](#first-run-walkthrough)
- [Upgrading](#upgrading)
- [Uninstall / reset](#uninstall--reset)

## Picking an image variant

Four tags are published to
[ghcr.io/i-ial9000/shrinkerr](https://github.com/I-IAL9000/shrinkerr/pkgs/container/shrinkerr):

| Tag | Platforms | Encoders | Use when |
|---|---|---|---|
| `:latest` | amd64, arm64 | libx265 (CPU) | Default for any host. Mac, Raspberry Pi, ARM cloud, Windows without GPU. |
| `:nvenc` | amd64 | NVENC + libx265 | NVIDIA GPU host, driver 525.60.13+. |
| `:edge` | amd64, arm64 | libx265, ffmpeg master | You want bleeding-edge ffmpeg features. |
| `:edge-nvenc` | amd64 | NVENC + libx265, ffmpeg master | GPU + latest ffmpeg + driver 570+. |

All four share the same DB schema and settings format — moving between them
is `image: …`-line edit + `docker compose pull && docker compose up -d`.

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
      - ./data:/app/data        # SQLite DB, logs, history, cached posters
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
- Mount the media dir **read-write** — Shrinkerr replaces files in place unless you configure a backup folder. If the user running Docker can't write to it, jobs will fail at the rename step.
- Mount the SAME library path as the server writes it. If Plex sees `/mnt/media/TV`, mount that path. Path mapping is for remote workers, not for the primary server.

**More than one media root:**

```yaml
    volumes:
      - /mnt/tv:/media/tv
      - /mnt/movies:/media/movies
      - /mnt/other:/media/other
```

Then add each `/media/*` path in Settings → Directories.

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
   You should see the GPU listed. In the UI, Nodes → Local should show GPU
   name + driver + `capabilities: [libx265, nvenc]`.

### Windows + WSL2

1. Install Docker Desktop.
2. Install a recent NVIDIA driver from NVIDIA's site (not Windows Update).
3. Use `:nvenc` image — nothing else to install. Docker Desktop 4.x+ bridges
   the GPU into WSL2 automatically when you set `--gpus all`.
4. Same compose snippet as Linux.

### What "NVENC not advertised" means

If Nodes → Local shows `libx265` only plus a red `nvenc_unavailable_reason`,
the detection ran and rejected NVENC. Common reasons:
- **no NVIDIA GPU detected** — `nvidia-smi` didn't work inside the container.
  Your `--gpus`/`runtime: nvidia` wiring is missing.
- **ffmpeg build has no hevc_nvenc encoder** — you're on the `:latest` image
  (CPU-only). Switch to `:nvenc`.
- **ffmpeg exited …** — the detection test encode failed. Usually a driver
  version mismatch; see Troubleshooting.

## Reverse proxy setups

Shrinkerr speaks plain HTTP on `:6680` and does not terminate TLS. Front it
with a proxy if you want HTTPS, auth, or virtual hosts. The app uses
WebSockets (`/ws`) for live progress, so make sure your proxy forwards
those.

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

## First-run walkthrough

1. Browse to `http://<host>:6680` — the setup wizard greets you.
2. **Add media directories** — click "Go to Settings", enter one or more
   absolute paths that match your compose volumes. Each directory gets a
   label (e.g. "Movies", "TV") used in the UI.
3. **Scan your library** — return to the Scanner page, pick a directory,
   hit Start Scan. First scan on a 10TB library typically takes 10–40
   minutes depending on disk speed; subsequent scans are incremental.
4. **Connect TMDB (optional but recommended)** — Settings → Connections →
   TMDB. Powers native-language detection, poster artwork, and rule
   conditions like "genre contains X".
5. **Encoder choice** — Settings → Video. If you have an NVIDIA GPU pick
   NVENC; otherwise libx265 is the default. See the
   [Encoding guide](encoding-guide.md) for preset tuning.
6. **Set an auth password** — Settings → System → Authentication. Don't
   expose the port to the internet without this.

The wizard stays visible in the Dashboard until those steps are done
(or you dismiss it with "Skip setup").

## Upgrading

```bash
docker compose pull
docker compose up -d
```

Database migrations run automatically on startup. If you want to be cautious,
back up `./data/shrinkerr.db` first — the DB is the only stateful thing.

Switching between `:latest` ↔ `:nvenc` ↔ `:edge` is safe — same schema,
same settings format, capability detection re-runs on startup.

## Uninstall / reset

```bash
docker compose down
rm -rf ./data            # wipes the DB, logs, cached posters
docker image rm ghcr.io/i-ial9000/shrinkerr:nvenc
```

Your media is untouched — Shrinkerr only writes into `/app/data`, converts
files in place, and (optionally) into a `.shrinkerr_backup` folder next to
each source file.
