import type { EncodingOverrides } from "./components/EstimateModal";

/** The dialog's choices as add-from-scan's fields (Add to Queue, and the
 *  Queue's Edit settings). priority null: Auto (the Queue's edit: unchanged). */
export function queueRequestFields(priority: number | null, overrides?: EncodingOverrides): Record<string, unknown> {
  const extra: Record<string, unknown> = {};
  // A priority picked is every file's, over the rules (null: Auto)
  if (priority !== null) extra.priority_override = priority;
  if (overrides) {
    if (overrides.encoder) extra.encoder_override = overrides.encoder;
    if (overrides.nvenc_preset) extra.nvenc_preset_override = overrides.nvenc_preset;
    if (overrides.nvenc_cq != null) extra.nvenc_cq_override = overrides.nvenc_cq;
    if (overrides.libx265_preset) extra.libx265_preset_override = overrides.libx265_preset;
    if (overrides.libx265_crf != null) extra.libx265_crf_override = overrides.libx265_crf;
    if (overrides.audio_codec) extra.audio_codec_override = overrides.audio_codec;
    if (overrides.audio_bitrate != null) extra.audio_bitrate_override = overrides.audio_bitrate;
    if (overrides.target_resolution) extra.target_resolution_override = overrides.target_resolution;
    if (overrides.force_reencode) extra.force_reencode = true;
    if (overrides.cleanup_only) extra.cleanup_only = true;
    // Fix for direct play (v0.10.0): ticked or not, the dialog's choice
    if (overrides.direct_play != null) extra.direct_play = overrides.direct_play;
    if (overrides.direct_play) extra.direct_play_fixes = overrides.direct_play_fixes;
    if (overrides.vmaf_target != null) extra.vmaf_target_override = overrides.vmaf_target;
    if (overrides.lossless_audio) extra.lossless_audio_override = overrides.lossless_audio;
    if (overrides.nvenc_aq) extra.nvenc_aq_override = overrides.nvenc_aq;
  }
  return extra;
}
