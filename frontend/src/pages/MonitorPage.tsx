import { useState, useEffect } from "react";
import { getSystemMetrics } from "../api";

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
  const [error, setError] = useState(false);

  useEffect(() => {
    const load = () => {
      getSystemMetrics().then(setMetrics).catch(() => setError(true));
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

  const { gpu, cpu, memory, disk_io, plex, squeezarr } = metrics;

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
          <div style={{ fontSize: 24, fontWeight: "bold", color: "var(--accent)" }}>{squeezarr?.running_jobs || 0}</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Encoding Jobs</div>
        </div>
        <div style={{ background: "var(--bg-card)", padding: 14, borderRadius: 6, textAlign: "center" }}>
          <div style={{ fontSize: 24, fontWeight: "bold", color: "var(--success)" }}>{plex?.total || 0}</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Plex Streams</div>
        </div>
        {squeezarr?.avg_fps > 0 && (
          <div style={{ background: "var(--bg-card)", padding: 14, borderRadius: 6, textAlign: "center" }}>
            <div style={{ fontSize: 24, fontWeight: "bold", color: "var(--text-primary)" }}>{squeezarr.avg_fps}</div>
            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Encoding FPS</div>
          </div>
        )}
      </div>

      {/* Detail cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 12 }}>
        {/* GPU */}
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
              <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--success)" }}>{plex?.total || 0}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Total Streams</div>
            </div>
            <div style={{ flex: 1, textAlign: "center" }}>
              <div style={{ fontSize: 28, fontWeight: "bold", color: "#ffa94d" }}>{plex?.transcoding || 0}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Transcoding</div>
            </div>
            <div style={{ flex: 1, textAlign: "center" }}>
              <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--accent)" }}>{plex?.direct || 0}</div>
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

        {/* Squeezarr Workload — spans full width */}
        <div style={{ gridColumn: "1 / -1" }}>
          <MetricCard title="Squeezarr Workload">
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
              <div style={{ flex: 1, textAlign: "center", minWidth: 80 }}>
                <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--accent)" }}>{squeezarr?.running_jobs || 0}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Running</div>
              </div>
              <div style={{ flex: 1, textAlign: "center", minWidth: 80 }}>
                <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--text-muted)" }}>{squeezarr?.pending_jobs || 0}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Pending</div>
              </div>
              <div style={{ flex: 1, textAlign: "center", minWidth: 80 }}>
                <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--success)" }}>{squeezarr?.completed_jobs || 0}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Completed</div>
              </div>
              {(squeezarr?.failed_jobs || 0) > 0 && (
                <div style={{ flex: 1, textAlign: "center", minWidth: 80 }}>
                  <div style={{ fontSize: 28, fontWeight: "bold", color: "#e94560" }}>{squeezarr.failed_jobs}</div>
                  <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Failed</div>
                </div>
              )}
              <div style={{ flex: 1, textAlign: "center", minWidth: 80 }}>
                <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--text-primary)" }}>{squeezarr?.avg_fps || 0}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Live FPS</div>
              </div>
              <div style={{ flex: 1, textAlign: "center", minWidth: 80 }}>
                <div style={{ fontSize: 28, fontWeight: "bold", color: "#74c0fc" }}>{squeezarr?.lifetime_avg_fps || 0}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Avg FPS</div>
              </div>
              <div style={{ flex: 1, textAlign: "center", minWidth: 80 }}>
                <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--accent)" }}>
                  {squeezarr?.total_saved ? `${(squeezarr.total_saved / (1024**4)).toFixed(1)} TB` : "0"}
                </div>
                <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Total Saved</div>
              </div>
            </div>
          </MetricCard>
        </div>
      </div>

      <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 12, textAlign: "center", opacity: 0.5 }}>
        Auto-refreshing every 3 seconds
      </div>
    </div>
  );
}
