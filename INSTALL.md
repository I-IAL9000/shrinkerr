# Shrinkarr — Installation Guide

## Prerequisites

- Docker with NVIDIA Container Toolkit (`nvidia-docker2`)
- NVIDIA GPU with NVENC support (e.g., Quadro P2200)
- Portainer (for web-based management)

## Step 1: Copy to NUC

Copy the `shrinkarr/` directory to your NUC:

```bash
scp -r shrinkarr/ hal9000@<nuc-ip>:/home/hal9000/shrinkarr
```

## Step 2: Build the Docker Image

SSH into the NUC and build:

```bash
ssh hal9000@<nuc-ip>
cd /home/hal9000/shrinkarr
docker build -t shrinkarr:latest .
```

This runs a multi-stage build (Node.js for frontend, nvidia/cuda for runtime). Takes a few minutes on first build.

## Step 3: Deploy via Portainer

1. Open Portainer (`http://<nuc-ip>:9000`)
2. Go to **Stacks** → **Add stack**
3. Name: `shrinkarr`
4. Choose **Web editor** and paste this compose file:

```yaml
services:
  shrinkarr:
    image: shrinkarr:latest
    container_name: shrinkarr
    ports:
      - "6680:6680"
    volumes:
      - /home/hal9000/shrinkarr/data:/app/data
      - /home/hal9000/HALHUB:/media:rw
    runtime: nvidia
    environment:
      - NVIDIA_VISIBLE_DEVICES=all
      - SHRINKARR_DB_PATH=/app/data/shrinkarr.db
      - SHRINKARR_MEDIA_ROOT=/media
    restart: unless-stopped
```

5. Click **Deploy the stack**

## Step 4: First-Time Setup

1. Open `http://<nuc-ip>:6680` in your browser
2. Go to **Settings** and add your media directories:
   - `/media/M2T2/TV4`
   - `/media/M2T2/KrakkaBio IS`
   - `/media/Misc/Movies2`
   - `/media/Misc/Movies3`
   - `/media/Misc/HD 2025`
   - `/media/Movies/HD 1900`
   - `/media/Movies/HD 2000`
   - `/media/Movies/HD 2010`
   - `/media/Movies/HD 2020`
   - `/media/Movies/ISL KrakkaBio`
   - `/media/Movies/ISL Movies`
   - `/media/TV1/TV1`
3. Go to **Scanner** → select paths → click **Scan**
4. Review results, adjust audio track selections
5. Click **Add selected to queue**
6. Go to **Queue** → **Start** (or **Schedule** to start later)

## Updating

When you update the code:

```bash
cd /home/hal9000/shrinkarr
git pull  # or copy updated files
docker build -t shrinkarr:latest .
```

Then in Portainer: go to the `shrinkarr` stack → click **Stop** → **Start** (or recreate the container). The SQLite database in `/home/hal9000/shrinkarr/data/` persists across rebuilds.

## Ports

| Service    | Port |
|------------|------|
| Shrinkarr  | 6680 |
| Sonarr     | 8989 |
| Radarr     | 7878 |
| Portainer  | 9000 |

## Troubleshooting

**Container won't start with GPU:**
```bash
# Verify NVIDIA runtime is available
docker run --rm --runtime=nvidia nvidia/cuda:12.3.1-runtime-ubuntu22.04 nvidia-smi
```

**Check container logs:**
```bash
docker logs shrinkarr
```

**ffmpeg not detecting GPU:**
Ensure `NVIDIA_VISIBLE_DEVICES=all` is set and the NVIDIA Container Toolkit is installed:
```bash
sudo apt install nvidia-docker2
sudo systemctl restart docker
```
