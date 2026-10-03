// One label for "which encoder settings does this job use" (v0.9.141).
// The running card, the pending row badge and the completed job details all
// used to show NVENC's "P6 / CQ 20" for every non-libx265 job — including
// QSV, VAAPI and VideoToolbox, whose settings look nothing like that.

export interface EncoderSettings {
  nvenc_preset?: string | null;
  nvenc_cq?: number | null;
  libx265_preset?: string | null;
  libx265_crf?: number | null;
  qsv_preset?: string | null;
  qsv_cq?: number | null;
  vaapi_qp?: number | null;
  videotoolbox_quality?: number | null;
}

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** e.g. "P6 / CQ 20", "Medium / CRF 20", "Medium / Q 22", "QP 22", "q:v 55". */
export function encoderSettingsLabel(encoder: string | null | undefined, s: EncoderSettings): string {
  switch ((encoder || "nvenc").toLowerCase()) {
    case "libx265":
      return `${cap(s.libx265_preset || "medium")} / CRF ${s.libx265_crf ?? 20}`;
    case "qsv":
      return `${cap(s.qsv_preset || "medium")} / Q ${s.qsv_cq ?? 22}`;
    case "vaapi":
      return `QP ${s.vaapi_qp ?? 22}`;
    case "videotoolbox":
      return `q:v ${s.videotoolbox_quality ?? 55}`;
    default:
      return `${(s.nvenc_preset || "p6").toUpperCase()} / CQ ${s.nvenc_cq ?? 20}`;
  }
}

/** A job's own per-job overrides, falling back to the global defaults.
 *  Only NVENC and libx265 have per-job overrides (rules / bulk edits);
 *  QSV, VAAPI and VideoToolbox always use the global settings. */
export function jobEncoderSettings(job: EncoderSettings, defaults: EncoderSettings | null | undefined): EncoderSettings {
  const d = defaults || {};
  return {
    nvenc_preset: job.nvenc_preset || d.nvenc_preset,
    nvenc_cq: job.nvenc_cq ?? d.nvenc_cq,
    libx265_preset: job.libx265_preset || d.libx265_preset,
    libx265_crf: job.libx265_crf ?? d.libx265_crf,
    qsv_preset: d.qsv_preset,
    qsv_cq: d.qsv_cq,
    vaapi_qp: d.vaapi_qp,
    videotoolbox_quality: d.videotoolbox_quality,
  };
}
