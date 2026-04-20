#!/usr/bin/env bash
# ============================================================================
# build-images.sh — build Shrinkerr's image variants locally
#
# Four tags. Two lineages × two ffmpeg builds:
#
#   Portable (multi-arch, CPU-only, libx265 encoding) — runs on anything:
#     shrinkerr:latest       Dockerfile,        ffmpeg n7.1
#     shrinkerr:edge         Dockerfile,        ffmpeg master
#
#   NVENC (x86_64 only, CUDA base, hardware encoding) — NVIDIA hosts:
#     shrinkerr:nvenc        Dockerfile.nvenc,  ffmpeg n7.1
#     shrinkerr:edge-nvenc   Dockerfile.nvenc,  ffmpeg master
#
# Usage:
#   ./scripts/build-images.sh                # all four tags
#   ./scripts/build-images.sh latest         # just one
#   ./scripts/build-images.sh latest nvenc   # a subset
#   PUSH=1 REGISTRY=ghcr.io/me ./scripts/build-images.sh
#
# Notes:
#   - By default, multi-arch tags are built for the HOST'S architecture only
#     (fast local build). To actually produce a multi-platform manifest (amd64
#     + arm64), set MULTIARCH=1 — requires `docker buildx` with QEMU set up
#     and will be much slower. CI does this automatically.
#   - The :nvenc and :edge-nvenc variants are always linux/amd64; building
#     them on a non-amd64 host requires QEMU (via `docker run --privileged
#     --rm tonistiigi/binfmt --install all`) and is painfully slow.
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

REGISTRY="${REGISTRY:-}"
PUSH="${PUSH:-0}"
MULTIARCH="${MULTIARCH:-0}"

tag_for() {
    local name="$1"
    if [[ -n "$REGISTRY" ]]; then
        echo "${REGISTRY}/shrinkerr:${name}"
    else
        echo "shrinkerr:${name}"
    fi
}

build_one() {
    local name="$1" dockerfile="$2" ffmpeg_build="$3" platforms="$4"
    local tag
    tag=$(tag_for "$name")
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Building $tag"
    echo "  Dockerfile: $dockerfile"
    echo "  ffmpeg:     $ffmpeg_build"
    echo "  platforms:  $platforms"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    local build_cmd=(docker)
    # Multi-platform builds require buildx. Single-platform builds can use
    # the plain docker daemon for a faster local loop.
    if [[ "$platforms" == *","* ]]; then
        build_cmd+=(buildx build --platform "$platforms")
        if [[ "$PUSH" == "1" && -n "$REGISTRY" ]]; then
            build_cmd+=(--push)
        else
            # Buildx without --push doesn't load multi-arch into the local
            # daemon by default; add --load for single-arch, warn otherwise.
            echo "Note: multi-arch buildx output is not loaded into the local Docker daemon." >&2
            echo "      Set PUSH=1 + REGISTRY=<prefix> to push, or drop MULTIARCH=1 for a local single-arch image." >&2
        fi
    else
        build_cmd+=(build --platform "$platforms")
    fi

    build_cmd+=(
        --build-arg "FFMPEG_BUILD=${ffmpeg_build}"
        -f "$dockerfile"
        -t "$tag"
        .
    )

    "${build_cmd[@]}"

    if [[ "$PUSH" == "1" && "$platforms" != *","* ]]; then
        if [[ -z "$REGISTRY" ]]; then
            echo "PUSH=1 set but REGISTRY is empty — skipping push." >&2
        else
            echo "Pushing $tag..."
            docker push "$tag"
        fi
    fi
}

# Map each logical tag to (dockerfile, ffmpeg_build, platforms).
# Platforms default to the host arch for a fast local build; MULTIARCH=1
# expands the portable variants to amd64+arm64 (requires buildx+qemu).
host_platform() { echo "linux/$(docker version --format '{{.Server.Arch}}' 2>/dev/null || uname -m | sed 's/x86_64/amd64/; s/aarch64/arm64/')"; }
HOST_PLATFORM="$(host_platform)"
PORTABLE_PLATFORMS="$HOST_PLATFORM"
if [[ "$MULTIARCH" == "1" ]]; then
    PORTABLE_PLATFORMS="linux/amd64,linux/arm64"
fi

build_variant() {
    case "$1" in
        latest)      build_one "latest"      "Dockerfile"        "n7.1"   "$PORTABLE_PLATFORMS" ;;
        edge)        build_one "edge"        "Dockerfile"        "master" "$PORTABLE_PLATFORMS" ;;
        nvenc)       build_one "nvenc"       "Dockerfile.nvenc"  "n7.1"   "linux/amd64" ;;
        edge-nvenc)  build_one "edge-nvenc"  "Dockerfile.nvenc"  "master" "linux/amd64" ;;
        *)
            echo "Unknown variant: $1" >&2
            echo "Valid: latest, edge, nvenc, edge-nvenc" >&2
            exit 2
            ;;
    esac
}

if [[ $# -eq 0 ]]; then
    # No args → build all four.
    for v in latest edge nvenc edge-nvenc; do
        build_variant "$v"
    done
else
    for v in "$@"; do
        build_variant "$v"
    done
fi

echo ""
echo "Done. Current Shrinkerr images on this host:"
docker images --filter=reference='*shrinkerr*' --format 'table {{.Repository}}:{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}' || true
