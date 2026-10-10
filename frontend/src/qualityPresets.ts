import type { QualityPreset } from "./api";

// The quality presets (v0.10.0) come from the server (/settings/quality-presets,
// backend/encoding_estimates.py): one list for the setup wizard, the queue
// panel and the Settings guide. Names: common:qualityPresets.<id>.

const UNIT: Record<string, string> = {
  nvenc_cq: "CQ", libx265_crf: "CRF", qsv_cq: "ICQ", vaapi_qp: "QP", videotoolbox_quality: "q:v",
};

/** "CQ 23" — the encoder setting a preset sets. */
export function presetSetting(p: QualityPreset): string {
  const [key, value] = Object.entries(p.settings)[0] || [];
  return key ? `${UNIT[key] || key} ${value}` : "";
}
