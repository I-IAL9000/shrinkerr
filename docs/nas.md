# Unraid, TrueNAS and Synology

Shrinkerr runs on a NAS like any other Docker container. This page covers
what's specific to each system.

> These steps follow each system's usual way of running containers but
> haven't been tested on all of them yet. If something differs on yours,
> please [open an issue](https://github.com/I-IAL9000/shrinkerr/issues) so
> this page can be fixed.

## On every NAS

- **Mount the media at the same path Sonarr and Radarr use.** If they see
  `/data/media/movies`, give Shrinkerr the same folder at the same path. Then
  it can tell them about converted files without path mappings.
- **Keep the data folder (`/app/data`) on local storage**, ideally an SSD,
  not on a network share: it holds a SQLite database.
- **Run as the user that owns your media** with `PUID` and `PGID`, so the
  files Shrinkerr writes belong to the same user as the rest of the library.
- **Choose "keep for N days" or "delete" for originals**, not "move to the
  trash": the NAS's own recycle bin doesn't see files a container deletes,
  and the trash Shrinkerr would use is a hidden `.Trash-<uid>` folder in your
  share ([What happens to your files](what-happens-to-your-files.md)).
- The first visit asks you to set up sign-in.

Which image to use: [Hardware](hardware.md).

## Unraid

There's no Community Applications template yet, so add the container by
hand: **Docker → Add Container**.

| Field | Value |
|---|---|
| Repository | `ghcr.io/i-ial9000/shrinkerr:latest` (or `:nvenc` for an NVIDIA GPU) |
| Network type | Bridge |
| Port | `6680` → `6680` |
| Path: `/app/data` | `/mnt/user/appdata/shrinkerr` (or `/mnt/cache/appdata/shrinkerr` to keep the database off the array) |
| Path: your media | The same host and container path your Sonarr / Radarr containers use, e.g. `/mnt/user/data` → `/data` |
| Variable: `PUID` | `99` (Unraid's `nobody`) |
| Variable: `PGID` | `100` (`users`) |

**NVIDIA GPU:** install the **Nvidia Driver** plugin from Apps and pick a
driver of version **570 or newer**. Then, in the container, use the `:nvenc`
image, add `--runtime=nvidia` under **Extra Parameters** (Advanced View), and
add two variables: `NVIDIA_VISIBLE_DEVICES` = your GPU's UUID from the
plugin's page (or `all`), and `NVIDIA_DRIVER_CAPABILITIES` = `all`.

**Intel or AMD GPU:** add a **Device**: `/dev/dri`. On Intel the **Intel GPU
TOP** plugin makes sure the GPU driver is loaded.

## TrueNAS SCALE

TrueNAS SCALE 24.10 (Electric Eel) and newer run apps with Docker. Use
**Apps → Discover Apps → Custom App → Install via YAML** and paste a compose
file like this:

```yaml
services:
  shrinkerr:
    image: ghcr.io/i-ial9000/shrinkerr:latest
    ports:
      - "6680:6680"
    environment:
      - PUID=568    # TrueNAS's "apps" user, or the owner of your media dataset
      - PGID=568
    volumes:
      - /mnt/tank/apps/shrinkerr:/app/data
      - /mnt/tank/data:/data       # the same path your Sonarr / Radarr apps use
    restart: unless-stopped
```

- **Permissions:** the user in `PUID` needs modify rights on the media
  dataset (Datasets → your dataset → Permissions).
- **NVIDIA GPU:** turn on **Apps → Configuration → Settings → Install NVIDIA
  Drivers**, then check the driver's version in **System → Shell** with
  `nvidia-smi`. The `:nvenc` image needs **570 or newer**; if your TrueNAS
  version ships an older driver, use `:latest` (CPU), or encode on another
  machine as a [remote worker](remote-workers.md). With a new enough driver,
  switch the image to `:nvenc` and add:
  ```yaml
      deploy:
        resources:
          reservations:
            devices:
              - driver: nvidia
                count: all
                capabilities: [gpu, video, compute, utility]
  ```
- **Intel or AMD GPU:** add `devices: ["/dev/dri:/dev/dri"]`.

## Synology

On DSM 7.2 and newer, use **Container Manager → Project → Create**. Choose a
folder for the project (e.g. `/volume1/docker/shrinkerr`) and paste a
compose file like this:

```yaml
services:
  shrinkerr:
    image: ghcr.io/i-ial9000/shrinkerr:latest
    ports:
      - "6680:6680"
    environment:
      - PUID=1026   # your user's id: run `id yourname` over SSH
      - PGID=100    # Synology's "users" group
    volumes:
      - /volume1/docker/shrinkerr/data:/app/data
      - /volume1/data:/data          # the same path your Sonarr / Radarr use
    restart: unless-stopped
```

- **Intel models with a GPU** (most "+" models with a Celeron or Pentium,
  such as the DS920+) can use Quick Sync or VAAPI: add
  `devices: ["/dev/dri:/dev/dri"]`. Models with an AMD Ryzen or Atom C
  processor have no video encoder, so they encode on the CPU.
- **ARM models**: the `:latest` image runs on 64-bit ARM, slowly. 32-bit ARM
  models can't run it.
- Encoding on a NAS processor is slow. A desktop with a GPU can do the work
  as a [remote worker](remote-workers.md) while the NAS runs Shrinkerr.
