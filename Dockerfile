# ============================================================================
# Shrinkerr — portable, multi-arch, CPU-encoding image.
#
# Runs on:
#   - linux/amd64  (x86_64 servers, Docker Desktop on Intel/AMD, etc.)
#   - linux/arm64  (Apple Silicon, Raspberry Pi 5, Ampere cloud hosts, …)
#
# Encoding: libx265 on the CPU. This image contains NO CUDA runtime — it's
# deliberately small and portable. If your host has an NVIDIA GPU and you
# want NVENC (hardware) encoding, pull the companion image instead:
#   ghcr.io/<owner>/shrinkerr:nvenc     (built from Dockerfile.nvenc)
# Shrinkerr's runtime capability detection transparently falls back to
# libx265 in this image; the Monitor page shows "Using CPU encoding".
# ============================================================================

# Stage 1: build the frontend bundle.
# Pinning --platform=$BUILDPLATFORM makes sure buildx does the npm step on
# the native architecture of the builder host (the runner, not the target),
# so cross-compilation for arm64 doesn't re-run npm inside QEMU. The output
# is pure JS/CSS, so it's portable across target architectures.
FROM --platform=$BUILDPLATFORM node:20-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: runtime image.
# python:3.11-slim-bookworm is multi-arch (amd64 + arm64) and much smaller
# than the ubuntu/CUDA image used by the :nvenc variant.
FROM python:3.11-slim-bookworm
ARG TARGETARCH

# curl + xz-utils only needed for the ffmpeg download step below; purged at
# the end of that step so they don't bloat the final image layer.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Install ffmpeg static build from BtbN (includes libx265, libvmaf, x264).
# BtbN publishes per-architecture assets under the rolling `latest` release;
# asset filenames differ between master and tagged (nX.Y) releases.
#   master: ffmpeg-master-latest-{linux64,linuxarm64}-gpl.tar.xz
#   nX.Y:   ffmpeg-nX.Y-latest-{linux64,linuxarm64}-gpl-X.Y.tar.xz
ARG FFMPEG_BUILD=n7.1
RUN set -e; \
    case "${TARGETARCH}" in \
        amd64) ARCH_TAG="linux64" ;; \
        arm64) ARCH_TAG="linuxarm64" ;; \
        *)     echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 2 ;; \
    esac; \
    case "${FFMPEG_BUILD}" in \
        master) FF_URL="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-${ARCH_TAG}-gpl.tar.xz" ;; \
        n*)     FF_VER="${FFMPEG_BUILD#n}"; \
                FF_URL="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-${FFMPEG_BUILD}-latest-${ARCH_TAG}-gpl-${FF_VER}.tar.xz" ;; \
        *)      echo "Unknown FFMPEG_BUILD: ${FFMPEG_BUILD} (expected 'master' or 'nX.Y')" >&2; exit 2 ;; \
    esac; \
    echo "Installing ffmpeg ${FFMPEG_BUILD} for ${TARGETARCH}: ${FF_URL}"; \
    curl -fsSL "${FF_URL}" \
        | tar -xJ --strip-components=2 -C /usr/local/bin/ --wildcards '*/bin/ffmpeg' '*/bin/ffprobe'; \
    chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe; \
    apt-get purge -y curl xz-utils && apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*; \
    echo "ffmpeg installed:"; ffmpeg -version 2>&1 | head -1; \
    echo "VMAF filter:"; (ffmpeg -filters 2>&1 | grep libvmaf || echo "NOT FOUND")

# Record lineage so the running backend's Monitor page knows exactly which
# ffmpeg build the image shipped with.
ENV SHRINKERR_FFMPEG_BUILD=${FFMPEG_BUILD}
# This image has no CUDA runtime, so NVENC is structurally unavailable.
# Backend detection (backend/nodes.py) will notice no NVIDIA GPU + no
# nvidia-smi and advertise only libx265. Flag it here too so the UI can
# explain "this image is CPU-only by design" rather than "your driver's
# too old" to users pulling :latest without realising :nvenc exists.
ENV SHRINKERR_CPU_ONLY_IMAGE=1

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY backend/ backend/
COPY VERSION .
# CHANGELOG is read at runtime by /api/stats/changelog so the Updates
# section in Settings can show release notes without a network call.
COPY CHANGELOG.md .
COPY --from=frontend-build /app/frontend/dist frontend/dist

RUN mkdir -p /app/data

EXPOSE 6680

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:6680/api/health')" || exit 1

# SHRINKERR_MODE=worker → run as a remote worker node (no server, no UI).
# Default → run the FastAPI server + UI.
CMD ["python3", "-m", "backend.main"]
