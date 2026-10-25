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

# BtbN ffmpeg build to install, master or nX.Y (ffmpeg-download stage).
ARG FFMPEG_BUILD=n8.1

# Stage 1: build the frontend bundle.
# Pinning --platform=$BUILDPLATFORM makes sure buildx does the npm step on
# the native architecture of the builder host (the runner, not the target),
# so cross-compilation for arm64 doesn't re-run npm inside QEMU. The output
# is pure JS/CSS, so it's portable across target architectures.
FROM --platform=$BUILDPLATFORM mirror.gcr.io/library/node:20-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: subtile-ocr, the VobSub (DVD subtitle) OCR tool (v0.10.0). The
# NVENC image has built it since v0.9.8; this one never had it, so DVD
# subtitles were neither language-detected nor converted to SRT. It's a Rust
# build (a rustup toolchain — subtile-ocr's deps need Cargo >= 1.85) against
# the runtime's tesseract / leptonica: bookworm's, for the target architecture
# through Debian multiarch. It compiles on the build machine's architecture
# and cross-compiles for the target's — compiled for arm64 under QEMU it took
# ~25 min, and on a cold cache (the base image is re-pushed upstream now and
# then) the arm64 compiles ran CI past its 30-minute limit. Its own stage
# keeps the toolchain out of the image. GUARDED: a failed build leaves /out
# empty, and the runtime code is fail-open (VobSub tracks stay images / und).
FROM --platform=$BUILDPLATFORM mirror.gcr.io/library/python:3.11-slim-bookworm AS subtile-ocr-build
ARG BUILDARCH
ARG TARGETARCH
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) GNU=x86_64-linux-gnu ;; \
        arm64) GNU=aarch64-linux-gnu ;; \
        *)     echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 2 ;; \
    esac; \
    RUST_TARGET="${GNU%%-*}-unknown-linux-gnu"; \
    RUST_TARGET_VAR="$(echo "${RUST_TARGET}" | tr '-' '_')"; \
    GCC=gcc; \
    if [ "${TARGETARCH}" != "${BUILDARCH}" ]; then \
        dpkg --add-architecture "${TARGETARCH}"; \
        GCC="gcc-$(echo "${GNU}" | tr '_' '-')"; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl "${GCC}" clang libclang-dev pkg-config \
        "libc6-dev:${TARGETARCH}" "libtesseract-dev:${TARGETARCH}" "libleptonica-dev:${TARGETARCH}"; \
    mkdir -p /out; \
    ( export RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo \
        PKG_CONFIG_ALLOW_CROSS=1 PKG_CONFIG_LIBDIR="/usr/lib/${GNU}/pkgconfig:/usr/share/pkgconfig" \
        "CC_${RUST_TARGET_VAR}=${GNU}-gcc" \
        "CARGO_TARGET_$(echo "${RUST_TARGET_VAR}" | tr 'a-z' 'A-Z')_LINKER=${GNU}-gcc" \
        && curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable --target "${RUST_TARGET}" \
        && /opt/cargo/bin/cargo install subtile-ocr --version 0.2.6 --target "${RUST_TARGET}" --root /out \
        && /out/bin/subtile-ocr --version ) \
        || echo "[BUILD] subtile-ocr build failed — VobSub OCR will be unavailable"; \
    rm -f /out/.crates.toml /out/.crates2.json

# dovi_tool (v0.10.0): keeps Dolby Vision through a conversion (backend/dovi.py):
# it takes the RPU from the original and puts it into the new HEVC stream.
# The release's static (musl) binary, pinned by checksum: upstream builds it
# with the built-in font (its plot command's), as we did, so it needs no
# fontconfig. Compiling it here took ~20 min for arm64 under QEMU. GUARDED: a
# failed download leaves /out empty, and Dolby Vision files then stay
# unconverted, as before.
FROM --platform=$BUILDPLATFORM mirror.gcr.io/library/python:3.11-slim-bookworm AS dovi-build
ARG TARGETARCH
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) DOVI_ARCH=x86_64;  DOVI_SHA256=1844258e13c26607b32224bf1fa82b595d3b35949f5467405fda560daad32b3f ;; \
        arm64) DOVI_ARCH=aarch64; DOVI_SHA256=b4f22a7db56954efe4ed8d02276d0f991799e84602f3584711be6c9df950cb15 ;; \
        *)     echo "Unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 2 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    mkdir -p /out; \
    ( curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors -o /tmp/dovi_tool.tar.gz \
          "https://github.com/quietvoid/dovi_tool/releases/download/2.3.4/dovi_tool-2.3.4-${DOVI_ARCH}-unknown-linux-musl.tar.gz" \
        && echo "${DOVI_SHA256}  /tmp/dovi_tool.tar.gz" | sha256sum -c - \
        && mkdir -p /out/bin && tar -xzf /tmp/dovi_tool.tar.gz -C /out/bin ./dovi_tool \
        && /out/bin/dovi_tool --version ) \
        || echo "[BUILD] dovi_tool download failed — Dolby Vision files won't be converted"

# ffmpeg: BtbN's static build (includes libx265, libvmaf, x264).
# BtbN publishes per-architecture assets under the rolling `latest` release;
# asset filenames differ between master and tagged (nX.Y) releases.
#   master: ffmpeg-master-latest-{linux64,linuxarm64}-gpl.tar.xz
#   nX.Y:   ffmpeg-nX.Y-latest-{linux64,linuxarm64}-gpl-X.Y.tar.xz
# BtbN replaces these with a new build every day under the same URLs, but a
# RUN is cached by its text, so the image kept whatever ffmpeg was current when
# the cache last missed. The workflow passes the assets' digests in
# FFMPEG_DIGESTS: a new BtbN build reruns the download. Downloaded and unpacked
# on the build machine's architecture, so the arm64 leg doesn't unpack it under
# QEMU (the binaries need the target's glibc, so they only run in the runtime
# stage); copied in after the pip install, so a new ffmpeg rebuilds nothing
# before it.
FROM --platform=$BUILDPLATFORM mirror.gcr.io/library/python:3.11-slim-bookworm AS ffmpeg-download
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl xz-utils
ARG TARGETARCH
ARG FFMPEG_BUILD
ARG FFMPEG_DIGESTS=""
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
    echo "Installing ffmpeg ${FFMPEG_BUILD} for ${TARGETARCH}: ${FF_URL} (BtbN digests: ${FFMPEG_DIGESTS:-not given})"; \
    # v0.9.105: download to a file with retries, THEN extract. Piping curl
    # straight into tar can't retry a mid-stream cut (tar has already consumed
    # a partial stream) — that flaked the arm64 leg under slow QEMU emulation.
    curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors "${FF_URL}" -o /tmp/ffmpeg.tar.xz; \
    mkdir -p /out; \
    tar -xJf /tmp/ffmpeg.tar.xz --strip-components=2 -C /out/ --wildcards '*/bin/ffmpeg' '*/bin/ffprobe'; \
    rm -f /tmp/ffmpeg.tar.xz; \
    chmod +x /out/ffmpeg /out/ffprobe

# Stage 3: runtime image.
# python:3.11-slim-bookworm is multi-arch (amd64 + arm64) and much smaller
# than the ubuntu/CUDA image used by the :nvenc variant.
#
# v0.7.4: pulled from Google's mirror.gcr.io instead of docker.io to avoid
# Docker Hub anonymous + authenticated pull rate limits (200/6h on free)
# from cooking the CI matrix (4 variants × 2 archs × multi-stage). The
# mirror is a byte-for-byte proxy of Docker Hub's `library/*` namespace,
# requires no auth, and has no rate limit. Same applies to the node:20-slim
# pull in stage 1 above, and to Dockerfile.nvenc.
FROM mirror.gcr.io/library/python:3.11-slim-bookworm
ARG TARGETARCH

# curl is only needed for the libva / vpl-gpu-rt downloads below, and xz-utils
# no longer at all (ffmpeg comes from its own stage; taking it out of this list
# would rebuild this slow layer). Both are purged in the next step.
#
# VA-API runtime libraries (~30 MB total) let ffmpeg's hevc_qsv (Intel
# Quick Sync) and hevc_vaapi (Intel + AMD generic VA-API) actually run
# when the host passes /dev/dri through. Harmless on hosts without an
# Intel/AMD GPU — ffmpeg simply doesn't surface those encoders. v0.3.67+.
#
# Cross-arch note (v0.3.71): the Intel iHD + i965 drivers are AMD64-only
# in Debian (Intel iGPUs don't exist on ARM, and there's no upstream arm64
# build of `intel-media-va-driver-non-free`). Without this gate the v0.3.67
# build broke the multi-arch publish for several releases — the arm64 leg
# failed with `Unable to locate package intel-media-va-driver-non-free`.
# The arm64 leg keeps the arch-agnostic packages (libva, mesa-va-drivers,
# vainfo) which are still useful for AMD GPUs reachable on arm64 hosts
# (rare but not impossible — Ampere + Radeon, Pi + USB-C eGPU, etc.).
#
#   libva2 + libva-drm2            → core VA-API runtime (multi-arch)
#   mesa-va-drivers                → AMD radeonsi (multi-arch)
#   vainfo                         → diagnostic (multi-arch)
#   intel-media-va-driver-non-free → Intel iHD (Gen9+: Skylake / Arc) — AMD64 ONLY
#   i965-va-driver                 → Intel legacy (Gen8 and older) — AMD64 ONLY
#
# `intel-media-va-driver-non-free` lives in Debian's `non-free` component;
# enabled below before the conditional install. Source-open driver,
# "non-free" naming is historical from when it shipped as a binary blob.
RUN set -eux; \
    apt-get update; \
    # v0.5.13: pull latest security patches at build time. Halves the
    # High/Medium CVE count Docker Scout reports without changing the
    # base image. Cheap and low-risk — security updates are minor
    # bumps within the bookworm release line.
    apt-get upgrade -y --no-install-recommends; \
    apt-get install -y --no-install-recommends curl xz-utils libarchive-tools libbluray-bin mkvtoolnix \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim tesseract-ocr-chi-tra tesseract-ocr-jpn tesseract-ocr-kor tesseract-ocr-rus tesseract-ocr-ara \
        tesseract-ocr-fra tesseract-ocr-deu tesseract-ocr-spa tesseract-ocr-ita tesseract-ocr-por tesseract-ocr-nld tesseract-ocr-swe tesseract-ocr-dan tesseract-ocr-nor tesseract-ocr-fin tesseract-ocr-isl tesseract-ocr-pol tesseract-ocr-ces tesseract-ocr-hun tesseract-ocr-tur tesseract-ocr-ell tesseract-ocr-heb \
        libgl1 libglib2.0-0; \
    if [ "${TARGETARCH}" = "amd64" ]; then \
        # All VA-API packages (runtime libs + Intel iHD + AMD radeonsi +
        # diagnostic) only on amd64. arm64 builds keep the minimal
        # pre-v0.3.67 install since Intel iGPUs are x86-only and AMD
        # GPUs on arm64 hosts are exceptionally rare.
        #
        # Enable non-free + non-free-firmware components by EDITING the
        # existing DEB822 sources file (python:3.11-slim-bookworm uses
        # `/etc/apt/sources.list.d/debian.sources`) rather than dropping
        # a second `.list`. Adding a parallel `bookworm` repo without a
        # `Signed-By:` directive makes apt fail with
        #   E: Conflicting values set for option Signed-By regarding
        #      source http://deb.debian.org/debian/ bookworm:
        #      /usr/share/keyrings/debian-archive-keyring.gpg !=
        # which is what bit v0.3.67 → v0.3.73. The sed targets both the
        # `bookworm` and `bookworm-security` stanzas in the same file
        # (one substitution per Components line). v0.3.74+.
        sed -i 's|^Components: main$|Components: main contrib non-free non-free-firmware|' \
            /etc/apt/sources.list.d/debian.sources; \
        apt-get update; \
        apt-get install -y --no-install-recommends \
            libva-drm2 \
            mesa-va-drivers \
            vainfo \
            intel-media-va-driver-non-free \
            i965-va-driver \
            libvpl2 libmfx1 \
            build-essential cmake meson ninja-build pkg-config \
            libdrm-dev libigdgmm-dev libpciaccess-dev bzip2; \
            # libvpl2: Intel oneVPL dispatcher. libmfx1: legacy MediaSDK
            # runtime — kept as a fallback for older HW; the modern
            # libmfx-gen runtime built below takes precedence on Gen9+.
            # build-essential + cmake + meson + ninja + dev headers:
            # temporarily installed for the libva and vpl-gpu-rt builds
            # below. Purged at the end of the layer.
        #
        # === libva 2.22 from source (v0.3.88+) ============================
        #
        # The BtbN n7.x ffmpeg static builds are compiled against
        # libva ≥ 2.20 (which introduced `vaMapBuffer2`). Debian
        # bookworm ships libva 2.17 even via backports — every apt-
        # installable libva on bookworm is too old, and the runtime
        # symbol lookup for `vaMapBuffer2` fails the first time
        # hevc_vaapi tries to encode. We replace the system libva with
        # an upstream 2.22.x build. libva 2.x is ABI-stable, so the
        # iHD / i965 / mesa drivers (compiled against 2.17) continue
        # to work against 2.22 without rebuilding. ~30s build.
        LIBVA_VER=2.22.0; \
        curl -fsSL "https://github.com/intel/libva/releases/download/${LIBVA_VER}/libva-${LIBVA_VER}.tar.bz2" -o /tmp/libva.tar.bz2; \
        mkdir -p /tmp/libva-src; \
        tar xjf /tmp/libva.tar.bz2 -C /tmp/libva-src --strip-components=1; \
        cd /tmp/libva-src; \
        meson setup build --prefix=/usr -Dlibdir=lib/x86_64-linux-gnu --buildtype=release; \
        ninja -C build install; \
        cd /; rm -rf /tmp/libva-src /tmp/libva.tar.bz2; \
        ldconfig; \
        #
        # === vpl-gpu-rt (libmfx-gen) from source (v0.3.89+) ===============
        #
        # Intel's modern oneVPL GPU runtime — what Sonarr/Radarr-tier
        # users on current Ubuntu/Arch get from `libmfx-gen1` package.
        # Not packaged for Debian 12 or Ubuntu 22.04. Without this,
        # libvpl2 (dispatcher) falls back to legacy libmfx1, which
        # rejects libva-2.22 VADisplays with `MFX_ERR_INCOMPATIBLE_-
        # VIDEO_PARAM (-17)` — that's what bit users testing v0.3.84
        # → v0.3.88. With libmfx-gen.so.1.2 installed, the dispatcher
        # finds the modern runtime first and QSV works against modern
        # libva + iHD drivers. ~3 min build on x86_64.
        VPL_VER=24.4.4; \
        curl -fsSL "https://github.com/intel/vpl-gpu-rt/archive/refs/tags/intel-onevpl-${VPL_VER}.tar.gz" -o /tmp/vpl.tar.gz; \
        mkdir -p /tmp/vpl-src; \
        tar xzf /tmp/vpl.tar.gz -C /tmp/vpl-src --strip-components=1; \
        cd /tmp/vpl-src; \
        # Memory-conservative configure (v0.3.92+). The default
        # `cmake --build --parallel` uses every core, and a
        # multi-arch buildx build runs both leg's compile/link
        # steps concurrently — easy way to exceed 7 GB on GHA
        # free runners and OOM-kill the runner mid-link
        # (lost-communication errors at the mfx_common_hw stage).
        # Mitigations:
        #   MinSizeRel + IPO=OFF  → smaller object files, less
        #                           template-instantiation RAM
        #   BUILD_TESTS=OFF       → skip test-suite compilation
        #   --parallel 1          → serialize compile + link;
        #                           slower (~10 min vs ~5 min)
        #                           but stays under the budget
        cmake -B build \
            -DCMAKE_BUILD_TYPE=MinSizeRel \
            -DCMAKE_INSTALL_PREFIX=/usr \
            -DCMAKE_INSTALL_LIBDIR=lib/x86_64-linux-gnu \
            -DCMAKE_INTERPROCEDURAL_OPTIMIZATION=OFF \
            -DBUILD_TESTS=OFF; \
        cmake --build build --parallel 1; \
        cmake --install build; \
        cd /; rm -rf /tmp/vpl-src /tmp/vpl.tar.gz; \
        ldconfig; \
        #
        # Purge build toolchain — keep the image lean. The runtime libs
        # (libva-drm2, mesa-va-drivers, libvpl2, libmfx1, …) stay
        # because they were explicitly installed above; apt sees them as
        # user-installed and won't auto-remove them.
        apt-get purge -y build-essential cmake meson ninja-build pkg-config libdrm-dev libigdgmm-dev libpciaccess-dev bzip2; \
        apt-get autoremove -y; \
    else \
        echo "Skipping VA-API runtime on ${TARGETARCH} (amd64-only in this image)"; \
    fi; \
    rm -rf /var/lib/apt/lists/*

RUN apt-get purge -y curl xz-utils && apt-get autoremove -y

# This image has no CUDA runtime, so NVENC is structurally unavailable.
# Backend detection (backend/nodes.py) will notice no NVIDIA GPU + no
# nvidia-smi and advertise only libx265. Flag it here too so the UI can
# explain "this image is CPU-only by design" rather than "your driver's
# too old" to users pulling :latest without realising :nvenc exists.
ENV SHRINKERR_CPU_ONLY_IMAGE=1

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# ffmpeg (ffmpeg-download stage).
COPY --from=ffmpeg-download /out/ /usr/local/bin/
RUN echo "ffmpeg installed:"; ffmpeg -version 2>&1 | head -1; \
    echo "VMAF filter:"; (ffmpeg -filters 2>&1 | grep libvmaf || echo "NOT FOUND")
# Record lineage so the running backend's Monitor page knows exactly which
# ffmpeg build the image shipped with.
ARG FFMPEG_BUILD
ENV SHRINKERR_FFMPEG_BUILD=${FFMPEG_BUILD}

COPY backend/ backend/
COPY VERSION .
# CHANGELOG is read at runtime by /api/stats/changelog so the Updates
# section in Settings can show release notes without a network call.
COPY CHANGELOG.md .
# The NZBGet / SABnzbd scripts Settings → Integrations offers for download
# (their URL and API key filled in). v0.10.0: never copied, so the download failed.
COPY nzbget-extension/ nzbget-extension/
COPY sabnzbd-script/ sabnzbd-script/
COPY --from=frontend-build /app/frontend/dist frontend/dist
# DVD subtitle OCR (stage 2); runs against the tesseract-ocr installed above.
COPY --from=subtile-ocr-build /out/ /usr/local/
# Dolby Vision (dovi-build); mkvmerge comes with mkvtoolnix above.
COPY --from=dovi-build /out/ /usr/local/

RUN mkdir -p /app/data

# Opt-in PUID / PGID / UMASK: run as that user instead of root (v0.10.0).
COPY docker/entrypoint.sh /usr/local/bin/shrinkerr-entrypoint
COPY docker/healthcheck.py /usr/local/bin/shrinkerr-healthcheck
RUN chmod +x /usr/local/bin/shrinkerr-entrypoint \
    && command -v setpriv && command -v useradd && command -v groupadd && command -v usermod

EXPOSE 6680

# Bundled TMDB non-commercial API key — passed in at build time from the
# TMDB_API_KEY GitHub Actions secret so the image ships with a working
# default. User-saved keys in Settings always win (see
# backend/metadata.py::resolve_tmdb_key). Builds without the secret set
# leave this empty and behave as before.
ARG TMDB_API_KEY=""
ENV SHRINKERR_TMDB_API_KEY=${TMDB_API_KEY}

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python3 /usr/local/bin/shrinkerr-healthcheck

# SHRINKERR_MODE=worker → run as a remote worker node (no server, no UI).
# Default → run the FastAPI server + UI.
ENTRYPOINT ["/usr/local/bin/shrinkerr-entrypoint"]
CMD ["python3", "-m", "backend.main"]
