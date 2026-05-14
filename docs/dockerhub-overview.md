<!--
Source for the Docker Hub "Repository overview" at
https://hub.docker.com/r/pal9000/shrinkerr — Docker Hub renders this
as the public-facing description. Edit here, copy-paste into the
Docker Hub UI (General → Repository overview → Edit). Or wire up the
peter-evans/dockerhub-description@v4 action in the build workflow to
sync it on every CI run.
-->

# Shrinkerr

**Self-hosted media library transcoder** with hardware encoding, VMAF quality assurance, and a safety net for your originals.

Re-encode H.264 / MPEG-2 / VC-1 / VP9 sources to HEVC, saving 40–65% disk space on a typical library. NVENC (NVIDIA) and libx265 (CPU) encoders; NVDEC / QSV / VAAPI hardware decode. VMAF auto-rejects bad encodes. Plex / Jellyfin / Emby aware (pauses on stream, refreshes libraries on completion). Sonarr / Radarr / NZBGet / SABnzbd integration with a rules engine. Distributed encoding across multiple workers.

## Image variants

| Tag | Platforms | Encoding | Use when |
|---|---|---|---|
| `:latest` | linux/amd64 + arm64 | libx265 (CPU) | **Default.** Mac, Raspberry Pi, ARM cloud, any host without an NVIDIA GPU. |
| `:edge` | linux/amd64 + arm64 | libx265 (CPU) | Same as `:latest`, bleeding-edge ffmpeg master. |
| `:nvenc` | linux/amd64 | NVENC + libx265 | NVIDIA GPU host, ffmpeg n7.1, driver 525.60.13+. |
| `:edge-nvenc` | linux/amd64 | NVENC + libx265 | NVIDIA GPU host, ffmpeg master, driver 570+. |

All variants share the same DB schema and settings format — switch between them by changing one `image:` line and pulling.

## Quick start

```yaml
# docker-compose.yml
services:
  shrinkerr:
    image: pal9000/shrinkerr:latest
    container_name: shrinkerr
    ports:
      - "6680:6680"
    volumes:
      - ./data:/app/data
      - /path/to/your/media:/media
    restart: unless-stopped
```

```bash
docker compose up -d
```

Then open http://localhost:6680, add your media directories in Settings, and run a scan.

For NVENC, follow the [GPU setup guide](https://github.com/I-IAL9000/shrinkerr/blob/main/docs/installation.md) — needs the NVIDIA Container Toolkit on the host.

## Links

- **Source + full documentation** — [github.com/I-IAL9000/shrinkerr](https://github.com/I-IAL9000/shrinkerr)
- **Installation guides** (Portainer, reverse proxy, Windows + WSL2) — [docs/installation.md](https://github.com/I-IAL9000/shrinkerr/blob/main/docs/installation.md)
- **Encoder tuning, VMAF, hardware decode** — [docs/encoding-guide.md](https://github.com/I-IAL9000/shrinkerr/blob/main/docs/encoding-guide.md)
- **Issues / feature requests** — [github.com/I-IAL9000/shrinkerr/issues](https://github.com/I-IAL9000/shrinkerr/issues)
- **Changelog** — [CHANGELOG.md](https://github.com/I-IAL9000/shrinkerr/blob/main/CHANGELOG.md)

## Mirrored on GHCR

Also available without Docker Hub's anonymous-pull rate limits (100 / 6h / IP):

```bash
docker pull ghcr.io/i-ial9000/shrinkerr:latest
```

Same images, both registries published simultaneously from CI.
