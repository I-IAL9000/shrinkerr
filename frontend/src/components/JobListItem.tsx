import { useState } from "react";
import type { Job } from "../types";
import { getJobLog } from "../api";

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

export default function JobListItem({ job, onCancel, onRetry, onRemove, onIgnore, onUndo, checked, onCheck, encodingDefaults }: JobListItemProps) {
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
              {job.space_saved <= 0 && (
                <span style={{ fontSize: 11, color: "var(--text-muted)", background: "var(--border)", padding: "1px 6px", borderRadius: 3 }}>Ignored</span>
              )}
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
                  <span style={{ color: "var(--success)" }}>{formatBytes(logData.encoding_stats.output_size)} <span style={{ opacity: 0.6 }}>({logData.encoding_stats.ratio}% saved)</span></span>
                </>}

                {logData.encoding_stats.input_bitrate != null && <>
                  <span style={{ color: "var(--text-muted)" }}>Bitrate</span>
                  <span style={{ color: "var(--text-secondary)" }}>{logData.encoding_stats.input_bitrate} Mbps</span>
                  <span style={{ color: "var(--text-secondary)" }}>{logData.encoding_stats.output_bitrate} Mbps</span>
                </>}

                <span style={{ color: "var(--text-muted)" }}>Codec</span>
                <span style={{ color: "var(--text-secondary)" }}>x264</span>
                <span style={{ color: "var(--text-secondary)" }}>x265 ({logData.encoding_stats.encoder === "libx265" ? "CPU" : "NVENC"})</span>

                {logData.vmaf_score != null && <>
                  <span style={{ color: "var(--text-muted)" }}>VMAF</span>
                  <span style={{ color: "var(--text-muted)", opacity: 0.5 }}>100 (ref)</span>
                  <span style={{
                    color: logData.vmaf_score >= 90 ? "#40c057" : logData.vmaf_score >= 80 ? "#ffa94d" : "#e94560",
                    fontWeight: 600,
                  }}>
                    {logData.vmaf_score} ({logData.vmaf_score >= 93 ? "Excellent" : logData.vmaf_score >= 87 ? "Good" : logData.vmaf_score >= 80 ? "Fair" : "Poor"})
                  </span>
                </>}
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
