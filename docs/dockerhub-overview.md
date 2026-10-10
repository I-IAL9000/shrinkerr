<!--
Source for the Docker Hub "Repository overview" at
https://hub.docker.com/r/pal9000/shrinkerr — Docker Hub renders this
as the public-facing description. Edit here, copy-paste into the
Docker Hub UI (General → Repository overview → Edit). Or wire up the
peter-evans/dockerhub-description@v4 action in the build workflow to
sync it on every CI run.
-->

# Shrinkerr

Shrinkerr makes a video library smaller without making it worse. It converts films and episodes to HEVC (or AV1), removes the audio and subtitle tracks you don't want, and replaces a file only when the new one passes its checks.

- **It knows your languages:** it keeps the languages you choose plus each title's original language (from TMDB), and identifies untagged tracks by listening to the audio or reading the subtitles.
- **It converts disc rips:** Blu-ray and DVD folders and ISOs become one MKV.
- **It checks before it replaces:** a new file has to be complete, still HDR if the original was, smaller, and good enough by its VMAF score. Originals can be kept for days, and Undo puts one back.

Converted files usually come out 40–65% smaller. It works with Sonarr, Radarr, Bazarr, Plex, Jellyfin and Emby, encodes on NVIDIA (NVENC), Intel / AMD (Quick Sync, VAAPI) or the CPU, and can share the work with other machines.

## Image variants

| Tag | Platforms | Encoding | Use when |
|---|---|---|---|
| `:latest` | linux/amd64 + arm64 | CPU; Quick Sync / VAAPI on amd64 | **Default.** Mac, Raspberry Pi, ARM cloud, any host without an NVIDIA GPU. |
| `:edge` | linux/amd64 + arm64 | as `:latest` | Same as `:latest`, bleeding-edge ffmpeg master. |
| `:nvenc` | linux/amd64 | NVENC + libx265 | NVIDIA GPU host, ffmpeg n8.1, driver 570+. |
| `:edge-nvenc` | linux/amd64 | NVENC + libx265 | NVIDIA GPU host, ffmpeg master, driver 570+. |

Each release is also tagged with its version (`:v0.10.0`, `:v0.10.0-nvenc`) and its minor line (`:0.10`, `:0.10-nvenc`: that line's patches only). `:develop` and `:develop-nvenc` are test builds of what's coming next.

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

Then open http://localhost:6680: set up sign-in, and the setup wizard walks you through media folders, the encoder, quality, languages and what happens to originals, then scans.

For NVENC, follow the [GPU setup guide](https://github.com/I-IAL9000/shrinkerr/blob/main/docs/installation.md) — needs the NVIDIA Container Toolkit on the host.

## Links

- **Source + full documentation** — [github.com/I-IAL9000/shrinkerr](https://github.com/I-IAL9000/shrinkerr)
- **Installation guides** (Portainer, reverse proxy, Windows + WSL2) — [docs/installation.md](https://github.com/I-IAL9000/shrinkerr/blob/main/docs/installation.md)
- **Encoder tuning, VMAF, hardware decode** — [docs/encoding-guide.md](https://github.com/I-IAL9000/shrinkerr/blob/main/docs/encoding-guide.md)
- **Issues / feature requests** — [github.com/I-IAL9000/shrinkerr/issues](https://github.com/I-IAL9000/shrinkerr/issues)
- **Changelog** — [CHANGELOG.md](https://github.com/I-IAL9000/shrinkerr/blob/main/CHANGELOG.md)

## Mirrored on GHCR

Also available from GitHub's registry, if you hit Docker Hub's limits on anonymous pulls:

```bash
docker pull ghcr.io/i-ial9000/shrinkerr:latest
```

Same images, both registries published simultaneously from CI.
