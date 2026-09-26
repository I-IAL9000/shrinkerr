import i18n from ".";

// Server message codes (v0.9.132). The backend stores/sends a stable key plus
// params next to its English text. When a key is present we translate it;
// otherwise (rows written before message codes existed, raw tool output, or a
// key this build doesn't know) we show the server's English unchanged.

type Params = Record<string, unknown> | null | undefined;

export function serverText(ns: string, key: string | null | undefined, params: Params, fallback: string | null | undefined): string {
  const english = fallback ?? "";
  if (!key) return english;
  const out = String(i18n.t(`${ns}:${key}`, { ...(params ?? {}), defaultValue: english }));
  return out || english;
}

/** Activity / file-history summary line. */
export const eventSummary = (ev: { summary?: string | null; summary_key?: string | null; summary_params?: Params }) =>
  serverText("serverEvents", ev.summary_key, ev.summary_params, ev.summary);

/** Live job progress step label ("converting", "removing tracks", …). */
export const jobStep = (p: { step?: string | null; step_key?: string | null; step_params?: Params }) =>
  serverText("serverJobs", p.step_key, p.step_params, p.step);

/**
 * Translated headline for a Shrinkerr-authored job error, or null when the
 * error has no key (raw tool output). The full `error_log` (ffmpeg stderr etc.)
 * stays the verbatim technical detail.
 */
export function jobErrorHeadline(job: { error_key?: string | null; error_params?: Params }): string | null {
  if (!job.error_key) return null;
  const out = String(i18n.t(`serverJobs:${job.error_key}`, { ...(job.error_params ?? {}), defaultValue: "" }));
  return out || null;
}

/** Per-track "why detection left it und" note. */
export const detectNote = (tr: { detect_note?: string | null; detect_note_key?: string | null; detect_note_params?: Params }) =>
  serverText("serverJobs", tr.detect_note_key ? `detectNotes.${tr.detect_note_key}` : null, tr.detect_note_params, tr.detect_note);
