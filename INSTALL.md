# Squeezarr — Installation Guide

## Prerequisites

- Docker with NVIDIA Container Toolkit (`nvidia-docker2`)
- NVIDIA GPU with NVENC support (e.g., Quadro P2200)
- Portainer (for web-based management)

## Step 1: Copy to NUC

Copy the `squeezarr/` directory to your NUC:

```bash
scp -r squeezarr/ hal9000@<nuc-ip>:/home/hal9000/squeezarr
```

## Step 2: Build the Docker Image

SSH into the NUC and build:

```bash
ssh hal9000@<nuc-ip>
cd /home/hal9000/squeezarr
docker build -t squeezarr:latest .
```

This runs a multi-stage build (Node.js for frontend, nvidia/cuda for runtime). Takes a few minutes on first build.

## Step 3: Deploy via Portainer

1. Open Portainer (`http://<nuc-ip>:9000`)
2. Go to **Stacks** → **Add stack**
3. Name: `squeezarr`
4. Choose **Web editor** and paste this compose file:

```yaml
services:
  squeezarr:
    image: squeezarr:latest
    container_name: squeezarr
    ports:
      - "6680:6680"
    volumes:
      - /home/hal9000/squeezarr/data:/app/data
      - /home/hal9000/HALHUB:/media:rw
    environment:
      - NVIDIA_VISIBLE_DEVICES=all
      - SQUEEZARR_DB_PATH=/app/data/squeezarr.db
      - SQUEEZARR_MEDIA_ROOT=/media
    restart: unless-stopped
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
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
cd /home/hal9000/squeezarr
git pull  # or copy updated files
docker build -t squeezarr:latest .
```

Then in Portainer: go to the `squeezarr` stack → click **Stop** → **Start** (or recreate the container). The SQLite database in `/home/hal9000/squeezarr/data/` persists across rebuilds.

## Ports

| Service    | Port |
|------------|------|
| Squeezarr  | 6680 |
| Sonarr     | 8989 |
| Radarr     | 7878 |
| Portainer  | 9000 |

## Troubleshooting

**"unknown or invalid runtime name: nvidia":**

Install the NVIDIA Container Toolkit (replaces the old `nvidia-docker2`):

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
  sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

**Verify GPU access in Docker:**
```bash
docker run --rm --gpus all nvidia/cuda:12.3.1-runtime-ubuntu22.04 nvidia-smi
```

**Check container logs:**
```bash
docker logs squeezarr
```

**ffmpeg not detecting GPU:**
Ensure `NVIDIA_VISIBLE_DEVICES=all` is set in the compose environment and the container starts with `nvidia-smi` showing your GPU.
