import { useState, useEffect, useCallback, useRef } from "react";
import { Trans, useTranslation } from "react-i18next";
import { getSystemMetrics, getNodeMetrics, type NodeMetricsEntry } from "../api";
import { fmtNum, fmtBytes } from "../fmt";
import { serverText } from "../i18n/server";
import { useVisibleInterval } from "../useVisibleInterval";

// A load meter, one per line (v0.10.0: was a ¾-circle gauge with a needle,
// then thin bars side by side that were hard to see): the label, the amounts
// and the percentage, over the queue's progress bar the width of the card.
// Accent while calm; orange past 70%, red past 90%.
function Meter({ value, max, label, unit = "" }: { value: number; max: number; label: string; unit?: string }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  const hot = pct > 90 ? "var(--danger)" : pct > 70 ? "var(--caution)" : undefined;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
        <span style={{ fontSize: 13, color: "var(--text-secondary)", flex: 1, minWidth: 0 }}>{label}</span>
        {unit !== "%" && (
          <span style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-muted)" }}>
            {fmtNum(value)}{unit} / {fmtNum(max)}{unit}
          </span>
        )}
        <span style={{ fontSize: 13, fontWeight: 600, fontFamily: "var(--font-mono)", color: hot || "var(--text-secondary)",
                       minWidth: 36, textAlign: "right" }}>{Math.round(pct)}%</span>
      </div>
      <div className="progress-bar-track">
        <div className="progress-bar-fill" style={{ width: `${pct}%`, background: hot }} />
      </div>
    </div>
  );
}

function Meters({ children }: { children: React.ReactNode }) {
  return <div style={{ display: "flex", flexDirection: "column", gap: 14, margin: "4px 0 16px" }}>{children}</div>;
}

// A figure and what it is. Softer than white (v0.10.0: a page of large white
// numbers was hard on the eyes); `color` for one that means something.
function Figure({ value, label, color }: { value: React.ReactNode; label: string; color?: string }) {
  return (
    <div style={{ flex: 1, textAlign: "center", minWidth: 80 }}>
      <div style={{ fontSize: 22, fontWeight: 600, color: color || "var(--text-secondary)", lineHeight: 1.3 }}>{value}</div>
      <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{label}</div>
    </div>
  );
}

function MetricCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ background: "var(--bg-card)", padding: 16, borderRadius: 6 }}>
      <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", marginBottom: 12 }}>{title}</div>
      {children}
    </div>
  );
}

function StatRow({ label, value, color }: { label: string; value: string | number; color?: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 4 }}>
      <span style={{ color: "var(--text-muted)" }}>{label}</span>
      <span style={{ color: color || "var(--text-secondary)", fontWeight: 600 }}>{value}</span>
    </div>
  );
}

export default function MonitorPage() {
  const { t } = useTranslation(["monitor", "common"]);
  const [metrics, setMetrics] = useState<any>(null);
  const [nodeMetrics, setNodeMetrics] = useState<NodeMetricsEntry[]>([]);
  const [error, setError] = useState(false);

  // Every 3 seconds while the tab is visible, never two at once (FE#14:
  // slow responses stacked up, and hidden tabs kept polling).
  const inFlight = useRef(false);
  const load = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      await Promise.all([
        getSystemMetrics().then(setMetrics).catch(() => setError(true)),
        // Node metrics are optional — no error state if none are registered.
        getNodeMetrics().then(r => setNodeMetrics(r.nodes || [])).catch(() => setNodeMetrics([])),
      ]);
    } finally {
      inFlight.current = false;
    }
  }, []);
  useEffect(() => { load(); }, [load]);
  useVisibleInterval(load, 3000);

  if (error && !metrics) {
    return (
      <div style={{ padding: 40, textAlign: "center", color: "var(--text-muted)" }}>
        {t("monitor:loadFailed")}
      </div>
    );
  }

  if (!metrics) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: 60, gap: 12 }}>
        <div className="spinner" />
        <span style={{ color: "var(--text-muted)" }}>{t("monitor:loading")}</span>
      </div>
    );
  }

  const { gpu, cpu, memory, disk_io, plex, shrinkerr } = metrics;
  // The local node's encoding capability info rides the same /api/nodes/metrics
  // endpoint used for remote workers. Pull it out here so we can render an
  // "Encoding Capability" badge in the main System Monitor alongside the GPU
  // gauges — that way CPU-only hosts and out-of-date-driver hosts see a clear
  // status instead of a mysterious absent-NVENC.
  const localNode = nodeMetrics.find(n => n.node_id === "local");

  return (
    <div>
      <h2 style={{ color: "var(--text-primary)", fontSize: 20, marginBottom: 20 }}>{t("monitor:title")}</h2>

      {/* Detail cards */}
      <div className="monitor-detail-grid" style={{ display: "grid", gap: 12 }}>
        {/* GPU — present only when nvidia-smi returned something real. The
            capability strip under the gauges confirms whether NVENC itself
            is actually working (vs. GPU visible but driver too old, etc.). */}
        {gpu && (
          <MetricCard title={`GPU — ${gpu.name}`}>
            <Meters>
              <Meter value={gpu.gpu_util} max={100} label={t("monitor:gpu.utilization")} unit="%" />
              <Meter value={Math.round(gpu.memory_used_mb)} max={Math.round(gpu.memory_total_mb)} label="VRAM" unit=" MB" />
              <Meter value={Math.round(gpu.power_draw_w)} max={Math.round(gpu.power_limit_w)} label={t("monitor:gpu.power")} unit=" W" />
            </Meters>
            <div>
              <StatRow label={t("monitor:gpu.temperature")} value={`${gpu.temperature_c}°C`} color={gpu.temperature_c > 85 ? "var(--danger)" : gpu.temperature_c > 70 ? "var(--caution)" : undefined} />
              {gpu.encoder_util != null && <StatRow label={t("monitor:gpu.encoder")} value={`${gpu.encoder_util}%`} />}
              {gpu.decoder_util != null && <StatRow label={t("monitor:gpu.decoder")} value={`${gpu.decoder_util}%`} />}
            </div>
            {localNode && <NodeEncodingStatus entry={localNode} />}
          </MetricCard>
        )}

        {/* No-GPU host — still show encoding capability so the user knows
            what they're using. Two possible flavours of "no GPU stats":
            (1) NVENC actually works on this host but nvidia-smi can't
                read stats (NVIDIA Container Toolkit running without the
                `utility` capability is the common cause). Conversions
                go through the GPU; we just can't render utilization /
                temp / VRAM gauges. v0.3.103+ detects NVENC via a test
                encode, so capabilities can include "nvenc" even when
                the GPU stats endpoint comes back empty.
            (2) Genuinely CPU-only host (no NVIDIA hardware, or driver
                too old / unavailable).
            v0.3.118+. */}
        {!gpu && localNode && (
          <MetricCard title={t("monitor:capability.title")}>
            {localNode.capabilities.includes("nvenc") ? (
              <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 6 }}>
                <Trans t={t} i18nKey="monitor:capability.nvencNoStats" components={{ code: <code /> }} />
              </div>
            ) : (
              <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 6 }}>
                {(() => {
                  // v0.10.0: QSV / VAAPI / VideoToolbox hosts were told "CPU".
                  const hw = ["videotoolbox", "qsv", "vaapi"].find(e => localNode.capabilities.includes(e));
                  return hw
                    ? t("monitor:capability.noNvidiaUsing", { encoder: t(`monitor:capability.encoderNames.${hw}`) })
                    : t("monitor:capability.cpuOnly");
                })()}
              </div>
            )}
            <NodeEncodingStatus entry={localNode} />
          </MetricCard>
        )}

        {/* CPU & Memory */}
        <MetricCard title={t("monitor:cpu.title", { count: cpu.cpu_count })}>
          <Meters>
            <Meter value={cpu.cpu_percent} max={100} label="CPU" unit="%" />
            <Meter value={memory.ram_used_gb} max={memory.ram_total_gb} label="RAM" unit=" GB" />
            <Meter value={memory.swap_used_gb || 0} max={memory.ram_total_gb} label={t("monitor:cpu.swap")} unit=" GB" />
          </Meters>
          <div>
            <StatRow label={t("monitor:cpu.loadAverage")} value={cpu.load_avg.map((l: number) => l.toFixed(2)).join(" / ")} />
            {cpu.cpu_freq_mhz && <StatRow label={t("monitor:cpu.frequency")} value={`${cpu.cpu_freq_mhz} MHz`} />}
          </div>
        </MetricCard>

        {/* Disk I/O */}
        <MetricCard title={t("monitor:disk.title")}>
          <div style={{ display: "flex", gap: 24 }}>
            <Figure value={disk_io.read_mbps} label={t("monitor:disk.read")} />
            <Figure value={disk_io.write_mbps} label={t("monitor:disk.write")} />
          </div>
        </MetricCard>

        {/* Plex Streams */}
        <MetricCard title={t("monitor:plex.title")}>
          <div style={{ display: "flex", gap: 24, marginBottom: plex?.sessions?.length > 0 ? 10 : 0 }}>
            <Figure value={fmtNum(plex?.total)} label={t("monitor:plex.total")} />
            <Figure value={fmtNum(plex?.transcoding)} label={t("monitor:plex.transcoding")}
              color={(plex?.transcoding || 0) > 0 ? "var(--caution)" : undefined} />
            <Figure value={fmtNum(plex?.direct)} label={t("monitor:plex.directPlay")} />
          </div>
          {plex?.sessions?.length > 0 && (
            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 8 }}>
              {plex.sessions.map((s: any, i: number) => (
                <div key={i} style={{ display: "flex", justifyContent: "space-between", gap: 12, fontSize: 11, marginBottom: 3 }}>
                  <span style={{ color: "var(--text-secondary)" }}>{s.user}: {s.title}</span>
                  <span style={{ color: s.is_transcoding ? "var(--caution)" : "var(--text-muted)", flexShrink: 0 }}>
                    {s.is_transcoding ? t("monitor:plex.transcoding") : t("monitor:plex.direct")}
                  </span>
                </div>
              ))}
            </div>
          )}
        </MetricCard>

        {/* Shrinkerr Workload — spans full width */}
        <div style={{ gridColumn: "1 / -1" }}>
          <MetricCard title={t("monitor:workload.title")}>
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
              <Figure value={fmtNum(shrinkerr?.running_jobs)} label={t("common:status.running")} />
              <Figure value={fmtNum(shrinkerr?.pending_jobs)} label={t("common:status.pending")} />
              <Figure value={fmtNum(shrinkerr?.completed_jobs)} label={t("common:status.completed")} />
              {(shrinkerr?.failed_jobs || 0) > 0 && (
                <Figure value={fmtNum(shrinkerr.failed_jobs)} label={t("common:status.failed")} color="var(--danger)" />
              )}
              <Figure value={shrinkerr?.avg_fps || 0} label={t("monitor:workload.liveFps")} />
              <Figure value={shrinkerr?.lifetime_avg_fps || 0} label={t("monitor:workload.avgFps")} />
              <Figure value={fmtBytes(shrinkerr?.total_saved || 0)} label={t("monitor:workload.totalSaved")} color="var(--accent-text)" />
            </div>
          </MetricCard>
        </div>
      </div>

      {/* ── Worker Nodes ─────────────────────────────────────────────── */}
      {/* Filter out the built-in "local" node — its metrics are already
          rendered in the System Monitor cards above, so listing it again
          here is redundant AND always shows "no metrics yet" because the
          server doesn't push metrics to itself. */}
      {(() => {
        const remote = nodeMetrics.filter(n => n.node_id !== "local");
        if (remote.length === 0) return null;
        const reporting = remote.filter(n => n.metrics).length;
        return (
          <>
            <h3 style={{ color: "var(--text-primary)", fontSize: 16, marginTop: 28, marginBottom: 12, display: "flex", alignItems: "center", gap: 8 }}>
              {t("monitor:nodes.title")}
              <span style={{ fontSize: 12, color: "var(--text-muted)", fontWeight: 400 }}>
                {t("monitor:nodes.reporting", { reporting, total: remote.length })}
              </span>
            </h3>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(320px, 100%), 1fr))", gap: 12 }}>
              {remote.map(n => <NodeMetricCard key={n.node_id} entry={n} />)}
            </div>
          </>
        );
      })()}

      <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 12, textAlign: "center" }}>
        {t("monitor:autoRefresh")}
      </div>
    </div>
  );
}


// Single worker-node card. Uses the same Meter/StatRow building blocks as
// the server cards above so the Monitor page stays visually consistent.
function NodeMetricCard({ entry }: { entry: NodeMetricsEntry }) {
  const { t } = useTranslation(["monitor", "common"]);
  const m = entry.metrics;
  const stale = m && entry.age_seconds !== null && (entry.age_seconds ?? 0) > 15;

  // Status badge color
  const statusColor = entry.status === "working" ? "var(--accent-text)"
    : entry.status === "online" ? "var(--success)"
    : entry.status === "error" ? "var(--danger)"
    : "var(--text-muted)";

  return (
    <div style={{ background: "var(--bg-card)", padding: 14, borderRadius: 6, border: entry.status === "working" ? "1px solid var(--accent)" : "1px solid transparent" }}>
      {/* Header: name + status + hostname */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
        <span style={{
          width: 8, height: 8, borderRadius: "50%", background: statusColor,
          flexShrink: 0,
        }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {entry.name}
          </div>
          <div style={{ fontSize: 10, color: "var(--text-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {entry.hostname || entry.node_id}
            {entry.current_job_id != null && <> · {t("monitor:nodes.job", { id: entry.current_job_id })}</>}
          </div>
        </div>
        {stale && (
          <span style={{ fontSize: 9, padding: "2px 6px", borderRadius: 3, background: "rgba(231,76,60,0.15)", color: "var(--danger)" }}>
            {t("monitor:nodes.stale", { seconds: Math.round(entry.age_seconds ?? 0) })}
          </span>
        )}
        {!m && (
          <span style={{ fontSize: 10, color: "var(--text-muted)", fontStyle: "italic" }}>
            {t("monitor:nodes.noMetrics")}
          </span>
        )}
      </div>

      {/* Encoding capability strip — shows driver + advertised encoders and,
          when NVENC is OFF, surfaces the specific reason so users know
          exactly what to fix. Always rendered so CPU-only nodes show a
          clean "Using CPU encoder (libx265)" instead of a mysterious void. */}
      <NodeEncodingStatus entry={entry} />

      {m && (
        <>
          {/* CPU + RAM gauges (always) */}
          <Meters>
            <Meter value={m.cpu.cpu_percent} max={100} label="CPU" unit="%" />
            <Meter value={m.memory.ram_used_gb} max={m.memory.ram_total_gb} label="RAM" unit=" GB" />
            {m.gpu && <Meter value={m.gpu.gpu_util} max={100} label="GPU" unit="%" />}
          </Meters>

          {/* GPU details */}
          {m.gpu && (
            <div style={{ marginTop: 6, paddingTop: 6, borderTop: "1px solid var(--border)" }}>
              <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 4 }}>{m.gpu.name}</div>
              <StatRow label="VRAM" value={`${Math.round(m.gpu.memory_used_mb)} / ${Math.round(m.gpu.memory_total_mb)} MB`} />
              <StatRow label={t("monitor:nodes.temp")} value={`${m.gpu.temperature_c}°C`} color={m.gpu.temperature_c > 85 ? "var(--danger)" : m.gpu.temperature_c > 70 ? "var(--caution)" : undefined} />
              <StatRow label={t("monitor:gpu.power")} value={`${Math.round(m.gpu.power_draw_w)} / ${Math.round(m.gpu.power_limit_w)} W`} />
              {m.gpu.encoder_util != null && (
                <StatRow label="NVENC" value={`${m.gpu.encoder_util}%`} />
              )}
              {m.gpu.decoder_util != null && (
                <StatRow label="NVDEC" value={`${m.gpu.decoder_util}%`} />
              )}
            </div>
          )}

          {/* CPU details if no GPU (keep card balanced) */}
          {!m.gpu && (
            <div style={{ marginTop: 6, paddingTop: 6, borderTop: "1px solid var(--border)" }}>
              <StatRow label={t("monitor:nodes.cores")} value={m.cpu.cpu_count} />
              <StatRow label={t("monitor:nodes.load")} value={m.cpu.load_avg.map(l => l.toFixed(2)).join(" / ")} />
              {m.cpu.cpu_freq_mhz && <StatRow label={t("monitor:cpu.frequency")} value={`${m.cpu.cpu_freq_mhz} MHz`} />}
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────────
// Encoding capability strip
//
// Three states this needs to communicate cleanly:
//   1. NVENC working          → green check + driver version
//   2. NVENC off, reason known → yellow warning + actionable reason
//   3. CPU-only host          → neutral info; libx265 is the only option anyway
//
// The reason string comes straight from the backend (ffmpeg's stderr tail on
// the test-encode, or one of our sentinel strings like "no NVIDIA GPU
// detected"). We don't try to pattern-match it — it's already terse.
// ──────────────────────────────────────────────────────────────────────────
function NodeEncodingStatus({ entry }: { entry: NodeMetricsEntry }) {
  const { t } = useTranslation(["monitor", "common"]);
  const hasNvenc = entry.capabilities.includes("nvenc");
  const hasLibx265 = entry.capabilities.includes("libx265");
  const reason = entry.nvenc_unavailable_reason
    ? serverText("serverJobs", entry.nvenc_unavailable_reason_key, entry.nvenc_unavailable_reason_params, entry.nvenc_unavailable_reason)
    : null;
  const driver = entry.driver_version;

  // Nothing advertised at all — shouldn't happen, but don't crash the card.
  if (!hasNvenc && !hasLibx265) return null;

  return (
    <div style={{
      marginTop: 4, marginBottom: 10, padding: "6px 8px",
      borderRadius: 4, background: "var(--bg-primary)",
      fontSize: 11, color: "var(--text-muted)",
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
        {/* NVENC badge */}
        {hasNvenc ? (
          <span style={{ color: "var(--success)", display: "inline-flex", alignItems: "center", gap: 4 }}>
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="20 6 9 17 4 12"/>
            </svg>
            NVENC
          </span>
        ) : reason ? (
          <span title={reason} style={{ color: "var(--caution)", display: "inline-flex", alignItems: "center", gap: 4 }}>
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
              <line x1="12" y1="9" x2="12" y2="13"/>
              <line x1="12" y1="17" x2="12.01" y2="17"/>
            </svg>
            {t("monitor:capability.nvencOff")}
          </span>
        ) : null}
        {/* Separator only when there's something on both sides */}
        {hasNvenc && hasLibx265 && <span style={{ opacity: 0.4 }}>·</span>}
        {/* libx265 always shows when available — it's the reliable CPU fallback */}
        {hasLibx265 && (
          <span style={{ color: hasNvenc ? "var(--text-muted)" : "var(--success)" }}>
            libx265
          </span>
        )}
        {/* Driver version when we have it (regardless of NVENC state — helps debug) */}
        {driver && (
          <span style={{ marginLeft: "auto", color: "var(--text-muted)" }}>
            {t("monitor:capability.driver", { version: driver })}
          </span>
        )}
      </div>
      {/* Full reason text for NVENC-off nodes. Wrap so long strings don't
          blow out the card layout; keep it terse so it stays on one line
          when possible. */}
      {!hasNvenc && reason && (
        <div style={{ marginTop: 4, fontSize: 10, color: "var(--caution)", wordBreak: "break-word", lineHeight: 1.4 }}>
          {reason}
        </div>
      )}
    </div>
  );
}
