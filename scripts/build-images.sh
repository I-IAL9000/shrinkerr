#!/usr/bin/env bash
# ============================================================================
# build-images.sh — build Shrinkerr's two image tags locally
#
#   shrinkerr:latest — ffmpeg n7.1 (NVENC SDK 12.2, driver 525.60.13+)
#                      Default. Works on most hosts with a reasonably recent
#                      NVIDIA driver. Pick this for production.
#
#   shrinkerr:edge   — ffmpeg master (NVENC SDK 13.0, driver 570.00+)
#                      Bleeding edge — latest ffmpeg features. Requires a
#                      newer NVIDIA driver but gives you any fixes/features
#                      that haven't landed in a tagged release yet.
#
# Usage:
#   ./scripts/build-images.sh                 # build both tags
#   ./scripts/build-images.sh latest          # build only :latest
#   ./scripts/build-images.sh edge            # build only :edge
#   PUSH=1 ./scripts/build-images.sh          # also docker push (set REGISTRY)
#   REGISTRY=ghcr.io/youruser ./scripts/build-images.sh
#
# On a multi-arch host you can also build the ARM64 worker image with:
#   docker build -f Dockerfile.worker-arm64 -t shrinkerr:arm64-worker .
# That's intentionally not covered here — ARM64 builds need buildx/qemu
# setup and most users don't need them.
# ============================================================================
set -euo pipefail

# Move to repo root regardless of where this is invoked from.
cd "$(dirname "$0")/.."

# Prefix all tags with $REGISTRY when set (e.g. "ghcr.io/myuser").
# Empty = purely local tags.
REGISTRY="${REGISTRY:-}"
PUSH="${PUSH:-0}"

# Which tag(s) to build. First positional arg, or "both" if omitted.
TARGET="${1:-both}"

tag_for() {
    local name="$1"
    if [[ -n "$REGISTRY" ]]; then
        echo "${REGISTRY}/shrinkerr:${name}"
    else
        echo "shrinkerr:${name}"
    fi
}

build_one() {
    local name="$1" ffmpeg_build="$2" min_driver="$3"
    local tag
    tag=$(tag_for "$name")
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Building $tag (ffmpeg: $ffmpeg_build, min NVIDIA driver: $min_driver)"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    docker build \
        --build-arg "FFMPEG_BUILD=${ffmpeg_build}" \
        --build-arg "NVENC_MIN_DRIVER=${min_driver}" \
        -t "$tag" \
        .
    if [[ "$PUSH" == "1" ]]; then
        if [[ -z "$REGISTRY" ]]; then
            echo "PUSH=1 set but REGISTRY is empty — skipping push (nothing to push to)." >&2
        else
            echo "Pushing $tag..."
            docker push "$tag"
        fi
    fi
}

case "$TARGET" in
    latest)
        build_one "latest" "n7.1"   "525.60.13"
        ;;
    edge)
        build_one "edge"   "master" "570.00"
        ;;
    both|"")
        build_one "latest" "n7.1"   "525.60.13"
        build_one "edge"   "master" "570.00"
        ;;
    *)
        echo "Unknown target: $TARGET" >&2
        echo "Valid targets: latest, edge, both (default)" >&2
        exit 2
        ;;
esac

echo ""
echo "Done. Current Shrinkerr images on this host:"
docker images --filter=reference='*shrinkerr*' --format 'table {{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}'
