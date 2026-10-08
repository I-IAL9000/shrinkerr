#!/bin/sh
# Shrinkerr container entrypoint (v0.10.0).
#
# Opt-in PUID / PGID / UMASK, as in the linuxserver.io images: when PUID or
# PGID is set, Shrinkerr runs as that user instead of root, so the files it
# writes (converted media, backups, its data folder) are owned like the ones
# Sonarr / Radarr / Plex write. Without them it runs as root, as before.
set -e

if [ -n "$UMASK" ]; then
    umask "$UMASK"
fi

if [ -z "$PUID" ] && [ -z "$PGID" ]; then
    exec "$@"
fi

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$PUID" = "0" ]; then
    exec "$@"  # explicitly root
fi

group="$(getent group "$PGID" | cut -d: -f1)"
if [ -z "$group" ]; then
    groupadd -o -g "$PGID" shrinkerr
    group=shrinkerr
fi
user="$(getent passwd "$PUID" | cut -d: -f1)"
if [ -z "$user" ]; then
    useradd -o -u "$PUID" -g "$PGID" -M -d /app/data/home -s /usr/sbin/nologin shrinkerr
    user=shrinkerr
fi

# Hardware encoders: join the groups that own the GPU device nodes
# (/dev/dri/renderD* for QSV / VAAPI; NVIDIA's are usually world-accessible).
for dev in /dev/dri/* /dev/nvidia*; do
    [ -e "$dev" ] || continue
    gid="$(stat -c %g "$dev")"
    [ "$gid" = "0" ] && continue
    devgroup="$(getent group "$gid" | cut -d: -f1)"
    if [ -z "$devgroup" ]; then
        devgroup="hostgpu$gid"
        groupadd -o -g "$gid" "$devgroup"
    fi
    usermod -aG "$devgroup" "$user" || echo "[ENTRYPOINT] Could not add $user to group $devgroup ($dev)"
done

# The data folder (database, posters, models) must be writable. Only files
# that don't already belong to the user are touched.
# A share that refuses chown (data on a NAS) only gets a warning.
data_dir="${SHRINKERR_DATA_DIR:-/app/data}"
db_dir="$(dirname "${SHRINKERR_DB_PATH:-$data_dir/shrinkerr.db}")"
for dir in "$data_dir" "$db_dir"; do
    mkdir -p "$dir"
    find "$dir" \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown "$PUID:$PGID" {} + \
        || echo "[ENTRYPOINT] Could not hand $dir to $PUID:$PGID; Shrinkerr may not be able to write there"
    [ "$db_dir" = "$data_dir" ] && break
done
mkdir -p "$data_dir/home"
chown "$PUID:$PGID" "$data_dir/home" || true

echo "[ENTRYPOINT] Running as $user ($PUID:$PGID)"
export HOME="$data_dir/home"
exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups "$@"
