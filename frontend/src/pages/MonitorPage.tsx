import { useState, useEffect } from "react";
import { getSystemMetrics, getNodeMetrics, type NodeMetricsEntry } from "../api";
import { fmtNum } from "../fmt";

function Gauge({ value, max, label, unit, size = 100, color }: { value: number; max: number; label: string; unit?: string; size?: number; color?: string }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  const fillColor = color || (pct > 90 ? "#e94560" : pct > 70 ? "#ffa94d" : "var(--success)");
  // Arc from 225° to -45° (270° sweep) — ¾ circle gauge
  const cx = size / 2, cy = size * 0.44, r = size * 0.38;
  const startAngle = 225, endAngle = -45, sweep = 270;
  const filledAngle = startAngle - (pct / 100) * sweep;
  const toRad = (deg: number) => (deg * Math.PI) / 180;
  const arcPoint = (angle: number) => ({
    x: cx + r * Math.cos(toRad(angle)),
    y: cy - r * Math.sin(toRad(angle)),
  });
  const strokeW = size * 0.08;
  // Background arc
  const bgStart = arcPoint(startAngle);
  const bgEnd = arcPoint(endAngle);
  const bgPath = `M ${bgStart.x} ${bgStart.y} A ${r} ${r} 0 1 1 ${bgEnd.x} ${bgEnd.y}`;
  // Value arc
  const valEnd = arcPoint(filledAngle);
  const valSweepAngle = (pct / 100) * sweep;
  const largeArc = valSweepAngle > 180 ? 1 : 0;
  const valPath = pct > 0 ? `M ${bgStart.x} ${bgStart.y} A ${r} ${r} 0 ${largeArc} 1 ${valEnd.x} ${valEnd.y}` : "";
  // Needle
  const needleAngle = startAngle - (pct / 100) * sweep;
  const needleLen = r * 0.75;
  const needleTip = { x: cx + needleLen * Math.cos(toRad(needleAngle)), y: cy - needleLen * Math.sin(toRad(needleAngle)) };

  // Calculate SVG height to include the full arc endpoints + strokeWidth + label below
  const svgH = size * 0.78;

  return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", flex: 1 }}>
      <svg width={size} height={svgH} viewBox={`0 0 ${size} ${svgH}`}>
        {/* Background track */}
        <path d={bgPath} fill="none" stroke="var(--bg-primary)" strokeWidth={strokeW} strokeLinecap="round" />
        {/* Filled arc */}
        {pct > 0 && <path d={valPath} fill="none" stroke={fillColor} strokeWidth={strokeW} strokeLinecap="round" style={{ transition: "stroke-dashoffset 0.5s, stroke 0.3s" }} />}
        {/* Needle */}
        <line x1={cx} y1={cy} x2={needleTip.x} y2={needleTip.y}
          stroke="#2d2355" strokeWidth={1.5} strokeLinecap="round"
          style={{ transition: "x2 0.5s, y2 0.5s" }} />
        <circle cx={cx} cy={cy} r={size * 0.03} fill="#2d2355" />
        {/* Center value */}
        <text x={cx} y={cy - size * 0.06} textAnchor="middle" fill={fillColor} fontSize={size * 0.18} fontWeight="bold">{Math.round(pct)}%</text>
        {/* Label */}
        <text x={cx} y={cy + size * 0.15} textAnchor="middle" fill="var(--text-muted)" fontSize={size * 0.09}>{label}</text>
      </svg>
      <div style={{ fontSize: 11, color: "var(--text-secondary)", marginTop: 4 }}>{value}{unit || ""} / {max}{unit || ""}</div>
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
  const [metrics, setMetrics] = useState<any>(null);
  const [nodeMetrics, setNodeMetrics] = useState<NodeMetricsEntry[]>([]);
  const [error, setError] = useState(false);

  useEffect(() => {
    const load = () => {
      getSystemMetrics().then(setMetrics).catch(() => setError(true));
      // Node metrics are optional — no error state if none are registered.
      getNodeMetrics().then(r => setNodeMetrics(r.nodes || [])).catch(() => setNodeMetrics([]));
    };
    load();
    const interval = setInterval(load, 3000); // Refresh every 3 seconds
    return () => clearInterval(interval);
  }, []);

  if (error && !metrics) {
    return (
      <div style={{ padding: 40, textAlign: "center", color: "var(--text-muted)" }}>
        Failed to load system metrics
      </div>
    );
  }

  if (!metrics) {
    return (
      <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: 60, gap: 12 }}>
        <div className="spinner" />
        <span style={{ color: "var(--text-muted)" }}>Loading metrics...</span>
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
      <h2 style={{ color: "var(--text-primary)", fontSize: 20, marginBottom: 20 }}>System Monitor</h2>

      {/* Top row: key numbers */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12, marginBottom: 12 }}>
        {gpu && (
          <div style={{ background: "var(--bg-card)", padding: 14, borderRadius: 6, textAlign: "center" }}>
            <div style={{ fontSize: 24, fontWeight: "bold", color: gpu.gpu_util > 80 ? "#e94560" : gpu.gpu_util > 50 ? "#ffa94d" : "var(--success)" }}>{gpu.gpu_util}%</div>
            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>GPU</div>
          </div>
        )}
        <div style={{ background: "var(--bg-card)", padding: 14, borderRadius: 6, textAlign: "center" }}>
          <div style={{ fontSize: 24, fontWeight: "bold", color: cpu.cpu_percent > 80 ? "#e94560" : cpu.cpu_percent > 50 ? "#ffa94d" : "var(--success)" }}>{cpu.cpu_percent}%</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)" }}>CPU</div>
        </div>
        <div style={{ background: "var(--bg-card)", padding: 14, borderRadius: 6, textAlign: "center" }}>
          <div style={{ fontSize: 24, fontWeight: "bold", color: memory.ram_percent > 85 ? "#e94560" : memory.ram_percent > 60 ? "#ffa94d" : "var(--success)" }}>{memory.ram_percent}%</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)" }}>RAM</div>
        </div>
        <div style={{ background: "var(--bg-card)", padding: 14, borderRadius: 6, textAlign: "center" }}>
          <div style={{ fontSize: 24, fontWeight: "bold", color: "var(--accent)" }}>{fmtNum(shrinkerr?.running_jobs)}</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Encoding Jobs</div>
        </div>
        <div style={{ background: "var(--bg-card)", padding: 14, borderRadius: 6, textAlign: "center" }}>
          <div style={{ fontSize: 24, fontWeight: "bold", color: "var(--success)" }}>{fmtNum(plex?.total)}</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Plex Streams</div>
        </div>
        {shrinkerr?.avg_fps > 0 && (
          <div style={{ background: "var(--bg-card)", padding: 14, borderRadius: 6, textAlign: "center" }}>
            <div style={{ fontSize: 24, fontWeight: "bold", color: "var(--text-primary)" }}>{shrinkerr.avg_fps}</div>
            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Encoding FPS</div>
          </div>
        )}
      </div>

      {/* Detail cards */}
      <div className="monitor-detail-grid" style={{ display: "grid", gap: 12 }}>
        {/* GPU — present only when nvidia-smi returned something real. The
            capability strip under the gauges confirms whether NVENC itself
            is actually working (vs. GPU visible but driver too old, etc.). */}
        {gpu && (
          <MetricCard title={`GPU — ${gpu.name}`}>
            <div style={{ display: "flex", justifyContent: "space-evenly", margin: "8px 0 12px" }}>
              <Gauge value={gpu.gpu_util} max={100} label="Utilization" unit="%" size={110} color="var(--accent)" />
              <Gauge value={Math.round(gpu.memory_used_mb)} max={Math.round(gpu.memory_total_mb)} label="VRAM" unit=" MB" size={110} color="#74c0fc" />
              <Gauge value={Math.round(gpu.power_draw_w)} max={Math.round(gpu.power_limit_w)} label="Power" unit="W" size={110} color="#ffa94d" />
            </div>
            <div style={{ marginTop: 8 }}>
              <StatRow label="Temperature" value={`${gpu.temperature_c}°C`} color={gpu.temperature_c > 85 ? "#e94560" : gpu.temperature_c > 70 ? "#ffa94d" : "var(--success)"} />
              {gpu.encoder_util != null && <StatRow label="NVENC (Encoder)" value={`${gpu.encoder_util}%`} color="#74c0fc" />}
              {gpu.decoder_util != null && <StatRow label="NVDEC (Decoder)" value={`${gpu.decoder_util}%`} color="#69db7c" />}
            </div>
            {localNode && <NodeEncodingStatus entry={localNode} />}
          </MetricCard>
        )}

        {/* No-GPU host — still show encoding capability so the user knows
            libx265 (CPU) is what they're using and why. Avoids the old
            "is it broken or is it just CPU-only?" ambiguity. */}
        {!gpu && localNode && (
          <MetricCard title="Encoding Capability">
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 6 }}>
              No NVIDIA GPU detected on this host — Shrinkerr will use CPU encoding (libx265).
            </div>
            <NodeEncodingStatus entry={localNode} />
          </MetricCard>
        )}

        {/* CPU & Memory */}
        <MetricCard title={`CPU — ${cpu.cpu_count} cores`}>
          <div style={{ display: "flex", justifyContent: "space-evenly", margin: "8px 0 12px" }}>
            <Gauge value={cpu.cpu_percent} max={100} label="CPU" unit="%" size={110} />
            <Gauge value={memory.ram_used_gb} max={memory.ram_total_gb} label="RAM" unit=" GB" size={110} color="#74c0fc" />
            <Gauge value={memory.swap_used_gb || 0} max={memory.ram_total_gb} label="Swap" unit=" GB" size={110} color={memory.swap_percent > 50 ? "#e94560" : "#ffa94d"} />
          </div>
          <div style={{ marginTop: 8 }}>
            <StatRow label="Load Average" value={cpu.load_avg.map((l: number) => l.toFixed(2)).join(" / ")} />
            {cpu.cpu_freq_mhz && <StatRow label="Frequency" value={`${cpu.cpu_freq_mhz} MHz`} />}
          </div>
        </MetricCard>

        {/* Disk I/O */}
        <MetricCard title="Disk I/O">
          <div style={{ display: "flex", gap: 24 }}>
            <div style={{ flex: 1, textAlign: "center" }}>
              <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--accent)" }}>{disk_io.read_mbps}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Read MB/s</div>
            </div>
            <div style={{ flex: 1, textAlign: "center" }}>
              <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--success)" }}>{disk_io.write_mbps}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Write MB/s</div>
            </div>
          </div>
        </MetricCard>

        {/* Plex Streams */}
        <MetricCard title="Plex Streams">
          <div style={{ display: "flex", gap: 24, marginBottom: plex?.sessions?.length > 0 ? 10 : 0 }}>
            <div style={{ flex: 1, textAlign: "center" }}>
              <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--success)" }}>{fmtNum(plex?.total)}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Total Streams</div>
            </div>
            <div style={{ flex: 1, textAlign: "center" }}>
              <div style={{ fontSize: 28, fontWeight: "bold", color: "#ffa94d" }}>{fmtNum(plex?.transcoding)}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Transcoding</div>
            </div>
            <div style={{ flex: 1, textAlign: "center" }}>
              <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--accent)" }}>{fmtNum(plex?.direct)}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Direct Play</div>
            </div>
          </div>
          {plex?.sessions?.length > 0 && (
            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 8 }}>
              {plex.sessions.map((s: any, i: number) => (
                <div key={i} style={{ display: "flex", justifyContent: "space-between", fontSize: 11, marginBottom: 3 }}>
                  <span style={{ color: "var(--text-muted)" }}>{s.user}: {s.title}</span>
                  <span style={{ color: s.is_transcoding ? "#ffa94d" : "var(--success)", fontWeight: 500 }}>
                    {s.is_transcoding ? "Transcoding" : "Direct"}
                  </span>
                </div>
              ))}
            </div>
          )}
        </MetricCard>

        {/* Shrinkerr Workload — spans full width */}
        <div style={{ gridColumn: "1 / -1" }}>
          <MetricCard title="Shrinkerr Workload">
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
              <div style={{ flex: 1, textAlign: "center", minWidth: 80 }}>
                <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--accent)" }}>{fmtNum(shrinkerr?.running_jobs)}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Running</div>
              </div>
              <div style={{ flex: 1, textAlign: "center", minWidth: 80 }}>
                <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--text-muted)" }}>{fmtNum(shrinkerr?.pending_jobs)}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Pending</div>
              </div>
              <div style={{ flex: 1, textAlign: "center", minWidth: 80 }}>
                <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--success)" }}>{fmtNum(shrinkerr?.completed_jobs)}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Completed</div>
              </div>
              {(shrinkerr?.failed_jobs || 0) > 0 && (
                <div style={{ flex: 1, textAlign: "center", minWidth: 80 }}>
                  <div style={{ fontSize: 28, fontWeight: "bold", color: "#e94560" }}>{fmtNum(shrinkerr.failed_jobs)}</div>
                  <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Failed</div>
                </div>
              )}
              <div style={{ flex: 1, textAlign: "center", minWidth: 80 }}>
                <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--text-primary)" }}>{shrinkerr?.avg_fps || 0}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Live FPS</div>
              </div>
              <div style={{ flex: 1, textAlign: "center", minWidth: 80 }}>
                <div style={{ fontSize: 28, fontWeight: "bold", color: "#74c0fc" }}>{shrinkerr?.lifetime_avg_fps || 0}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Avg FPS</div>
              </div>
              <div style={{ flex: 1, textAlign: "center", minWidth: 80 }}>
                <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--accent)" }}>
                  {shrinkerr?.total_saved ? `${(shrinkerr.total_saved / (1024**4)).toFixed(1)} TB` : "0"}
                </div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Total Saved</div>
              </div>
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
              Worker Nodes
              <span style={{ fontSize: 12, color: "var(--text-muted)", fontWeight: 400 }}>
                {reporting} of {remote.length} reporting
              </span>
            </h3>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 12 }}>
              {remote.map(n => <NodeMetricCard key={n.node_id} entry={n} />)}
            </div>
          </>
        );
      })()}

      <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 12, textAlign: "center", opacity: 0.5 }}>
        Auto-refreshing every 3 seconds
      </div>
    </div>
  );
}


// Single worker-node card. Uses the same Gauge/StatRow building blocks as
// the server cards above so the Monitor page stays visually consistent.
function NodeMetricCard({ entry }: { entry: NodeMetricsEntry }) {
  const m = entry.metrics;
  const stale = m && entry.age_seconds !== null && (entry.age_seconds ?? 0) > 15;

  // Status badge color
  const statusColor = entry.status === "working" ? "var(--accent)"
    : entry.status === "online" ? "var(--success)"
    : entry.status === "error" ? "#e94560"
    : "var(--text-muted)";

  return (
    <div style={{ background: "var(--bg-card)", padding: 14, borderRadius: 6, border: entry.status === "working" ? "1px solid var(--accent)" : "1px solid transparent" }}>
      {/* Header: name + status + hostname */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
        <span style={{
          width: 8, height: 8, borderRadius: "50%", background: statusColor,
          boxShadow: entry.status === "working" ? `0 0 6px ${statusColor}` : "none",
          flexShrink: 0,
        }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {entry.name}
          </div>
          <div style={{ fontSize: 10, color: "var(--text-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {entry.hostname || entry.node_id}
            {entry.current_job_id != null && <> · job #{entry.current_job_id}</>}
          </div>
        </div>
        {stale && (
          <span style={{ fontSize: 9, padding: "2px 6px", borderRadius: 3, background: "rgba(231,76,60,0.15)", color: "#e94560" }}>
            {Math.round(entry.age_seconds ?? 0)}s stale
          </span>
        )}
        {!m && (
          <span style={{ fontSize: 10, color: "var(--text-muted)", fontStyle: "italic" }}>
            no metrics yet
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
          <div style={{ display: "flex", justifyContent: "space-evenly", margin: "4px 0 10px" }}>
            <Gauge value={m.cpu.cpu_percent} max={100} label="CPU" unit="%" size={88} />
            <Gauge value={m.memory.ram_used_gb} max={m.memory.ram_total_gb} label="RAM" unit=" GB" size={88} color="#74c0fc" />
            {m.gpu && (
              <Gauge value={m.gpu.gpu_util} max={100} label="GPU" unit="%" size={88} color="var(--accent)" />
            )}
          </div>

          {/* GPU details */}
          {m.gpu && (
            <div style={{ marginTop: 6, paddingTop: 6, borderTop: "1px solid var(--border)" }}>
              <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 4 }}>{m.gpu.name}</div>
              <StatRow label="VRAM" value={`${Math.round(m.gpu.memory_used_mb)} / ${Math.round(m.gpu.memory_total_mb)} MB`} color="#74c0fc" />
              <StatRow label="Temp" value={`${m.gpu.temperature_c}°C`} color={m.gpu.temperature_c > 85 ? "#e94560" : m.gpu.temperature_c > 70 ? "#ffa94d" : "var(--success)"} />
              <StatRow label="Power" value={`${Math.round(m.gpu.power_draw_w)} / ${Math.round(m.gpu.power_limit_w)} W`} color="#ffa94d" />
              {m.gpu.encoder_util != null && (
                <StatRow label="NVENC" value={`${m.gpu.encoder_util}%`} color="#74c0fc" />
              )}
              {m.gpu.decoder_util != null && (
                <StatRow label="NVDEC" value={`${m.gpu.decoder_util}%`} color="#69db7c" />
              )}
            </div>
          )}

          {/* CPU details if no GPU (keep card balanced) */}
          {!m.gpu && (
            <div style={{ marginTop: 6, paddingTop: 6, borderTop: "1px solid var(--border)" }}>
              <StatRow label="Cores" value={m.cpu.cpu_count} />
              <StatRow label="Load" value={m.cpu.load_avg.map(l => l.toFixed(2)).join(" / ")} />
              {m.cpu.cpu_freq_mhz && <StatRow label="Frequency" value={`${m.cpu.cpu_freq_mhz} MHz`} />}
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
  const hasNvenc = entry.capabilities.includes("nvenc");
  const hasLibx265 = entry.capabilities.includes("libx265");
  const reason = entry.nvenc_unavailable_reason;
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
          <span title={reason} style={{ color: "#ffa94d", display: "inline-flex", alignItems: "center", gap: 4 }}>
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
              <line x1="12" y1="9" x2="12" y2="13"/>
              <line x1="12" y1="17" x2="12.01" y2="17"/>
            </svg>
            NVENC off
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
          <span style={{ marginLeft: "auto", opacity: 0.7 }}>
            driver {driver}
          </span>
        )}
      </div>
      {/* Full reason text for NVENC-off nodes. Wrap so long strings don't
          blow out the card layout; keep it terse so it stays on one line
          when possible. */}
      {!hasNvenc && reason && (
        <div style={{ marginTop: 4, fontSize: 10, color: "#ffa94d", wordBreak: "break-word", lineHeight: 1.4 }}>
          {reason}
        </div>
      )}
    </div>
  );
}
