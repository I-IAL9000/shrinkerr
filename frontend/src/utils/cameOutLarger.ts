// A file converted before that came out larger (v0.10.0): the queue's note,
// not an ignore (backend scan_filters.AUTO_IGNORE_REASONS). Add to Queue
// converts it again; the Scanner says when and at what quality.
import { qualityLabelForCq } from "./encoderLabel";

type T = (key: string, options?: Record<string, unknown>) => string;

export interface CameOutLarger {
  reason: string;
  at: string;
  details?: { cq?: number; encoder?: string; crf?: number; pct?: number; error_key?: string } | null;
}

/** A tried quality as the encoder names it ("CQ 25", "CRF 27", "q:v 40"). */
export function triedQuality(q: { cq: number; encoder?: string | null; crf?: number | null }): string {
  return qualityLabelForCq(q.encoder, q.cq, q.crf != null ? q.crf - q.cq : null);
}

/** The badge's tooltip: when, at what quality, how much larger. */
export function cameOutLargerNote(mark: CameOutLarger, t: T): string {
  const d = mark.details || {};
  const date = mark.at ? new Date(mark.at).toLocaleDateString() : "";
  const quality = d.cq != null ? triedQuality({ cq: d.cq, encoder: d.encoder, crf: d.crf }) : null;
  if (mark.reason === "video_kept") return t("library:badges.cameOutLargerKept", { date });
  if (quality && d.pct != null) return t("library:badges.cameOutLargerPct", { date, quality, pct: d.pct });
  if (quality) return t("library:badges.cameOutLargerAt", { date, quality });
  return t("library:badges.cameOutLargerOn", { date });
}
