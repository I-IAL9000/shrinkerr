# Hardware

What Shrinkerr can encode with, on which machines, and which image to use.

## Encoders

| Encoder | Hardware | Image | HEVC | AV1 | HDR kept | Dolby Vision kept | Status |
|---|---|---|---|---|---|---|---|
| **NVENC** | NVIDIA GPU | `:nvenc` (amd64) | ✓ | RTX 40 series or newer | ✓ | ✓ | Stable |
| **libx265 / SVT-AV1** | Any CPU | `:latest` (amd64, arm64) | ✓ | ✓ | ✓ | ✓ | Stable |
| **Intel Quick Sync** (QSV) | Intel iGPU or Arc | any, amd64 | ✓ | Arc, Core Ultra | — | — | Experimental |
| **VAAPI** | Intel iGPU, AMD GPU | any | ✓ | — | — | — | Experimental |
| **VideoToolbox** | Apple Silicon or Intel Mac | none: [native install](native-install-mac.md) | ✓ | — | ✓ | with dovi_tool installed | Native macOS only |

- **HDR kept**: HDR10 and HLG stay HDR. Quick Sync and VAAPI encode 8-bit
  here, so Shrinkerr doesn't give them HDR files.
- **Dolby Vision kept**: profiles 7 and 8, converted to HEVC at the same
  resolution ([Encoding guide](encoding-guide.md#hdr-and-dolby-vision)).
- **AV1** is chosen per rule ([Encoding guide](encoding-guide.md#av1-output)).
  The hardware AV1 encoders are tested with one frame before they're
  offered.
- Settings → Video → **Re-detect** looks for encoders again, e.g. after you
  pass a GPU through to the container. The **Doctor** page lists the
  encoders found; the **Monitor** page shows the NVIDIA driver version and
  why NVENC is off, if it is.

## NVIDIA

- **Driver 570 or newer** on the host (both `:nvenc` and `:edge-nvenc`),
  and the [NVIDIA Container Toolkit](https://github.com/NVIDIA/nvidia-container-toolkit).
- **GPUs:** 10-bit HEVC, the default, needs a Pascal GPU (GTX 10 series,
  Quadro P-series) or newer; a second-generation Maxwell GPU (GTX 950 and up)
  can encode 8-bit HEVC (Settings → Video → Bit Depth). AV1 needs an Ada
  Lovelace GPU (RTX 40 series, RTX Ada) or newer.
- Decoding runs on the GPU too (NVDEC) where it can; a file whose decode
  produces blank frames is retried on the CPU.
- In the `:nvenc` image, audio language detection (Whisper) also runs on the
  GPU.
- Windows: Docker Desktop with WSL2 and a current NVIDIA driver works the
  same way.

## Intel and AMD (experimental)

Both images include the VA-API runtime; Intel's Quick Sync driver is in the
amd64 images only. Pass `/dev/dri` through to the container
([README](../README.md#intelamd-gpu-support-experimental)).

- **Quick Sync** (`hevc_qsv`): Intel iGPUs from 6th-gen Core (Skylake) on,
  Intel Arc. AV1 on Arc and Core Ultra.
- **VAAPI** (`hevc_vaapi`): Intel iGPUs and AMD GPUs from the RX 400 series
  (Polaris) on.

These have been confirmed on few machines. If you use one, an issue saying
"works on …" or "fails with …" helps.

## Apple

- **Docker on a Mac** runs the `:latest` image (arm64 on Apple Silicon)
  with CPU encoding: Docker can't reach the Mac's video encoder.
- **Natively** ([macOS install](native-install-mac.md)), Shrinkerr uses
  VideoToolbox, the Mac's hardware encoder.

## CPU only

`:latest` runs anywhere Docker does, including arm64 boards such as the
Raspberry Pi 5. libx265 gives the smallest files for the quality but is
slow: expect a film to take hours on a small machine. Several machines can
share the work as [remote workers](remote-workers.md), each with its own
encoder.

## Which image

| Your machine | Image |
|---|---|
| NVIDIA GPU (Linux, or Windows with WSL2) | `:nvenc` |
| Intel or AMD GPU | `:latest`, with `/dev/dri` passed through |
| No GPU, or a Mac running Docker | `:latest` |
| Mac, with hardware encoding | none: [native install](native-install-mac.md) |

`:edge` and `:edge-nvenc` are the same with ffmpeg's development version
instead of its latest release. See
[Installation](installation.md) for all the tags.
