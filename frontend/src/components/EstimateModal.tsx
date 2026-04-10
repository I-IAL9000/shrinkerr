import { useState, useEffect } from "react";
import { estimateJobs, startTestEncode } from "../api";

function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 4) return `${(bytes / (1024 ** 4)).toFixed(2)} TB`;
  if (bytes >= 1024 ** 3) return `${(bytes / (1024 ** 3)).toFixed(1)} GB`;
  return `${(bytes / (1024 ** 2)).toFixed(0)} MB`;
}

function formatTime(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return h > 24 ? `${(seconds / 86400).toFixed(1)} days` : `${h}h ${m}m`;
}

const PRIORITY_LABELS = ["Normal", "High", "Highest"];
const PRIORITY_COLORS = ["var(--text-muted)", "#ffa94d", "#e94560"];

export interface EncodingOverrides {
  encoder?: string;
  nvenc_preset?: string;
  nvenc_cq?: number;
  libx265_crf?: number;
  libx265_preset?: string;
  audio_codec?: string;
  audio_bitrate?: number;
  target_resolution?: string;
  force_reencode?: boolean;
}

const CPU_PRESETS = [
  { value: "ultrafast", label: "Ultrafast" },
  { value: "superfast", label: "Superfast" },
  { value: "veryfast", label: "Very Fast" },
  { value: "faster", label: "Faster" },
  { value: "fast", label: "Fast" },
  { value: "medium", label: "Medium (default)" },
  { value: "slow", label: "Slow" },
  { value: "slower", label: "Slower" },
  { value: "veryslow", label: "Very Slow (Best quality)" },
];

interface EstimateModalProps {
  filePaths: string[];
  hasIgnoredFiles?: boolean;
  activeFilter?: string;
  onConfirm: (priority: number, overrideRules: boolean, encodingOverrides?: EncodingOverrides) => void;
  onCancel: () => void;
}

export default function EstimateModal({ filePaths, hasIgnoredFiles, activeFilter, onConfirm, onCancel }: EstimateModalProps) {
  const [estimate, setEstimate] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [priority, setPriority] = useState(0);
  const [overrideRules, setOverrideRules] = useState(!!hasIgnoredFiles);
  const [settingsOpen, setSettingsOpen] = useState(false);

  // Encoding overrides — null = auto
  const [encoder, setEncoder] = useState<string | null>(null);
  const [preset, setPreset] = useState<string | null>(null);
  const [cq, setCq] = useState<number | null>(null);
  const [audioCdc, setAudioCdc] = useState<string | null>(null);
  // Test encode state
  const [testRunning, setTestRunning] = useState(false);
  const [testResult, setTestResult] = useState<any>(null);
  const [testStep, setTestStep] = useState("");
  const [audioBr, setAudioBr] = useState<number | null>(null);
  const [resolution, setResolution] = useState<string | null>(null);
  const [forceReencode, setForceReencode] = useState(false);

  // Debounced estimate refresh
  useEffect(() => {
    setLoading(true);
    const overrides: Record<string, any> = {};
    if (cq !== null) {
      if (encoder === "libx265") overrides.libx265_crf_override = cq;
      else overrides.nvenc_cq_override = cq;
    }
    if (forceReencode) overrides.force_reencode = true;
    if (activeFilter && activeFilter !== "all") overrides.filter = activeFilter;
    estimateJobs(filePaths, overrideRules, overrides).then(data => {
      setEstimate(data);
      setLoading(false);
    }).catch(() => setLoading(false));
  }, [overrideRules, cq, forceReencode]);

  const isCpu = encoder === "libx265";
  const buildOverrides = (): EncodingOverrides | undefined => {
    const o: EncodingOverrides = {};
    if (encoder !== null) o.encoder = encoder;
    if (preset !== null) {
      if (isCpu) o.libx265_preset = preset;
      else o.nvenc_preset = preset;
    }
    if (cq !== null) {
      if (isCpu) o.libx265_crf = cq;
      else o.nvenc_cq = cq;
    }
    if (audioCdc !== null) o.audio_codec = audioCdc;
    if (audioBr !== null) o.audio_bitrate = audioBr;
    if (resolution !== null) o.target_resolution = resolution;
    if (forceReencode) o.force_reencode = true;
    return Object.keys(o).length > 0 ? o : undefined;
  };

  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.7)", display: "flex",
      alignItems: "center", justifyContent: "center", zIndex: 1000,
    }} onClick={e => { if (e.target === e.currentTarget) onCancel(); }}>
      <div style={{
        background: "var(--bg-card)", borderRadius: 8, padding: 24, minWidth: 500, maxWidth: 620,
        maxHeight: "90vh", overflowY: "auto",
        border: "1px solid var(--border)",
      }}>
        <h3 style={{ color: "white", margin: "0 0 16px", fontSize: 16 }}>Add to Queue</h3>

        {/* Override rules toggle */}
        {hasIgnoredFiles && (
          <div style={{
            background: "rgba(255,169,77,0.1)", border: "1px solid rgba(255,169,77,0.2)",
            borderRadius: 4, padding: "10px 14px", marginBottom: 14,
            display: "flex", alignItems: "center", gap: 10,
          }}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#ffa94d" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
              <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
            </svg>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 12, color: "#ffa94d", fontWeight: 500 }}>Some selected files are covered by encoding rules</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
                Toggle override to process them anyway with default settings.
              </div>
            </div>
            <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer", flexShrink: 0 }}>
              <input type="checkbox" checked={overrideRules} onChange={() => setOverrideRules(!overrideRules)}
                style={{ accentColor: "var(--accent)" }} />
              <span style={{ fontSize: 11, color: "var(--text-secondary)", whiteSpace: "nowrap" }}>Override rules</span>
            </label>
          </div>
        )}

        {loading ? (
          <div style={{ display: "flex", alignItems: "center", gap: 12, padding: 20, justifyContent: "center" }}>
            <div className="spinner" style={{ width: 20, height: 20 }} />
            <span style={{ color: "var(--text-muted)", fontSize: 13 }}>Estimating...</span>
          </div>
        ) : estimate ? (
          <>
            {/* Summary */}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12, marginBottom: 16 }}>
              <div style={{ background: "var(--bg-primary)", padding: 12, borderRadius: 4, textAlign: "center" }}>
                <div style={{ fontSize: 22, fontWeight: "bold", color: "white" }}>{estimate.total_files}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Files to process</div>
              </div>
              <div style={{ background: "var(--bg-primary)", padding: 12, borderRadius: 4, textAlign: "center" }}>
                <div style={{ fontSize: 22, fontWeight: "bold", color: "var(--success)" }}>~{formatBytes(estimate.estimated_savings)}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Est. savings ({estimate.savings_pct}%)</div>
              </div>
              <div style={{ background: "var(--bg-primary)", padding: 12, borderRadius: 4, textAlign: "center" }}>
                <div style={{ fontSize: 22, fontWeight: "bold", color: "var(--accent)" }}>~{formatTime(estimate.estimated_time_seconds)}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Est. time</div>
              </div>
            </div>

            {/* Breakdown */}
            {(estimate.total_files > 0) && (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 16 }}>
                <div style={{ background: "var(--bg-primary)", padding: 12, borderRadius: 4 }}>
                  <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6, fontWeight: 600 }}>By job type</div>
                  {(() => {
                    const bt = estimate.by_type || {};
                    const conv = (bt.convert || 0) + (bt.combined || 0);
                    const cleanup = (bt.audio || 0) + (bt.combined || 0);
                    const items: [string, number][] = [];
                    if (conv > 0) items.push(["conversions", conv]);
                    if (cleanup > 0) items.push(["audio/sub cleanups", cleanup]);
                    if (items.length === 0) {
                      Object.entries(bt).filter(([, v]) => (v as number) > 0).forEach(([k, v]) => items.push([k, v as number]));
                    }
                    return items.map(([label, count]) => (
                      <div key={label} style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginTop: 3 }}>
                        <span style={{ color: "var(--text-muted)" }}>{label}</span>
                        <span style={{ color: "var(--text-secondary)", fontWeight: 500 }}>{count}</span>
                      </div>
                    ));
                  })()}
                </div>
                <div style={{ background: "var(--bg-primary)", padding: 12, borderRadius: 4 }}>
                  <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6, fontWeight: 600 }}>By source</div>
                  {Object.entries(estimate.by_source || {}).sort(([,a], [,b]) => (b as number) - (a as number)).map(([k, v]) => (
                    <div key={k} style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginTop: 3 }}>
                      <span style={{ color: "var(--text-muted)" }}>{k}</span>
                      <span style={{ color: "var(--text-secondary)", fontWeight: 500 }}>{v as number}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Resolution breakdown */}
            {estimate.resolution_breakdown && Object.values(estimate.resolution_breakdown).some((v: any) => v > 0) && (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 16 }}>
                {estimate.resolution_breakdown && (
                  <div style={{ background: "var(--bg-primary)", padding: 12, borderRadius: 4 }}>
                    <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6, fontWeight: 600 }}>By resolution</div>
                    {(["4k", "1080p", "720p", "sd"] as const).map(res => {
                      const count = estimate.resolution_breakdown[res] || 0;
                      if (!count) return null;
                      return (
                        <div key={res} style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginTop: 3 }}>
                          <span style={{ color: "var(--text-muted)" }}>{res === "sd" ? "SD" : res.toUpperCase()}</span>
                          <span style={{ color: "var(--text-secondary)", fontWeight: 500 }}>{count}</span>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            )}

            {estimate.total_files === 0 && (
              <div style={{ textAlign: "center", padding: 16, color: "var(--text-muted)", fontSize: 13 }}>
                No actionable files in selection. Files may already be optimized.
                {(estimate.ignored_files > 0 || estimate.skipped_by_rules > 0) && (
                  <div style={{ marginTop: 8 }}>
                    {estimate.ignored_files > 0 && (
                      <span>{estimate.ignored_files} file{estimate.ignored_files !== 1 ? "s" : ""} with <span style={{ background: "var(--border)", color: "var(--text-secondary)", padding: "1px 6px", borderRadius: 3, fontSize: 11 }}>IGNORE</span> status</span>
                    )}
                    {estimate.ignored_files > 0 && estimate.skipped_by_rules > 0 && <span> · </span>}
                    {estimate.skipped_by_rules > 0 && (
                      <span style={{ color: "#ffa94d" }}>{estimate.skipped_by_rules} skipped by rules</span>
                    )}
                  </div>
                )}
              </div>
            )}
            {estimate.total_files === 0 && (estimate.ignored_files > 0 || estimate.skipped_by_rules > 0) && (
              <div style={{ textAlign: "center", marginBottom: 12 }}>
                <label style={{ display: "inline-flex", alignItems: "center", gap: 8, cursor: "pointer", fontSize: 12, color: "var(--text-secondary)" }}>
                  <input type="checkbox" checked={overrideRules}
                    onChange={() => setOverrideRules(!overrideRules)}
                    style={{ accentColor: "var(--accent)" }} />
                  Include ignored files and override rules
                </label>
              </div>
            )}

            {/* Priority selector */}
            <div style={{ marginBottom: 16 }}>
              <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 6 }}>Priority</div>
              <div style={{ display: "flex", gap: 8 }}>
                {PRIORITY_LABELS.map((label, i) => (
                  <button key={i}
                    style={{
                      padding: "5px 14px", borderRadius: 4, fontSize: 12, cursor: "pointer",
                      border: priority === i ? `1px solid ${PRIORITY_COLORS[i]}` : "1px solid var(--border)",
                      background: priority === i ? `${PRIORITY_COLORS[i]}22` : "var(--bg-primary)",
                      color: priority === i ? PRIORITY_COLORS[i] : "var(--text-secondary)",
                      fontWeight: priority === i ? 600 : 400,
                    }}
                    onClick={() => setPriority(i)}
                  >{label}</button>
                ))}
              </div>
            </div>

            {/* Encoding Settings (expandable) */}
            <div style={{ marginBottom: 16 }}>
              <button
                onClick={() => setSettingsOpen(!settingsOpen)}
                style={{
                  background: "none", border: "1px solid var(--border)", color: "var(--text-muted)",
                  padding: "6px 12px", borderRadius: 4, fontSize: 12, cursor: "pointer", width: "100%",
                  textAlign: "left", display: "flex", alignItems: "center", gap: 6,
                }}
              >
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ transform: settingsOpen ? "rotate(90deg)" : "none", transition: "transform 0.15s" }}>
                  <polyline points="9 18 15 12 9 6"/>
                </svg>
                Encoding Settings
                <span style={{ marginLeft: "auto", opacity: 0.5, fontSize: 11 }}>
                  {cq !== null || encoder !== null || preset !== null || audioCdc !== null ? "Custom" : "Default settings"}
                </span>
              </button>

              {settingsOpen && (
                <div style={{ background: "var(--bg-primary)", borderRadius: "0 0 4px 4px", padding: 14, display: "flex", flexDirection: "column", gap: 10 }}>
                  {/* Encoder */}
                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <label style={{ fontSize: 12, color: "var(--text-muted)", width: 80, flexShrink: 0 }}>Encoder</label>
                    <select value={encoder ?? ""} onChange={e => { setEncoder(e.target.value || null); setPreset(null); }}
                      style={{ flex: 1, backgroundColor: "var(--bg-card)", color: "var(--text-secondary)", border: "1px solid var(--border)", padding: "4px 8px", borderRadius: 4, fontSize: 12 }}>
                      <option value="">Auto</option>
                      <option value="nvenc">NVENC (GPU)</option>
                      <option value="libx265">libx265 (CPU)</option>
                    </select>
                  </div>

                  {/* Quality CQ/CRF */}
                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <label style={{ fontSize: 12, color: "var(--text-muted)", width: 80, flexShrink: 0 }}>Quality ({isCpu ? "CRF" : "CQ"})</label>
                    <div style={{ flex: 1, display: "flex", alignItems: "center", gap: 8 }}>
                      <input type="range" min={15} max={30} value={cq ?? (estimate?.cq || 20)}
                        onChange={e => setCq(Number(e.target.value))}
                        style={{ flex: 1, accentColor: "var(--accent)" }} />
                      <span style={{ fontSize: 12, color: cq !== null ? "white" : "var(--text-muted)", fontWeight: 600, width: 24, textAlign: "center" }}>
                        {cq ?? (estimate?.cq || 20)}
                      </span>
                      {cq !== null && (
                        <button onClick={() => setCq(null)}
                          style={{ background: "none", border: "1px solid var(--border)", color: "var(--text-muted)", padding: "1px 6px", borderRadius: 3, fontSize: 10, cursor: "pointer" }}>
                          Reset
                        </button>
                      )}
                    </div>
                  </div>

                  {/* Preset */}
                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <label style={{ fontSize: 12, color: "var(--text-muted)", width: 80, flexShrink: 0 }}>Preset</label>
                    <select value={preset ?? ""} onChange={e => setPreset(e.target.value || null)}
                      style={{ flex: 1, backgroundColor: "var(--bg-card)", color: "var(--text-secondary)", border: "1px solid var(--border)", padding: "4px 8px", borderRadius: 4, fontSize: 12 }}>
                      <option value="">Auto</option>
                      {isCpu ? (
                        CPU_PRESETS.map(p => <option key={p.value} value={p.value}>{p.label}</option>)
                      ) : (
                        <>
                          <option value="p1">P1 — Fastest</option>
                          <option value="p2">P2 — Very Fast</option>
                          <option value="p3">P3 — Fast</option>
                          <option value="p4">P4 — Medium</option>
                          <option value="p5">P5 — Slow</option>
                          <option value="p6">P6 — Very Slow</option>
                          <option value="p7">P7 — Slowest (Best quality)</option>
                        </>
                      )}
                    </select>
                  </div>

                  {/* Audio Codec */}
                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <label style={{ fontSize: 12, color: "var(--text-muted)", width: 80, flexShrink: 0 }}>Audio</label>
                    <select value={audioCdc ?? ""} onChange={e => setAudioCdc(e.target.value || null)}
                      style={{ flex: 1, backgroundColor: "var(--bg-card)", color: "var(--text-secondary)", border: "1px solid var(--border)", padding: "4px 8px", borderRadius: 4, fontSize: 12 }}>
                      <option value="">Auto</option>
                      <option value="copy">Copy (passthrough)</option>
                      <option value="eac3">EAC3 (Dolby Digital+)</option>
                      <option value="ac3">AC3 (Dolby Digital)</option>
                      <option value="aac">AAC</option>
                      <option value="opus">Opus</option>
                    </select>
                    {audioCdc && audioCdc !== "copy" && (
                      <select value={audioBr ?? ""} onChange={e => setAudioBr(e.target.value ? Number(e.target.value) : null)}
                        style={{ width: 80, backgroundColor: "var(--bg-card)", color: "var(--text-secondary)", border: "1px solid var(--border)", padding: "4px 8px", borderRadius: 4, fontSize: 12 }}>
                        <option value="">Auto</option>
                        <option value="96">96k</option>
                        <option value="128">128k</option>
                        <option value="192">192k</option>
                        <option value="256">256k</option>
                        <option value="320">320k</option>
                        <option value="640">640k</option>
                      </select>
                    )}
                  </div>

                  {/* Resolution */}
                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <label style={{ fontSize: 12, color: "var(--text-muted)", width: 80, flexShrink: 0 }}>Resolution</label>
                    <select value={resolution ?? ""} onChange={e => setResolution(e.target.value || null)}
                      style={{ flex: 1, backgroundColor: "var(--bg-card)", color: "var(--text-secondary)", border: "1px solid var(--border)", padding: "4px 8px", borderRadius: 4, fontSize: 12 }}>
                      <option value="">Auto (keep original)</option>
                      <option value="1080p">1080p</option>
                      <option value="720p">720p</option>
                      <option value="480p">480p</option>
                    </select>
                  </div>

                  {/* Force re-encode */}
                  <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 6 }}>
                    <label style={{ fontSize: 12, color: "var(--text-muted)", width: 80, flexShrink: 0 }}>Re-encode</label>
                    <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                      <input type="checkbox" checked={forceReencode} onChange={(e) => setForceReencode(e.target.checked)}
                        style={{ accentColor: "var(--accent)" }} />
                      <span style={{ fontSize: 11, color: forceReencode ? "var(--text-secondary)" : "var(--text-muted)" }}>
                        Force re-encode all files (including x265)
                      </span>
                    </label>
                  </div>

                  {/* Test Encode */}
                  <div style={{ borderTop: "1px solid var(--border)", paddingTop: 10, marginTop: 4 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <button
                        className="btn btn-secondary"
                        style={{ fontSize: 11, padding: "4px 12px", borderRadius: 4, whiteSpace: "nowrap" }}
                        disabled={testRunning || filePaths.length === 0}
                        onClick={async () => {
                          setTestRunning(true);
                          setTestResult(null);
                          setTestStep("encoding");
                          try {
                            let testFile = filePaths.find(p => !p.endsWith("/")) || filePaths[0];
                            const result = await startTestEncode(
                              testFile,
                              encoder ?? undefined,
                              cq ?? (estimate?.cq || 20),
                              preset ?? undefined,
                            );
                            setTestResult(result);
                          } catch (err: any) {
                            setTestResult({ status: "failed", error: err.message });
                          }
                          setTestRunning(false);
                        }}
                      >
                        {testRunning ? (
                          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                            <div className="spinner" style={{ width: 12, height: 12 }} />
                            {testStep === "extracting" ? "Extracting sample..." :
                             testStep === "encoding" ? "Encoding sample..." :
                             testStep === "analyzing" ? "VMAF analysis..." : "Starting..."}
                          </span>
                        ) : "Test Encode (30s sample)"}
                      </button>
                      {testResult && testResult.status === "complete" && (
                        <div style={{ fontSize: 11, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                          <span style={{ color: "var(--text-secondary)" }}>
                            {formatBytes(testResult.original_size)} → {formatBytes(testResult.encoded_size)}
                          </span>
                          <span style={{
                            fontWeight: 600,
                            color: testResult.ratio > 50 ? "var(--success)" : testResult.ratio > 30 ? "var(--accent)" : "var(--text-muted)",
                          }}>
                            {testResult.ratio}% savings
                          </span>
                          {testResult.vmaf_score != null && (
                            <span style={{
                              padding: "1px 6px", borderRadius: 3, fontSize: 10, fontWeight: 600,
                              background: testResult.vmaf_score >= 90 ? "rgba(64,192,87,0.2)" :
                                         testResult.vmaf_score >= 80 ? "rgba(255,169,77,0.2)" : "rgba(233,69,96,0.2)",
                              color: testResult.vmaf_score >= 90 ? "#40c057" :
                                     testResult.vmaf_score >= 80 ? "#ffa94d" : "#e94560",
                            }}>
                              VMAF {testResult.vmaf_score} ({testResult.vmaf_label})
                            </span>
                          )}
                          {testResult.encoding_fps > 0 && (
                            <span style={{ color: "var(--text-muted)", fontSize: 10 }}>
                              {testResult.encoding_fps} fps
                            </span>
                          )}
                        </div>
                      )}
                      {testResult && testResult.status === "failed" && (
                        <span style={{ fontSize: 11, color: "#e94560" }}>
                          Failed: {testResult.error?.slice(0, 80)}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              )}
            </div>
          </>
        ) : (
          <div style={{ color: "var(--text-muted)", padding: 20, textAlign: "center" }}>Failed to estimate</div>
        )}

        {/* Actions */}
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 16px" }} onClick={onCancel}>
            Cancel
          </button>
          <button className="btn btn-primary" style={{ fontSize: 12, padding: "6px 16px" }}
            disabled={loading || !estimate || (estimate.total_files === 0 && !overrideRules)}
            onClick={() => onConfirm(priority, overrideRules, buildOverrides())}
          >
            Add {estimate?.total_files || filePaths.length} to Queue
          </button>
        </div>
      </div>
    </div>
  );
}
