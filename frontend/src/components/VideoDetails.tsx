import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { readVideoDetails } from "../api";
import type { ScannedFile } from "../types";
import { hdrLabel } from "../codecLabels";
import { fmtBitrate, fmtBytes, fmtDuration, fmtDate } from "../fmt";

// The codecs by their standard names (the badges elsewhere say x264 / x265).
const CODEC_NAMES: Record<string, string> = {
  h264: "H.264 (AVC)", avc: "H.264 (AVC)", hevc: "H.265 (HEVC)", h265: "H.265 (HEVC)", av1: "AV1",
  mpeg2video: "MPEG-2", mpeg4: "MPEG-4 Part 2", msmpeg4v3: "MPEG-4 (DivX)", vc1: "VC-1", wmv3: "WMV 9",
  vp9: "VP9", vp8: "VP8", mpeg1video: "MPEG-1",
};
const TIERS: Record<string, string> = { "4k": "4K", "1080p": "1080p", "720p": "720p", sd: "SD" };
const NAMED_RATIOS: [number, string][] = [[16 / 9, "16:9"], [4 / 3, "4:3"], [1.85, "1.85:1"], [2.39, "2.39:1"], [2.4, "2.40:1"]];

/** "16:9", "2.40:1": the probe's display ratio (right for anamorphic DVDs), else width / height. */
function aspectRatio(dar: string | null | undefined, width?: number, height?: number): string | null {
  let ratio = 0;
  const m = (dar || "").match(/^(\d+):(\d+)$/);
  if (m && Number(m[1]) > 0 && Number(m[2]) > 0) ratio = Number(m[1]) / Number(m[2]);
  else if (width && height) ratio = width / height;
  if (!ratio) return null;
  const named = NAMED_RATIOS.find(([r]) => Math.abs(r - ratio) < 0.004);
  return named ? named[1] : `${ratio.toFixed(2)}:1`;
}

function container(file: ScannedFile): string | null {
  if (file.disc_type === "dvd") return "DVD (VIDEO_TS)";
  if (file.disc_type === "bdmv") return file.file_path.toLowerCase().endsWith(".iso") ? "Blu-ray (ISO)" : "Blu-ray (BDMV)";
  const ext = file.file_path.split(".").pop() || "";
  return ext && ext !== file.file_path ? ext.toUpperCase() : null;
}

/**
 * A file's video details for the Scanner's file panel (v0.10.0): the picture
 * (codec, resolution, width, height, aspect ratio, frame rate), then the file
 * (container, duration, size, bitrate, bits per pixel, when it was added). "—"
 * where the scan didn't read it. A file scanned before 0.10.0 has no width
 * yet: the server reads it now ("…" meanwhile).
 */
export default function VideoDetails({ file: listed }: { file: ScannedFile }) {
  const { t } = useTranslation(["fileDetail"]);
  const unread = !listed.video_width && !listed.disc_type && (listed.probe_status ?? "ok") === "ok";
  const [read, setRead] = useState<Partial<ScannedFile> | null>(null);
  const [reading, setReading] = useState(false);
  useEffect(() => {
    if (!unread) return;
    let live = true;
    setReading(true);
    readVideoDetails(listed.file_path)
      .then(details => { if (live) setRead(details); })
      .catch(() => {})
      .finally(() => { if (live) setReading(false); });
    return () => { live = false; };
  }, [listed.file_path, unread]);
  const file: ScannedFile = read ? { ...listed, ...read } : listed;
  const codec = (file.video_codec || "").toLowerCase();
  const overall = file.duration > 0 ? (file.file_size * 8) / file.duration : 0;
  const fps = file.video_fps ? Number(file.video_fps.toFixed(3)) : 0;
  // Bits per pixel per frame (v0.10.0), as Advanced Search reads it
  // (scan_filters._BPP): the whole file's bitrate, the width or a 16:9 one,
  // 24 fps when unknown, so the two agree.
  const pixels = file.video_height ? (file.video_width || file.video_height * 16 / 9) * file.video_height : 0;
  const bpp = overall && pixels ? overall / (pixels * (file.video_fps || 24)) : 0;
  // The picture, then the file.
  const rows: [string, string | null, string?][] = [
    [t("fileDetail:details.codec"), codec ? [CODEC_NAMES[codec] || codec.toUpperCase(),
      file.video_bit_depth ? t("fileDetail:details.bitDepth", { bits: file.video_bit_depth }) : null,
      hdrLabel(file.hdr_format)].filter(Boolean).join(" · ") : null],
    [t("fileDetail:details.resolution"), file.resolution ? TIERS[file.resolution] : null],
    [t("fileDetail:details.width"), file.video_width ? `${file.video_width} px` : null],
    [t("fileDetail:details.height"), file.video_height ? `${file.video_height} px` : null],
    [t("fileDetail:details.aspectRatio"), aspectRatio(file.video_dar, file.video_width, file.video_height)],
    [t("fileDetail:details.frameRate"), fps ? [`${fps} fps`, file.video_vfr ? t("fileDetail:details.variable") : null,
      file.video_interlaced ? t("fileDetail:details.interlaced") : null].filter(Boolean).join(" · ") : null],
    [t("fileDetail:details.container"), container(file)],
    [t("fileDetail:details.duration"), file.duration > 0 ? fmtDuration(file.duration) : null],
    [t("fileDetail:details.size"), file.file_size ? fmtBytes(file.file_size) : null],
    [t("fileDetail:details.bitrate"), overall ? fmtBitrate(overall) + (file.video_bitrate
      ? ` (${t("fileDetail:details.videoBitrate", { rate: fmtBitrate(file.video_bitrate) })})` : "") : null],
    [t("fileDetail:details.bitsPerPixel"), bpp ? bpp.toFixed(3) : null, t("fileDetail:details.bitsPerPixelHelp")],
    [t("fileDetail:details.added"), file.file_mtime ? fmtDate(file.file_mtime * 1000) : null],
  ];
  return (
    <dl style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(min(150px, calc(50% - 8px)), 1fr))", gap: "4px 16px",
                 margin: "4px 0 8px", fontSize: 12 }}>
      {rows.map(([label, value, help]) => (
        <div key={label} style={{ minWidth: 0 }} title={help}>
          <dt style={{ fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: 0.4 }}>{label}</dt>
          <dd style={{ margin: 0, color: value ? "var(--text-secondary)" : "var(--text-muted)", overflowWrap: "anywhere" }}>
            {value ?? (reading ? "…" : "—")}
          </dd>
        </div>
      ))}
    </dl>
  );
}
