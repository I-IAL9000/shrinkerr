import { memo, useEffect, useState } from "react";
import { displayNameForPath } from "../utils/displayName";
import { useTranslation } from "react-i18next";
import type { Job } from "../types";
import { getJobLog } from "../api";
import { vmafColor, vmafLabel } from "../utils/vmaf";
import { jobErrorHeadline } from "../i18n/server";
import { copyText } from "../utils/clipboard";
import { fmtDateTime, fmtBytes, fmtDuration } from "../fmt";
import { encoderSettingsLabel, jobEncoderSettings } from "../utils/encoderLabel";
import { pressable } from "../utils/a11y";
import PendingJobPlan from "./PendingJobPlan";

interface JobListItemProps {
  job: Job;
  onCancel: (id: number) => void;
  onRetry?: (id: number) => void;
  onRemove: (id: number) => void;
  onIgnore?: (id: number, filePath: string) => void;
  onUndo?: (id: number) => void;
  checked?: boolean;
  onCheck?: (e: { shiftKey: boolean }) => void;
  encodingDefaults?: any;
  // Controlled expansion (v0.9.138). The Queue page virtualizes its lists, so
  // rows unmount when scrolled away — it owns the expanded set so a row is
  // still open when you scroll back. Omitted = local state, as before.
  expanded?: boolean;
  onToggleExpand?: (id: number) => void;
  // A pending job's tracks were edited in its "what will happen" panel.
  onPlanChanged?: () => void;
}

const iconBtnStyle: React.CSSProperties = {
  background: "none", border: "none", cursor: "pointer",
  fontSize: 18, lineHeight: 1, padding: "4px 6px", borderRadius: 4,
};

function JobListItemImpl({ job, onCancel, onRetry, onRemove, onIgnore, onUndo, checked, onCheck, encodingDefaults, expanded: expandedProp, onToggleExpand, onPlanChanged }: JobListItemProps) {
  const { t } = useTranslation(["queue", "common"]);
  const [expandedLocal, setExpandedLocal] = useState(false);
  const expanded = expandedProp ?? expandedLocal;
  const [logData, setLogData] = useState<any>(null);
  const [logLoading, setLogLoading] = useState(false);
  const [showFullLog, setShowFullLog] = useState(false);
  const fileName = displayNameForPath(job.file_path);
  const hasAudioRemoval = job.audio_tracks_to_remove && job.audio_tracks_to_remove.length > 0;
  const hasSubRemoval = job.subtitle_tracks_to_remove && job.subtitle_tracks_to_remove.length > 0;
  const typeBadge = job.job_type === "combined"
    ? `${t("queue:item.type.convert")}${hasAudioRemoval ? ` + ${t("queue:item.type.audio")}` : ""}${hasSubRemoval ? ` + ${t("queue:item.type.subs")}` : ""}${!hasAudioRemoval && !hasSubRemoval ? ` + ${t("queue:item.type.cleanup")}` : ""}`
    : job.job_type === "convert" ? t("queue:item.type.convert")
    : job.job_type === "health_check" ? `${t("queue:item.type.healthCheck")}${job.encoder ? ` (${job.encoder})` : ""}`
    // v0.9.68: handle the both-removals case explicitly — it previously fell
    // through to "Remux", mislabelling an audio+sub cleanup as a plain remux.
    : hasAudioRemoval && hasSubRemoval ? t("queue:item.type.audioSubCleanup")
    : hasSubRemoval ? t("queue:item.type.subCleanup")
    : hasAudioRemoval ? t("queue:item.type.audioCleanup")
    // v0.9.38: an audio job with no track removal is a stream-copy remux
    // (e.g. apply a detected language to an AVI) — not a "cleanup".
    : t("queue:item.type.remux");

  // A pending job opens what it will do (v0.10.0); a health check changes nothing.
  const canExpand = job.status === "failed" || job.status === "completed"
    || (job.status === "pending" && job.job_type !== "health_check");

  const handleExpand = () => {
    if (!canExpand) return;
    if (onToggleExpand) onToggleExpand(job.id);
    else setExpandedLocal(v => !v);
  };

  // Load conversion log on first expand for completed AND failed jobs.
  // Failed jobs show error_log + ffmpeg_command + ffmpeg_log so the user
  // can diagnose the failure without docker exec'ing into the container.
  // v0.4.8+. Runs on mount too, so a row that was open when it scrolled
  // out of a virtualized list reloads its log when it comes back.
  useEffect(() => {
    if (!expanded || logData || !(job.status === "completed" || job.status === "failed")) return;
    let cancelled = false;
    setLogLoading(true);
    getJobLog(job.id)
      .then(data => { if (!cancelled) setLogData(data); })
      .catch(() => { /* ignore */ })
      .finally(() => { if (!cancelled) setLogLoading(false); });
    return () => { cancelled = true; };
  }, [expanded, job.id, job.status]);

  return (
    <div>
    <div className="job-row" onClick={canExpand ? handleExpand : undefined} {...(canExpand ? pressable(handleExpand, { expanded }) : {})} style={canExpand ? { cursor: "pointer" } : undefined}>
      {job.status === "completed" && (() => {
        // Priority order (worst → best):
        //   1. health_status === "corrupt"  → red triangle
        //   2. health_status === "warnings" → amber circle
        //   3. completed but space_saved <= 0 → amber circle ("no savings, ignored")
        //   4. healthy completion with real savings → green check
        const isHealthCorrupt = job.health_status === "corrupt";
        const isHealthWarn    = job.health_status === "warnings";
        const noSavings       = !job.job_type || job.job_type !== "health_check"
                                  ? (job.space_saved ?? 0) <= 0
                                  : false;
        const showAmber = isHealthWarn || (!isHealthCorrupt && noSavings);
        const amberTitle = isHealthWarn
          ? t("queue:item.healthWarnTitle")
          : t("queue:item.noSavingsTitle");
        return (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6, width: 34, flexShrink: 0 }}>
            {isHealthCorrupt ? (
              <span
                title={jobErrorHeadline(job) ?? job.error_log ?? t("queue:item.corruptTitle")}
                style={{ color: "var(--danger, var(--danger))", fontSize: 14, display: "inline-flex", alignItems: "center" }}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
                  <line x1="12" y1="9" x2="12" y2="13"/>
                  <line x1="12" y1="17" x2="12.01" y2="17"/>
                </svg>
              </span>
            ) : showAmber ? (
              <span
                title={amberTitle}
                style={{ color: "var(--caution)", fontSize: 14, display: "inline-flex", alignItems: "center" }}
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="12" cy="12" r="10"/>
                  <line x1="12" y1="8" x2="12" y2="12"/>
                  <line x1="12" y1="16" x2="12.01" y2="16"/>
                </svg>
              </span>
            ) : (
              <span style={{ color: "var(--success)", fontSize: 14 }}>&#x2713;</span>
            )}
            <span style={{ fontSize: 10, color: "var(--text-muted)", opacity: 0.5 }}>{expanded ? "\u25BC" : "\u25B6"}</span>
          </span>
        );
      })()}
      {job.status === "failed" && <span style={{ color: "var(--danger)", width: 20, fontSize: 14 }}>{expanded ? "\u25BC" : "\u25B6"}</span>}
      {job.status === "pending" && onCheck && (
        <input
          type="checkbox"
          checked={!!checked}
          readOnly
          onClick={(e) => { e.stopPropagation(); onCheck({ shiftKey: e.shiftKey }); }}
          style={{ marginRight: 4 }}
        />
      )}
      {job.status === "pending" && (
        <span style={{ cursor: "grab", opacity: 0.3, marginLeft: 8, marginRight: 10, fontSize: 14 }}>&#x2807;</span>
      )}
      {job.status === "pending" && (
        <span style={{ fontSize: 10, color: "var(--text-muted)", opacity: canExpand ? 0.5 : 0, width: 14, flexShrink: 0 }}>{expanded ? "\u25BC" : "\u25B6"}</span>
      )}
      <span className="job-filename" style={{ flex: 1, minWidth: 0 }}>
        {fileName}
        {(job as any).original_size > 0 && (
          <span style={{ marginLeft: 8, fontSize: 11, color: "var(--text-muted)" }}>
            {job.status === "completed" && job.space_saved > 0
              ? fmtBytes((job as any).original_size - job.space_saved)
              : fmtBytes((job as any).original_size)}
          </span>
        )}
        {(job as any).priority > 0 && (
          <span style={{
            marginLeft: 8, fontSize: 9, padding: "1px 5px", borderRadius: 6, fontWeight: "bold",
            background: (job as any).priority >= 2 ? "rgba(233,69,96,0.15)" : "rgba(255,169,77,0.15)",
            color: (job as any).priority >= 2 ? "var(--danger)" : "var(--caution)",
          }}>
            {(job as any).priority >= 2 ? t("queue:item.badgeHighest") : t("queue:item.badgeHigh")}
          </span>
        )}
      </span>
      {job.status === "completed" && (
        <>
          {job.job_type === "health_check" ? (
            job.error_log && job.error_log.startsWith("Corrupt") ? (
              <span
                title={jobErrorHeadline(job) ?? job.error_log}
                style={{ fontSize: 11, color: "#ffffff", background: "var(--danger)", padding: "1px 6px", borderRadius: 3, fontWeight: 600 }}
              >{t("queue:item.corrupt")}</span>
            ) : (
              <span
                style={{ fontSize: 11, color: "#ffffff", background: "var(--success)", padding: "1px 6px", borderRadius: 3, fontWeight: 600 }}
              >{t("queue:item.healthy")}</span>
            )
          ) : (
            <>
              {job.space_saved > 0 && (
                <span style={{ color: "var(--success)", fontSize: 11 }}>{t("queue:item.saved", { size: fmtBytes(job.space_saved) })}</span>
              )}
              {job.space_saved <= 0 && job.error_log?.startsWith("VMAF ") ? (
                // VMAF-rejected encodes get a distinct amber badge so the
                // user can tell at a glance WHY this file wasn't converted
                // (vs. a generic "no savings" skip). Full reason lives in
                // the expanded view + tooltip.
                <span
                  title={jobErrorHeadline(job) ?? job.error_log}
                  style={{ fontSize: 11, color: "var(--caution)", background: "rgba(255,169,77,0.15)", padding: "1px 6px", borderRadius: 3, fontWeight: 600 }}
                >
                  {t("queue:item.vmafRejected")}
                </span>
              ) : job.space_saved <= 0 ? (
                <span style={{ fontSize: 11, color: "var(--text-muted)", background: "var(--bg-tertiary)", padding: "1px 6px", borderRadius: 3 }}>{t("queue:item.ignored")}</span>
              ) : null}
            </>
          )}
          {onUndo && (job as any).backup_path && (
            <button
              onClick={(e) => { e.stopPropagation(); onUndo(job.id); }}
              style={{ ...iconBtnStyle, color: "var(--accent-text)", marginLeft: 4, fontSize: 14 }}
              title={t("queue:item.restoreOriginal")}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>
              </svg>
            </button>
          )}
          <button aria-label={t("common:actions.remove")} onClick={(e) => { e.stopPropagation(); onRemove(job.id); }}
            style={{ ...iconBtnStyle, color: "var(--text-muted)", marginLeft: 4 }}
            title={t("common:actions.remove")}>
            &times;
          </button>
        </>
      )}
      {job.status === "failed" && (
        <>
          {onRetry && (
            <button className="btn btn-secondary" style={{ fontSize: 11, padding: "2px 8px" }}
              onClick={(e) => { e.stopPropagation(); onRetry(job.id); }}>{t("common:actions.retry")}</button>
          )}
          <button aria-label={t("common:actions.remove")} onClick={(e) => { e.stopPropagation(); onRemove(job.id); }}
            style={{ ...iconBtnStyle, color: "var(--danger)", marginLeft: 8 }}
            title={t("common:actions.remove")}>
            &times;
          </button>
        </>
      )}
      {job.status === "pending" && (
        <>
          <span className="job-type-badge" style={{ background: "var(--bg-tertiary)" }}>{typeBadge}</span>
          {/* v0.10.0: an import waiting for Bazarr's subtitles */}
          {job.not_before && new Date(job.not_before).getTime() > Date.now() && (
            <span title={t("queue:item.waitingHelp")}
              style={{ fontSize: 10, padding: "1px 5px", borderRadius: 3, background: "var(--bg-tertiary)", color: "var(--caution)", marginLeft: 4 }}>
              {t("queue:item.waitingUntil", { time: new Date(job.not_before).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) })}
            </span>
          )}
          {/* v0.9.38: encoder settings are irrelevant for an audio-only
              stream-copy remux — don't imply a re-encode that isn't happening.
              Nor for a health check (v0.10.0), which encodes nothing; as the
              running card, conversions only. */}
          {(job.job_type === "convert" || job.job_type === "combined") && (
          <span style={{ fontSize: 10, padding: "1px 5px", borderRadius: 3, background: "var(--bg-tertiary)", color: "var(--text-secondary)", marginLeft: 4 }}>
            {encoderSettingsLabel(job.encoder || encodingDefaults?.default_encoder, jobEncoderSettings(job, encodingDefaults))}
          </span>
          )}
          <div style={{ display: "inline-flex", alignItems: "center", gap: 2, marginLeft: 6 }}>
            {onIgnore && (
              <button aria-label={t("queue:item.ignoreFile")} onClick={(e) => { e.stopPropagation(); onIgnore(job.id, job.file_path); }}
                style={{ ...iconBtnStyle, color: "var(--text-muted)", padding: "2px 4px", fontSize: 16, display: "inline-flex", alignItems: "center" }}
                title={t("queue:item.ignoreFile")}>
                &#x2298;
              </button>
            )}
            <button aria-label={t("queue:item.removeFromQueue")} onClick={(e) => { e.stopPropagation(); onCancel(job.id); }}
              style={{ ...iconBtnStyle, color: "var(--text-muted)", padding: "2px 4px", fontSize: 16, display: "inline-flex", alignItems: "center" }}
              title={t("queue:item.removeFromQueue")}>
              &times;
            </button>
          </div>
        </>
      )}
    </div>

    {/* Expanded details for completed jobs — conversion log */}
    {expanded && job.status === "completed" && (
      <div style={{
        padding: "10px 12px 10px 36px", fontSize: 12, lineHeight: 1.6,
        background: "rgba(71,191,255,0.03)", borderBottom: "1px solid var(--bg-card)",
      }}>
        {logLoading ? (
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <div className="spinner" style={{ width: 14, height: 14 }} />
            <span style={{ color: "var(--text-muted)" }}>{t("queue:item.loadingDetails")}</span>
          </div>
        ) : logData ? (
          <>
            {/* Comparison table: original vs encoded.
                Audio-only jobs (job_type="audio") get a cleanup-flavoured
                variant that drops the codec/bitrate rows (no video
                re-encode) and adds track-removal counts + languages.
                v0.3.117+. */}
            {logData.encoding_stats && (
              <div style={{ display: "grid", gridTemplateColumns: "auto 1fr 1fr", gap: "4px 16px", marginBottom: 12, fontSize: 11 }}>
                <span style={{ color: "var(--text-muted)", fontWeight: 600 }}></span>
                <span style={{ color: "var(--text-muted)", fontWeight: 600, fontSize: 10, textTransform: "uppercase" }}>{t("queue:item.stats.original")}</span>
                <span style={{ color: "var(--text-muted)", fontWeight: 600, fontSize: 10, textTransform: "uppercase" }}>
                  {job.job_type === "audio" ? t("queue:item.stats.cleaned") : t("queue:item.stats.encoded")}
                </span>

                {logData.encoding_stats.input_size > 0 && <>
                  <span style={{ color: "var(--text-muted)" }}>{t("queue:item.stats.size")}</span>
                  <span style={{ color: "var(--text-secondary)" }}>{fmtBytes(logData.encoding_stats.input_size)}</span>
                  {/* Negative ratio = skipped_larger (encode grew the file
                      and the original was kept). Render in a warning
                      colour with an explicit "discarded" hint so the row
                      doesn't read like a successful saving. v0.3.55+. */}
                  {logData.encoding_stats.ratio < 0 ? (
                    <span style={{ color: "var(--caution)" }}>
                      {fmtBytes(logData.encoding_stats.output_size)}{" "}
                      <span style={{ color: "var(--text-muted)" }}>
                        {t("queue:item.stats.largerDiscarded", { pct: Math.abs(logData.encoding_stats.ratio) })}
                      </span>
                    </span>
                  ) : (
                    <span style={{ color: "var(--success)" }}>
                      {fmtBytes(logData.encoding_stats.output_size)}{" "}
                      <span style={{ color: "var(--text-muted)" }}>{t("queue:item.stats.savedPct", { pct: logData.encoding_stats.ratio })}</span>
                    </span>
                  )}
                </>}

                {logData.encoding_stats.input_bitrate != null && <>
                  <span style={{ color: "var(--text-muted)" }}>{t("queue:item.stats.bitrate")}</span>
                  <span style={{ color: "var(--text-secondary)" }}>{logData.encoding_stats.input_bitrate} Mbps</span>
                  <span style={{ color: "var(--text-secondary)" }}>{logData.encoding_stats.output_bitrate} Mbps</span>
                </>}

                {/* Codec row — video conversion path only. Audio-only
                    jobs don't change the video codec. */}
                {job.job_type !== "audio" && <>
                  <span style={{ color: "var(--text-muted)" }}>{t("queue:item.stats.codec")}</span>
                  <span style={{ color: "var(--text-secondary)" }}>x264</span>
                  {/* Match the v0.3.30 rename rule: libx265 → "x265" (the
                      specific encoder), hardware encoders → "h265" (the
                      codec spec, encoder-agnostic) tagged with which one
                      actually produced the file. Keeps the post-job report
                      consistent with the renamed output filename. v0.3.67
                      extended this to qsv / vaapi. */}
                  <span style={{ color: "var(--text-secondary)" }}>
                    {(() => {
                      const enc = (logData.encoding_stats.encoder || "").toLowerCase();
                      if (logData.encoding_stats.output_codec === "av1") {  // v0.10.0
                        return enc === "libx265" ? "AV1 (SVT-AV1)" : enc === "qsv" ? "AV1 (QSV)" : "AV1 (NVENC)";
                      }
                      if (enc === "libx265") return "x265 (CPU)";
                      if (enc === "qsv") return "h265 (QSV)";
                      if (enc === "vaapi") return "h265 (VAAPI)";
                      if (enc === "videotoolbox") return "h265 (VideoToolbox)";
                      return "h265 (NVENC)";
                    })()}
                  </span>
                </>}

                {/* v0.10.0: the quality the VMAF target search chose. */}
                {logData.encoding_stats.vmaf_target && (() => {
                  const vt = logData.encoding_stats.vmaf_target;
                  const quality = (logData.encoding_stats.encoder || "").toLowerCase() === "libx265"
                    ? `CRF ${logData.encoding_stats.crf}` : `CQ ${vt.cq}`;
                  return <>
                    <span style={{ color: "var(--text-muted)" }}>{t("queue:item.stats.vmafTarget")}</span>
                    <span style={{ color: "var(--text-secondary)", gridColumn: "2 / span 2" }}>
                      {t(vt.reached ? "queue:item.stats.vmafTargetFound" : "queue:item.stats.vmafTargetMissed",
                        { quality, vmaf: vt.vmaf, target: vt.target })}
                    </span>
                  </>;
                })()}

                {/* Audio-conversion row — shown when one or more source
                    tracks were re-encoded to a new codec/bitrate
                    (lossless→lossy auto-conversion or global audio_codec
                    setting). v0.4.7+. Renders e.g.
                    "Audio: DTS-HD MA → EAC3 640kb". */}
                {Array.isArray(logData.encoding_stats.audio_converted_from)
                  && logData.encoding_stats.audio_converted_from.length > 0
                  && (() => {
                    const sources = logData.encoding_stats.audio_converted_from as string[];
                    // Target codec + bitrate: prefer the lossless-conversion-specific
                    // fields (set when auto_convert_lossless triggered), fall back to
                    // the global audio_codec/bitrate (set when audio_codec != "copy").
                    const targetCodec = (logData.encoding_stats.lossless_target_codec
                      || logData.encoding_stats.audio_codec || "").toUpperCase();
                    const targetBitrate = logData.encoding_stats.lossless_target_bitrate
                      ?? logData.encoding_stats.audio_bitrate;
                    const targetLabel = targetBitrate
                      ? `${targetCodec} ${targetBitrate}kb`
                      : targetCodec;
                    return (
                      <>
                        <span style={{ color: "var(--text-muted)" }}>{t("queue:item.stats.audio")}</span>
                        <span style={{ color: "var(--text-secondary)", gridColumn: "2 / span 2" }}>
                          {sources.join(" + ")} <span style={{ color: "var(--text-muted)" }}>→</span> {targetLabel}
                        </span>
                      </>
                    );
                  })()}

                {/* Track-removal rows — shown whenever the worker
                    recorded removal counts on the stats payload, which
                    happens for both audio-only cleanup jobs (v0.3.117+)
                    and combined video+cleanup jobs (v0.3.123+). The
                    presence-of-field check makes this future-proof: any
                    job that did track removal can surface it here. */}
                {(logData.encoding_stats.audio_tracks_removed > 0) && <>
                  <span style={{ color: "var(--text-muted)" }}>{t("queue:item.stats.audioRemoved")}</span>
                  <span style={{ color: "var(--text-muted)", gridColumn: "2 / span 2" }}>
                    {t("queue:item.stats.tracks", { count: logData.encoding_stats.audio_tracks_removed })}
                    {Array.isArray(logData.encoding_stats.removed_audio_languages) && logData.encoding_stats.removed_audio_languages.length > 0 && (
                      <span style={{ color: "var(--text-muted)" }}> ({logData.encoding_stats.removed_audio_languages.join(", ")})</span>
                    )}
                  </span>
                </>}
                {(logData.encoding_stats.subtitle_tracks_removed > 0) && <>
                  <span style={{ color: "var(--text-muted)" }}>{t("queue:item.stats.subsRemoved")}</span>
                  <span style={{ color: "var(--text-muted)", gridColumn: "2 / span 2" }}>
                    {t("queue:item.stats.tracks", { count: logData.encoding_stats.subtitle_tracks_removed })}
                    {Array.isArray(logData.encoding_stats.removed_subtitle_languages) && logData.encoding_stats.removed_subtitle_languages.length > 0 && (
                      <span style={{ color: "var(--text-muted)" }}> ({logData.encoding_stats.removed_subtitle_languages.join(", ")})</span>
                    )}
                  </span>
                </>}

                {Array.isArray(logData.encoding_stats.applied_audio_languages) && logData.encoding_stats.applied_audio_languages.length > 0 && <>
                  <span style={{ color: "var(--text-muted)" }}>{t("queue:item.stats.languageApplied")}</span>
                  <span style={{ color: "var(--accent-text)", gridColumn: "2 / span 2" }}>
                    {t("queue:item.stats.audioArrow", { langs: logData.encoding_stats.applied_audio_languages.join(", ") })}
                  </span>
                </>}

                {logData.vmaf_score != null && <>
                  <span style={{ color: "var(--text-muted)" }}>VMAF</span>
                  <span style={{ color: "var(--text-muted)" }}>{t("queue:item.stats.vmafRef")}</span>
                  <span style={{
                    color: vmafColor(logData.vmaf_score),
                    fontWeight: 600,
                  }}>
                    {logData.vmaf_score} ({vmafLabel(logData.vmaf_score)})
                    {/* Uncertain marker — when libvmaf desynced on every
                        analysis window, the score is logged but flagged so
                        a "Poor" tier on a visually-fine encode is
                        recognisable as a measurement artefact, not a real
                        quality issue. v0.3.32+.

                        Coerce to boolean: vmaf_uncertain comes from a
                        SQLite INTEGER column, so the wire value is 0 or 1
                        (despite the TS type saying boolean). `0 && (...)`
                        evaluates to `0`, and React renders numeric zero
                        as the literal text "0" — which produced
                        "96.9 (Excellent)0" trailing the score. v0.3.54. */}
                    {!!job.vmaf_uncertain && (
                      <span
                        title={t("queue:item.stats.vmafUncertain")}
                        style={{ marginLeft: 4, color: "var(--warning)", cursor: "help" }}
                      >&#9888;</span>
                    )}
                  </span>
                </>}
              </div>
            )}

            {/* VMAF rejection banner — only shows when a completed job was
                rejected for failing the VMAF threshold. Makes the reason
                impossible to miss in the expanded details. */}
            {job.error_log?.startsWith("VMAF ") && (
              <div style={{
                marginBottom: 10, padding: "8px 10px", borderRadius: 4,
                background: "rgba(255,169,77,0.08)",
                border: "1px solid rgba(255,169,77,0.35)",
                display: "flex", alignItems: "center", gap: 8,
              }}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--caution)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
                  <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
                  <line x1="12" y1="9" x2="12" y2="13"/>
                  <line x1="12" y1="17" x2="12.01" y2="17"/>
                </svg>
                <div>
                  <div style={{ fontSize: 12, fontWeight: 600, color: "var(--caution)" }}>{t("queue:item.vmafBanner.title")}</div>
                  <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 2 }}>
                    {jobErrorHeadline(job) ?? job.error_log} {t("queue:item.vmafBanner.body")}
                  </div>
                </div>
              </div>
            )}

            {/* Encoding settings + timing */}
            <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 14px", marginBottom: 10, fontSize: 11 }}>
              {logData.encoding_stats?.encoder && (
                <span style={{ color: "var(--text-muted)" }}>{t("queue:item.details.encoder")} <strong style={{ color: "var(--text-secondary)" }}>{logData.encoding_stats.encoder}</strong></span>
              )}
              {logData.encoding_stats && "videotoolbox_quality" in logData.encoding_stats ? (
                // v0.9.141+: stats carry every encoder's own settings.
                <span style={{ color: "var(--text-muted)" }}>{t("queue:item.details.settings")} <strong style={{ color: "var(--text-secondary)" }}>{encoderSettingsLabel(logData.encoding_stats.encoder, {
                  ...logData.encoding_stats,
                  nvenc_preset: logData.encoding_stats.preset,
                  nvenc_cq: logData.encoding_stats.cq,
                  libx265_crf: logData.encoding_stats.crf,
                })}</strong></span>
              ) : (<>
                {/* Older jobs recorded only NVENC's preset/CQ (+ CRF): show
                    them only for NVENC runs, where they're the real values. */}
                {logData.encoding_stats?.preset && (logData.encoding_stats.encoder || "nvenc") === "nvenc" && (
                  <span style={{ color: "var(--text-muted)" }}>{t("queue:item.details.preset")} <strong style={{ color: "var(--text-secondary)" }}>{logData.encoding_stats.preset.toUpperCase()}</strong></span>
                )}
                {logData.encoding_stats?.cq != null && (logData.encoding_stats.encoder || "nvenc") === "nvenc" && (
                  <span style={{ color: "var(--text-muted)" }}>CQ: <strong style={{ color: "var(--text-secondary)" }}>{logData.encoding_stats.cq}</strong></span>
                )}
                {logData.encoding_stats?.crf != null && logData.encoding_stats?.encoder === "libx265" && (
                  <span style={{ color: "var(--text-muted)" }}>CRF: <strong style={{ color: "var(--text-secondary)" }}>{logData.encoding_stats.crf}</strong></span>
                )}
              </>)}
              {logData.encoding_stats?.encode_seconds > 0 && (
                <span style={{ color: "var(--text-muted)" }}>{t("queue:item.details.encodeTime")} <strong style={{ color: "var(--text-secondary)" }}>{fmtDuration(logData.encoding_stats.encode_seconds)}</strong></span>
              )}
              <span style={{ color: "var(--text-muted)" }}>{t("queue:item.details.type")} {t(`queue:item.jobTypes.${job.job_type}`, { defaultValue: job.job_type })}</span>
              {logData.started_at && logData.completed_at && (
                <span style={{ color: "var(--text-muted)" }}>{t("queue:item.details.total")} {fmtDuration((new Date(logData.completed_at).getTime() - new Date(logData.started_at).getTime()) / 1000)}</span>
              )}
              {logData.started_at && <span style={{ color: "var(--text-muted)" }}>{t("queue:item.details.started")} {fmtDateTime(logData.started_at)}</span>}
            </div>

            {/* Health check result (inline post-conversion OR standalone health_check job) */}
            {job.health_status && (
              <div style={{ marginBottom: 10, padding: "8px 10px", borderRadius: 4, background: "var(--bg-primary)", border: "1px solid var(--border)" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: job.health_status === "corrupt" && job.health_errors_json ? 6 : 0 }}>
                  <span style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: 0.5 }}>{t("queue:item.health.label")}</span>
                  <span style={{
                    fontSize: 11, fontWeight: 600, padding: "1px 6px", borderRadius: 3, color: "#ffffff",
                    background: job.health_status === "corrupt" ? "var(--danger)" : "var(--success)",
                  }}>
                    {job.health_status === "corrupt" ? t("queue:item.corrupt") : t("queue:item.healthy")}
                  </span>
                  {job.health_check_type && (
                    <span style={{ fontSize: 11, color: "var(--text-muted)" }}>({job.health_check_type})</span>
                  )}
                  {job.health_check_seconds != null && (
                    <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{job.health_check_seconds.toFixed(1)}s</span>
                  )}
                </div>
                {job.health_status === "corrupt" && job.health_errors_json && (() => {
                  let errs: string[] = [];
                  try { errs = JSON.parse(job.health_errors_json); } catch { /* ignore */ }
                  if (!errs.length) return null;
                  return (
                    <ul style={{ margin: 0, padding: "4px 0 0 18px", color: "var(--danger)", fontSize: 11, fontFamily: "var(--font-mono)", lineHeight: 1.5 }}>
                      {errs.slice(0, 8).map((e, i) => <li key={i} style={{ wordBreak: "break-word" }}>{e}</li>)}
                      {errs.length > 8 && <li style={{ color: "var(--text-muted)" }}>{t("queue:item.health.more", { count: errs.length - 8 })}</li>}
                    </ul>
                  );
                })()}
              </div>
            )}

            {/* ffmpeg command (copyable) */}
            {logData.ffmpeg_command && (
              <div style={{ marginBottom: 8 }}>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4, display: "flex", alignItems: "center", gap: 6 }}>
                  <span>{t("queue:item.log.ffmpegCommand")}</span>
                  <button
                    onClick={(e) => { e.stopPropagation(); copyText(logData.ffmpeg_command); }}
                    style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 2, display: "inline-flex", opacity: 0.6 }}
                    title={t("queue:item.log.copyCommand")}
                  >
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/>
                    </svg>
                  </button>
                </div>
                <div style={{
                  fontFamily: "var(--font-mono)", fontSize: 10, color: "var(--text-secondary)",
                  background: "var(--bg-primary)", padding: 8, borderRadius: 4,
                  whiteSpace: "pre-wrap", wordBreak: "break-all", maxHeight: 80, overflowY: "auto",
                }}>
                  {logData.ffmpeg_command}
                </div>
              </div>
            )}

            {/* ffmpeg log (collapsible) */}
            {logData.ffmpeg_log && (
              <div>
                <button
                  onClick={(e) => { e.stopPropagation(); setShowFullLog(!showFullLog); }}
                  style={{ background: "none", border: "none", color: "var(--accent-text)", fontSize: 11, cursor: "pointer", padding: 0 }}
                >
                  {t(showFullLog ? "queue:item.log.hideOutput" : "queue:item.log.showOutput", { count: logData.ffmpeg_log.split("\n").length })}
                </button>
                {showFullLog && (
                  <div style={{
                    fontFamily: "var(--font-mono)", fontSize: 10, color: "var(--text-muted)",
                    background: "var(--bg-primary)", padding: 8, borderRadius: 4, marginTop: 4,
                    whiteSpace: "pre-wrap", wordBreak: "break-all", maxHeight: 200, overflowY: "auto",
                  }}>
                    {logData.ffmpeg_log}
                  </div>
                )}
              </div>
            )}

            {/* Full file path */}
            <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 6, wordBreak: "break-all" }}>
              {job.file_path}
            </div>
          </>
        ) : (
          <div style={{ color: "var(--text-muted)" }}>{t("queue:item.noDetails")}</div>
        )}
      </div>
    )}

    {expanded && canExpand && job.status === "pending" && (
      <PendingJobPlan job={job} encodingDefaults={encodingDefaults} onChanged={onPlanChanged} />
    )}

    {/* Expanded error details for failed jobs */}
    {expanded && job.status === "failed" && (
      <div style={{
        padding: "8px 12px 8px 36px", fontSize: 12, lineHeight: 1.6,
        background: "rgba(233,69,96,0.05)", borderBottom: "1px solid var(--bg-card)",
      }}>
        {/* Primary error message — sticky-captured by the converter so
            the actual error survives even when an MKV's stream metadata
            would otherwise push it out of the rolling buffer. v0.4.8+. */}
        {job.error_log || job.error_key ? (() => {
          // v0.9.132: translated headline for Shrinkerr-authored errors; the
          // rest of error_log (ffmpeg stderr etc.) stays as verbatim detail.
          // The English headline is error_log's first line, so drop it when
          // a translated one is shown to avoid saying it twice.
          const headline = jobErrorHeadline(job);
          const raw = job.error_log ?? "";
          const detail = headline ? raw.split("\n").slice(1).join("\n").trim() : raw;
          return (
            <>
              {headline && (
                <div style={{ fontSize: 12, color: "var(--danger)", fontWeight: 600, marginBottom: detail ? 4 : 0 }}>{headline}</div>
              )}
              {detail && (
                <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "var(--danger)", whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
                  {detail}
                </div>
              )}
            </>
          );
        })() : (
          <div style={{ color: "var(--text-muted)" }}>{t("queue:item.noErrorDetails")}</div>
        )}
        <div style={{ display: "flex", gap: 12, marginTop: 6, fontSize: 11, color: "var(--text-muted)" }}>
          <span>{t("queue:item.details.type")} {t(`queue:item.jobTypes.${job.job_type}`, { defaultValue: job.job_type })}</span>
          {job.encoder && <span>{t("queue:item.details.encoder")} {job.encoder}</span>}
          {(job as any).original_size > 0 && <span>{t("queue:item.details.size")} {fmtBytes((job as any).original_size)}</span>}
          {job.started_at && <span>{t("queue:item.details.started")} {fmtDateTime(job.started_at)}</span>}
          {job.completed_at && <span>{t("queue:item.details.failed")} {fmtDateTime(job.completed_at)}</span>}
        </div>
        <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4, wordBreak: "break-all" }}>
          {job.file_path}
        </div>

        {/* ffmpeg command + full stderr log, fetched lazily on expand.
            v0.4.8+ — pre-fix the only failure detail surfaced was the
            short error_log, with no way to see what command was run or
            inspect the broader ffmpeg output. Required for diagnosing
            anything more complex than "file not found". */}
        {logLoading && (
          <div style={{ marginTop: 10, fontSize: 11, color: "var(--text-muted)" }}>{t("queue:item.loadingLog")}</div>
        )}
        {logData && (logData.ffmpeg_command || logData.ffmpeg_log) && (
          <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 8 }}>
            {logData.ffmpeg_command && (
              <details style={{ background: "var(--bg-card)", borderRadius: 4, padding: "6px 10px", fontSize: 11 }}>
                <summary style={{ cursor: "pointer", color: "var(--text-secondary)", userSelect: "none" }}>{t("queue:item.log.ffmpegCommand")}</summary>
                <pre style={{
                  marginTop: 6, marginBottom: 0, fontSize: 10, lineHeight: 1.45,
                  fontFamily: "var(--font-mono)", whiteSpace: "pre-wrap", wordBreak: "break-all",
                  color: "var(--text-secondary)",
                }}>{logData.ffmpeg_command}</pre>
              </details>
            )}
            {logData.ffmpeg_log && (
              <details style={{ background: "var(--bg-card)", borderRadius: 4, padding: "6px 10px", fontSize: 11 }}>
                <summary style={{ cursor: "pointer", color: "var(--text-secondary)", userSelect: "none" }}>
                  {t("queue:item.log.ffmpegLog")} <span style={{ color: "var(--text-muted)" }}>{t("queue:item.log.lastLines", { count: logData.ffmpeg_log.split("\n").length })}</span>
                </summary>
                <pre style={{
                  marginTop: 6, marginBottom: 0, fontSize: 10, lineHeight: 1.45,
                  fontFamily: "var(--font-mono)", whiteSpace: "pre-wrap", wordBreak: "break-all",
                  color: "var(--text-muted)", maxHeight: 400, overflowY: "auto",
                }}>{logData.ffmpeg_log}</pre>
              </details>
            )}
          </div>
        )}
      </div>
    )}
    </div>
  );
}

// Memoize: during active encoding, the parent QueuePage re-renders on every
// job_progress WebSocket message. Without memo, every row in the pending /
// completed / failed lists re-renders too, which is expensive at scale.
// A shallow compare on `job` + the callbacks is enough — Job objects are
// treated as immutable and only replaced when the backend reports changes.
const JobListItem = memo(JobListItemImpl, (prev, next) => {
  // Fast path: same reference = no change.
  if (prev.job !== next.job && !shallowJobEqual(prev.job, next.job)) return false;
  if (prev.checked !== next.checked) return false;
  if (prev.encodingDefaults !== next.encodingDefaults) return false;
  if (prev.expanded !== next.expanded) return false;
  // We intentionally ignore callback identity — parent recreates them on every
  // render but their behavior is stable. If a callback changes semantics the
  // parent also re-renders; stale closures aren't a concern here because the
  // callbacks just call `load()` / `toast()` / setters that take their own
  // fresh state.
  return true;
});

function shallowJobEqual(a: Job, b: Job): boolean {
  // Compare the fields that actually drive rendering. Avoids deep equality
  // while still catching real changes (status transitions, progress, etc.).
  if (
    a.id !== b.id ||
    a.status !== b.status ||
    a.file_path !== b.file_path ||
    a.space_saved !== b.space_saved ||
    a.error_log !== b.error_log ||
    (a as any).priority !== (b as any).priority ||
    (a as any).original_size !== (b as any).original_size ||
    a.job_type !== b.job_type ||
    a.encoder !== b.encoder ||
    a.nvenc_preset !== b.nvenc_preset ||
    a.nvenc_cq !== b.nvenc_cq ||
    a.libx265_preset !== b.libx265_preset ||
    a.libx265_crf !== b.libx265_crf ||
    a.health_status !== b.health_status ||
    (a as any).backup_path !== (b as any).backup_path ||
    // Fields that drive rendering in the expanded/collapsed row body:
    // the typeBadge string depends on the track-removal lengths, and the
    // health-check UI depends on the health_* fields. Timestamps show up
    // in the expanded "details" section for completed / failed rows.
    (a as any).health_check_type !== (b as any).health_check_type ||
    (a as any).health_check_seconds !== (b as any).health_check_seconds ||
    (a as any).health_errors_json !== (b as any).health_errors_json ||
    (a as any).started_at !== (b as any).started_at ||
    (a as any).completed_at !== (b as any).completed_at
  ) return false;
  // Compare track-removal arrays by length + elements. QueuePage.parseJobs
  // reallocates these arrays on every 10s poll (via JSON.parse), so a
  // reference check would force a re-render even when nothing changed.
  if (!sameNumArray(a.audio_tracks_to_remove, b.audio_tracks_to_remove)) return false;
  if (!sameNumArray(a.subtitle_tracks_to_remove, b.subtitle_tracks_to_remove)) return false;
  return true;
}

function sameNumArray(a: number[] | undefined, b: number[] | undefined): boolean {
  if (a === b) return true;
  const al = a?.length ?? 0;
  const bl = b?.length ?? 0;
  if (al !== bl) return false;
  if (!a || !b) return al === 0;
  for (let i = 0; i < al; i++) {
    if (a[i] !== b[i]) return false;
  }
  return true;
}

export default JobListItem;
