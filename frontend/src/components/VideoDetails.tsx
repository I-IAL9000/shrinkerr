import { useTranslation } from "react-i18next";
import type { ScannedFile } from "../types";
import { hdrLabel } from "../codecLabels";
import { fmtBitrate, fmtBytes, fmtDuration } from "../fmt";

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
 * A file's video details for the Scanner's file panel (v0.10.0): duration,
 * codec, size, bitrate, width, height, aspect ratio, resolution, frame rate
 * and container. "—" where the scan didn't read it.
 */
export default function VideoDetails({ file }: { file: ScannedFile }) {
  const { t } = useTranslation(["fileDetail"]);
  const codec = (file.video_codec || "").toLowerCase();
  const overall = file.duration > 0 ? (file.file_size * 8) / file.duration : 0;
  const fps = file.video_fps ? Number(file.video_fps.toFixed(3)) : 0;
  const rows: [string, string | null][] = [
    [t("fileDetail:details.duration"), file.duration > 0 ? fmtDuration(file.duration) : null],
    [t("fileDetail:details.codec"), codec ? [CODEC_NAMES[codec] || codec.toUpperCase(),
      file.video_bit_depth ? t("fileDetail:details.bitDepth", { bits: file.video_bit_depth }) : null,
      hdrLabel(file.hdr_format)].filter(Boolean).join(" · ") : null],
    [t("fileDetail:details.size"), file.file_size ? fmtBytes(file.file_size) : null],
    [t("fileDetail:details.bitrate"), overall ? fmtBitrate(overall) + (file.video_bitrate
      ? ` (${t("fileDetail:details.videoBitrate", { rate: fmtBitrate(file.video_bitrate) })})` : "") : null],
    [t("fileDetail:details.width"), file.video_width ? `${file.video_width} px` : null],
    [t("fileDetail:details.height"), file.video_height ? `${file.video_height} px` : null],
    [t("fileDetail:details.aspectRatio"), aspectRatio(file.video_dar, file.video_width, file.video_height)],
    [t("fileDetail:details.resolution"), file.resolution ? TIERS[file.resolution] : null],
    [t("fileDetail:details.frameRate"), fps ? [`${fps} fps`, file.video_vfr ? t("fileDetail:details.variable") : null,
      file.video_interlaced ? t("fileDetail:details.interlaced") : null].filter(Boolean).join(" · ") : null],
    [t("fileDetail:details.container"), container(file)],
  ];
  return (
    <dl style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))", gap: "4px 16px",
                 margin: "4px 0 8px", fontSize: 12 }}>
      {rows.map(([label, value]) => (
        <div key={label} style={{ minWidth: 0 }}>
          <dt style={{ fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: 0.4 }}>{label}</dt>
          <dd style={{ margin: 0, color: value ? "var(--text-secondary)" : "var(--text-muted)", overflowWrap: "anywhere" }}>
            {value ?? "—"}
          </dd>
        </div>
      ))}
    </dl>
  );
}
