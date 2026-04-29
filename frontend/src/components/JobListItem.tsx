import { memo, useState } from "react";
import type { Job } from "../types";
import { getJobLog } from "../api";
import { vmafColor, vmafLabel } from "../utils/vmaf";

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
}

function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 4) return `${(bytes / (1024 ** 4)).toFixed(2)} TB`;
  const gb = bytes / (1024 ** 3);
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${(bytes / (1024 ** 2)).toFixed(0)} MB`;
}

function formatDuration(start: string, end: string): string {
  const s = (new Date(end).getTime() - new Date(start).getTime()) / 1000;
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m ${Math.round(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
}

const iconBtnStyle: React.CSSProperties = {
  background: "none", border: "none", cursor: "pointer",
  fontSize: 18, lineHeight: 1, padding: "4px 6px", borderRadius: 4,
};

function JobListItemImpl({ job, onCancel, onRetry, onRemove, onIgnore, onUndo, checked, onCheck, encodingDefaults }: JobListItemProps) {
  const [expanded, setExpanded] = useState(false);
  const [logData, setLogData] = useState<any>(null);
  const [logLoading, setLogLoading] = useState(false);
  const [showFullLog, setShowFullLog] = useState(false);
  const fileName = job.file_path.split("/").pop() || job.file_path;
  const hasAudioRemoval = job.audio_tracks_to_remove && job.audio_tracks_to_remove.length > 0;
  const hasSubRemoval = job.subtitle_tracks_to_remove && job.subtitle_tracks_to_remove.length > 0;
  const typeBadge = job.job_type === "combined"
    ? `Convert${hasAudioRemoval ? " + Audio" : ""}${hasSubRemoval ? " + Subs" : ""}${!hasAudioRemoval && !hasSubRemoval ? " + Cleanup" : ""}`
    : job.job_type === "convert" ? "Convert"
    : job.job_type === "health_check" ? `Health check${job.encoder ? ` (${job.encoder})` : ""}`
    : hasSubRemoval && !hasAudioRemoval ? "Sub cleanup"
    : hasAudioRemoval && !hasSubRemoval ? "Audio cleanup"
    : "Cleanup";

  const canExpand = job.status === "failed" || job.status === "completed";

  const handleExpand = async () => {
    if (!canExpand) return;
    const next = !expanded;
    setExpanded(next);
    // Load conversion log on first expand for completed jobs
    if (next && job.status === "completed" && !logData) {
      setLogLoading(true);
      try {
        const data = await getJobLog(job.id);
        setLogData(data);
      } catch { /* ignore */ }
      setLogLoading(false);
    }
  };

  return (
    <div>
    <div className="job-row" onClick={canExpand ? handleExpand : undefined} style={canExpand ? { cursor: "pointer" } : undefined}>
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
          ? "Health check surfaced warnings (file is playable)"
          : "No space savings — file was auto-ignored after conversion";
        return (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6, width: 34, flexShrink: 0 }}>
            {isHealthCorrupt ? (
              <span
                title={job.error_log || "Health check flagged this file as corrupt"}
                style={{ color: "var(--danger, #e94560)", fontSize: 14, display: "inline-flex", alignItems: "center" }}
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
                style={{ color: "#ffa94d", fontSize: 14, display: "inline-flex", alignItems: "center" }}
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
      {job.status === "failed" && <span style={{ color: "#e94560", width: 20, fontSize: 14 }}>{expanded ? "\u25BC" : "\u25B6"}</span>}
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
      <span className="job-filename" style={{ flex: 1, minWidth: 0 }}>
        {fileName}
        {(job as any).original_size > 0 && (
          <span style={{ marginLeft: 8, fontSize: 11, opacity: 0.4 }}>
            {job.status === "completed" && job.space_saved > 0
              ? formatBytes((job as any).original_size - job.space_saved)
              : formatBytes((job as any).original_size)}
          </span>
        )}
        {(job as any).priority > 0 && (
          <span style={{
            marginLeft: 8, fontSize: 9, padding: "1px 5px", borderRadius: 6, fontWeight: "bold",
            background: (job as any).priority >= 2 ? "rgba(233,69,96,0.15)" : "rgba(255,169,77,0.15)",
            color: (job as any).priority >= 2 ? "#e94560" : "#ffa94d",
          }}>
            {(job as any).priority >= 2 ? "HIGHEST" : "HIGH"}
          </span>
        )}
      </span>
      {job.status === "completed" && (
        <>
          {job.job_type === "health_check" ? (
            job.error_log && job.error_log.startsWith("Corrupt") ? (
              <span
                title={job.error_log}
                style={{ fontSize: 11, color: "#ffffff", background: "var(--danger)", padding: "1px 6px", borderRadius: 3, fontWeight: 600 }}
              >Corrupt</span>
            ) : (
              <span
                style={{ fontSize: 11, color: "#ffffff", background: "var(--success)", padding: "1px 6px", borderRadius: 3, fontWeight: 600 }}
              >Healthy</span>
            )
          ) : (
            <>
              {job.space_saved > 0 && (
                <span style={{ color: "var(--success)", fontSize: 11 }}>saved {formatBytes(job.space_saved)}</span>
              )}
              {job.space_saved <= 0 && job.error_log?.startsWith("VMAF ") ? (
                // VMAF-rejected encodes get a distinct amber badge so the
                // user can tell at a glance WHY this file wasn't converted
                // (vs. a generic "no savings" skip). Full reason lives in
                // the expanded view + tooltip.
                <span
                  title={job.error_log}
                  style={{ fontSize: 11, color: "#ffa94d", background: "rgba(255,169,77,0.15)", padding: "1px 6px", borderRadius: 3, fontWeight: 600 }}
                >
                  VMAF rejected
                </span>
              ) : job.space_saved <= 0 ? (
                <span style={{ fontSize: 11, color: "var(--text-muted)", background: "var(--border)", padding: "1px 6px", borderRadius: 3 }}>Ignored</span>
              ) : null}
            </>
          )}
          {onUndo && (job as any).backup_path && (
            <button
              onClick={(e) => { e.stopPropagation(); onUndo(job.id); }}
              style={{ ...iconBtnStyle, color: "var(--accent)", marginLeft: 4, fontSize: 14 }}
              title="Restore original"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>
              </svg>
            </button>
          )}
          <button onClick={(e) => { e.stopPropagation(); onRemove(job.id); }}
            style={{ ...iconBtnStyle, color: "var(--text-muted)", marginLeft: 4 }}
            title="Remove">
            &times;
          </button>
        </>
      )}
      {job.status === "failed" && (
        <>
          {onRetry && (
            <button className="btn btn-secondary" style={{ fontSize: 11, padding: "2px 8px" }}
              onClick={(e) => { e.stopPropagation(); onRetry(job.id); }}>Retry</button>
          )}
          <button onClick={(e) => { e.stopPropagation(); onRemove(job.id); }}
            style={{ ...iconBtnStyle, color: "#e94560", marginLeft: 8 }}
            title="Remove">
            &times;
          </button>
        </>
      )}
      {job.status === "pending" && (
        <>
          <span className="job-type-badge" style={{ background: "var(--border)" }}>{typeBadge}</span>
          <span style={{ fontSize: 10, padding: "1px 5px", borderRadius: 3, background: "var(--bg-tertiary)", color: "var(--text-secondary)", marginLeft: 4 }}>
            {(job.encoder === "libx265" || (!job.encoder && encodingDefaults?.default_encoder === "libx265"))
              ? `${(job.libx265_preset || encodingDefaults?.libx265_preset || "medium").charAt(0).toUpperCase() + (job.libx265_preset || encodingDefaults?.libx265_preset || "medium").slice(1)} / CRF ${job.libx265_crf ?? encodingDefaults?.libx265_crf ?? 20}`
              : `${(job.nvenc_preset || encodingDefaults?.nvenc_preset || "P6").toUpperCase()} / CQ ${job.nvenc_cq ?? encodingDefaults?.nvenc_cq ?? 20}`
            }
          </span>
          <div style={{ display: "inline-flex", alignItems: "center", gap: 2, marginLeft: 6 }}>
            {onIgnore && (
              <button onClick={() => onIgnore(job.id, job.file_path)}
                style={{ ...iconBtnStyle, color: "var(--text-muted)", padding: "2px 4px", fontSize: 16, display: "inline-flex", alignItems: "center" }}
                title="Ignore this file">
                &#x2298;
              </button>
            )}
            <button onClick={() => onCancel(job.id)}
              style={{ ...iconBtnStyle, color: "var(--text-muted)", padding: "2px 4px", fontSize: 16, display: "inline-flex", alignItems: "center" }}
              title="Remove from queue">
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
            <span style={{ color: "var(--text-muted)" }}>Loading conversion details...</span>
          </div>
        ) : logData ? (
          <>
            {/* Comparison table: original vs encoded */}
            {logData.encoding_stats && (
              <div style={{ display: "grid", gridTemplateColumns: "auto 1fr 1fr", gap: "4px 16px", marginBottom: 12, fontSize: 11 }}>
                <span style={{ color: "var(--text-muted)", fontWeight: 600 }}></span>
                <span style={{ color: "var(--text-muted)", fontWeight: 600, fontSize: 10, textTransform: "uppercase" }}>Original</span>
                <span style={{ color: "var(--text-muted)", fontWeight: 600, fontSize: 10, textTransform: "uppercase" }}>Encoded</span>

                {logData.encoding_stats.input_size > 0 && <>
                  <span style={{ color: "var(--text-muted)" }}>Size</span>
                  <span style={{ color: "var(--text-secondary)" }}>{formatBytes(logData.encoding_stats.input_size)}</span>
                  {/* Negative ratio = skipped_larger (encode grew the file
                      and the original was kept). Render in a warning
                      colour with an explicit "discarded" hint so the row
                      doesn't read like a successful saving. v0.3.55+. */}
                  {logData.encoding_stats.ratio < 0 ? (
                    <span style={{ color: "#ffa94d" }}>
                      {formatBytes(logData.encoding_stats.output_size)}{" "}
                      <span style={{ opacity: 0.7 }}>
                        ({Math.abs(logData.encoding_stats.ratio)}% larger — discarded)
                      </span>
                    </span>
                  ) : (
                    <span style={{ color: "var(--success)" }}>
                      {formatBytes(logData.encoding_stats.output_size)}{" "}
                      <span style={{ opacity: 0.6 }}>({logData.encoding_stats.ratio}% saved)</span>
                    </span>
                  )}
                </>}

                {logData.encoding_stats.input_bitrate != null && <>
                  <span style={{ color: "var(--text-muted)" }}>Bitrate</span>
                  <span style={{ color: "var(--text-secondary)" }}>{logData.encoding_stats.input_bitrate} Mbps</span>
                  <span style={{ color: "var(--text-secondary)" }}>{logData.encoding_stats.output_bitrate} Mbps</span>
                </>}

                <span style={{ color: "var(--text-muted)" }}>Codec</span>
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
                    if (enc === "libx265") return "x265 (CPU)";
                    if (enc === "qsv") return "h265 (QSV)";
                    if (enc === "vaapi") return "h265 (VAAPI)";
                    return "h265 (NVENC)";
                  })()}
                </span>

                {logData.vmaf_score != null && <>
                  <span style={{ color: "var(--text-muted)" }}>VMAF</span>
                  <span style={{ color: "var(--text-muted)", opacity: 0.5 }}>100 (ref)</span>
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
                        title="VMAF measurement-suspect: libvmaf desynced on every analysis window we tried. The score is unreliable; the encode is almost certainly visually fine. Re-measure from Settings → Encoding → VMAF."
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
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#ffa94d" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
                  <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
                  <line x1="12" y1="9" x2="12" y2="13"/>
                  <line x1="12" y1="17" x2="12.01" y2="17"/>
                </svg>
                <div>
                  <div style={{ fontSize: 12, fontWeight: 600, color: "#ffa94d" }}>Encode rejected by VMAF threshold</div>
                  <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 2 }}>
                    {job.error_log} The original file was kept. Retry with a lower CQ/CRF or a slower preset
                    to get a better score, or lower the threshold in Settings → Video.
                  </div>
                </div>
              </div>
            )}

            {/* Encoding settings + timing */}
            <div style={{ display: "flex", flexWrap: "wrap", gap: "4px 14px", marginBottom: 10, fontSize: 11 }}>
              {logData.encoding_stats?.encoder && (
                <span style={{ color: "var(--text-muted)" }}>Encoder: <strong style={{ color: "var(--text-secondary)" }}>{logData.encoding_stats.encoder}</strong></span>
              )}
              {logData.encoding_stats?.preset && (
                <span style={{ color: "var(--text-muted)" }}>Preset: <strong style={{ color: "var(--text-secondary)" }}>{logData.encoding_stats.preset.toUpperCase()}</strong></span>
              )}
              {logData.encoding_stats?.cq != null && (
                <span style={{ color: "var(--text-muted)" }}>CQ: <strong style={{ color: "var(--text-secondary)" }}>{logData.encoding_stats.cq}</strong></span>
              )}
              {logData.encoding_stats?.crf != null && logData.encoding_stats?.encoder === "libx265" && (
                <span style={{ color: "var(--text-muted)" }}>CRF: <strong style={{ color: "var(--text-secondary)" }}>{logData.encoding_stats.crf}</strong></span>
              )}
              {logData.encoding_stats?.encode_seconds > 0 && (
                <span style={{ color: "var(--text-muted)" }}>Encode time: <strong style={{ color: "var(--text-secondary)" }}>{formatDuration("2000-01-01T00:00:00", new Date(new Date("2000-01-01T00:00:00").getTime() + logData.encoding_stats.encode_seconds * 1000).toISOString())}</strong></span>
              )}
              <span style={{ color: "var(--text-muted)" }}>Type: {job.job_type}</span>
              {logData.started_at && logData.completed_at && (
                <span style={{ color: "var(--text-muted)" }}>Total: {formatDuration(logData.started_at, logData.completed_at)}</span>
              )}
              {logData.started_at && <span style={{ color: "var(--text-muted)" }}>Started: {new Date(logData.started_at).toLocaleString()}</span>}
            </div>

            {/* Health check result (inline post-conversion OR standalone health_check job) */}
            {job.health_status && (
              <div style={{ marginBottom: 10, padding: "8px 10px", borderRadius: 4, background: "var(--bg-primary)", border: "1px solid var(--border)" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: job.health_status === "corrupt" && job.health_errors_json ? 6 : 0 }}>
                  <span style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: 0.5 }}>Health check</span>
                  <span style={{
                    fontSize: 11, fontWeight: 600, padding: "1px 6px", borderRadius: 3, color: "#ffffff",
                    background: job.health_status === "corrupt" ? "var(--danger)" : "var(--success)",
                  }}>
                    {job.health_status === "corrupt" ? "Corrupt" : "Healthy"}
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
                    <ul style={{ margin: 0, padding: "4px 0 0 18px", color: "#e94560", fontSize: 11, fontFamily: "var(--font-mono)", lineHeight: 1.5 }}>
                      {errs.slice(0, 8).map((e, i) => <li key={i} style={{ wordBreak: "break-word" }}>{e}</li>)}
                      {errs.length > 8 && <li style={{ opacity: 0.6 }}>...and {errs.length - 8} more</li>}
                    </ul>
                  );
                })()}
              </div>
            )}

            {/* ffmpeg command (copyable) */}
            {logData.ffmpeg_command && (
              <div style={{ marginBottom: 8 }}>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 4, display: "flex", alignItems: "center", gap: 6 }}>
                  <span>ffmpeg command</span>
                  <button
                    onClick={(e) => { e.stopPropagation(); navigator.clipboard.writeText(logData.ffmpeg_command); }}
                    style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 2, display: "inline-flex", opacity: 0.6 }}
                    title="Copy command"
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
                  style={{ background: "none", border: "none", color: "var(--accent)", fontSize: 11, cursor: "pointer", padding: 0 }}
                >
                  {showFullLog ? "Hide" : "Show"} ffmpeg output ({logData.ffmpeg_log.split("\n").length} lines)
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
            <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 6, opacity: 0.6, wordBreak: "break-all" }}>
              {job.file_path}
            </div>
          </>
        ) : (
          <div style={{ color: "var(--text-muted)" }}>No conversion details available</div>
        )}
      </div>
    )}

    {/* Expanded error details for failed jobs */}
    {expanded && job.status === "failed" && (
      <div style={{
        padding: "8px 12px 8px 36px", fontSize: 12, lineHeight: 1.6,
        background: "rgba(233,69,96,0.05)", borderBottom: "1px solid var(--bg-card)",
      }}>
        {job.error_log ? (
          <div style={{ fontFamily: "var(--font-mono)", fontSize: 11, color: "#e94560", whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
            {job.error_log}
          </div>
        ) : (
          <div style={{ color: "var(--text-muted)" }}>No error details available</div>
        )}
        <div style={{ display: "flex", gap: 12, marginTop: 6, fontSize: 11, color: "var(--text-muted)" }}>
          <span>Type: {job.job_type}</span>
          {job.encoder && <span>Encoder: {job.encoder}</span>}
          {(job as any).original_size > 0 && <span>Size: {formatBytes((job as any).original_size)}</span>}
          {job.started_at && <span>Started: {new Date(job.started_at).toLocaleString()}</span>}
          {job.completed_at && <span>Failed: {new Date(job.completed_at).toLocaleString()}</span>}
        </div>
        <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4, opacity: 0.6, wordBreak: "break-all" }}>
          {job.file_path}
        </div>
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
