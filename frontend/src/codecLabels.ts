export const CODEC_LABELS: Record<string, string> = {
  h264: "x264", x264: "x264", avc: "x264", avc1: "x264",
  hevc: "x265", h265: "x265", x265: "x265",
  av1: "AV1", av01: "AV1",
  mpeg2video: "MPEG-2", mpeg2: "MPEG-2",
  mpeg4: "XviD", msmpeg4v3: "DivX", msmpeg4v2: "DivX",
  vc1: "VC-1", wmv3: "WMV",
  vp9: "VP9", vp8: "VP8",
  svq3: "SVQ3",
};

export function getCodecLabel(videoCodec: string, needsConversion: boolean): string {
  const codec = (videoCodec || "").toLowerCase();
  return CODEC_LABELS[codec] || codec.toUpperCase() || (needsConversion ? "x264" : "x265");
}
