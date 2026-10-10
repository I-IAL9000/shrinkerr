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

// VideoToolbox -q:v measured against NVENC's CQ — backend/encoding_estimates.py
// _VT_Q_AS_CQ (a backend test keeps the two in step).
const VT_Q_AS_CQ: [number, number][] = [[40, 29], [45, 28], [50, 26], [55, 23], [60, 21], [62, 18], [65, 15]];

/** The -q:v nearest to `cq` (the better quality on a tie), as the server picks it. */
function vtQualityForCq(cq: number): number {
  let best = VT_Q_AS_CQ[0];
  for (const p of VT_Q_AS_CQ) {
    const d = Math.abs(p[1] - cq), bestD = Math.abs(best[1] - cq);
    if (d < bestD || (d === bestD && p[0] > best[0])) best = p;
  }
  return best[0];
}

/** A job's own per-job overrides, falling back to the global defaults.
 *  The job's quality (rules / bulk edits) is on NVENC's CQ scale and reaches
 *  every encoder (v0.10.0): QSV and VAAPI share the scale, VideoToolbox maps
 *  it, libx265 has its own CRF. Only presets stay global for QSV. */
export function jobEncoderSettings(job: EncoderSettings, defaults: EncoderSettings | null | undefined): EncoderSettings {
  const d = defaults || {};
  const cq = job.nvenc_cq;
  return {
    nvenc_preset: job.nvenc_preset || d.nvenc_preset,
    nvenc_cq: cq ?? d.nvenc_cq,
    libx265_preset: job.libx265_preset || d.libx265_preset,
    libx265_crf: job.libx265_crf ?? d.libx265_crf,
    qsv_preset: d.qsv_preset,
    qsv_cq: cq ?? d.qsv_cq,
    vaapi_qp: cq ?? d.vaapi_qp,
    videotoolbox_quality: cq != null ? vtQualityForCq(cq) : d.videotoolbox_quality,
  };
}
