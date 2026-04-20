# Stage 1: Build frontend
FROM node:20-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: Runtime
FROM nvidia/cuda:12.3.1-runtime-ubuntu22.04

# Install Python, curl, and GPG for repo signing
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3.11-venv python3-pip \
    curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Install ffmpeg with NVENC + libvmaf.
# BtbN GPL static builds include: NVENC, libvmaf, x265, and all common codecs.
#
# FFMPEG_BUILD picks which BtbN release asset to install. The NVENC SDK baked
# in determines the minimum NVIDIA driver required on the host:
#   n7.0    = NVENC SDK 12.2  → driver 525.60.13+  (DEFAULT — widest compat)
#   n7.1    = NVENC SDK 12.2  → driver 525.60.13+
#   master  = NVENC SDK 13.0  → driver 570.00+     (bleeding edge)
#
# All three are assets of the same rolling `latest` BtbN release, so the URL
# pattern is stable. Override at build time:
#   docker build --build-arg FFMPEG_BUILD=master -t shrinkerr:edge .
ARG FFMPEG_BUILD=n7.0
RUN echo "Installing ffmpeg build: ${FFMPEG_BUILD}" && \
    curl -fsSL "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-${FFMPEG_BUILD}-latest-linux64-gpl.tar.xz" \
        | tar -xJ --strip-components=2 -C /usr/local/bin/ --wildcards '*/bin/ffmpeg' '*/bin/ffprobe' && \
    chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe && \
    echo "ffmpeg installed:" && ffmpeg -version 2>&1 | head -1 && \
    echo "VMAF filter:" && (ffmpeg -filters 2>&1 | grep libvmaf || echo "NOT FOUND")

# Record the ffmpeg lineage + its NVENC-SDK driver floor so the backend can
# produce an actionable error message if the runtime NVENC test fails. Read
# by backend/nodes.py during startup-time capability detection.
ENV SHRINKERR_FFMPEG_BUILD=${FFMPEG_BUILD}
ARG NVENC_MIN_DRIVER=525.60.13
ENV SHRINKERR_NVENC_MIN_DRIVER=${NVENC_MIN_DRIVER}

# Use python3.11 as default
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy backend and version file
COPY backend/ backend/
COPY VERSION .

# Copy built frontend
COPY --from=frontend-build /app/frontend/dist frontend/dist

# Create data directory
RUN mkdir -p /app/data

EXPOSE 6680

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:6680/api/health')" || exit 1

# SHRINKERR_MODE=worker → run as a remote worker node (no server, no UI)
# Default (server) → run the FastAPI server with UI
CMD ["python3", "-m", "backend.main"]
