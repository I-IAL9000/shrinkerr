import { useState, useEffect } from "react";
import { getSystemMetrics } from "../api";

function MetricBar({ label, value, max, unit, color }: { label: string; value: number; max: number; unit?: string; color?: string }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 3 }}>
        <span style={{ color: "var(--text-muted)" }}>{label}</span>
        <span style={{ color: "var(--text-secondary)", fontWeight: 600 }}>{value}{unit || ""} / {max}{unit || ""}</span>
      </div>
      <div style={{ height: 6, background: "var(--bg-primary)", borderRadius: 3, overflow: "hidden" }}>
        <div style={{
          height: "100%", width: `${pct}%`, borderRadius: 3, transition: "width 0.5s",
          background: pct > 90 ? "#e94560" : pct > 70 ? "#ffa94d" : color || "var(--accent)",
        }} />
      </div>
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

  const { gpu, cpu, memory, disk_io, network, plex, squeezarr } = metrics;

  return (
    <div>
      <h2 style={{ color: "var(--text-primary)", fontSize: 20, marginBottom: 20 }}>System Monitor</h2>

      {/* Top row: key numbers */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12, marginBottom: 16 }}>
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
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        {/* GPU */}
        {gpu && (
          <MetricCard title={`GPU — ${gpu.name}`}>
            <MetricBar label="GPU Utilization" value={gpu.gpu_util} max={100} unit="%" color="var(--accent)" />
            {gpu.encoder_util != null && <MetricBar label="NVENC (Encoder)" value={gpu.encoder_util} max={100} unit="%" color="#74c0fc" />}
            {gpu.decoder_util != null && <MetricBar label="NVDEC (Decoder)" value={gpu.decoder_util} max={100} unit="%" color="#69db7c" />}
            <MetricBar label="VRAM" value={Math.round(gpu.memory_used_mb)} max={Math.round(gpu.memory_total_mb)} unit=" MB" />
            <MetricBar label="Power" value={Math.round(gpu.power_draw_w)} max={Math.round(gpu.power_limit_w)} unit="W" />
            <StatRow label="Temperature" value={`${gpu.temperature_c}°C`} color={gpu.temperature_c > 85 ? "#e94560" : gpu.temperature_c > 70 ? "#ffa94d" : "var(--success)"} />
          </MetricCard>
        )}

        {/* CPU & Memory */}
        <MetricCard title={`CPU — ${cpu.cpu_count} cores`}>
          <MetricBar label="CPU Usage" value={cpu.cpu_percent} max={100} unit="%" />
          <MetricBar label="RAM" value={memory.ram_used_gb} max={memory.ram_total_gb} unit=" GB" />
          {memory.swap_percent > 0 && (
            <MetricBar label="Swap" value={memory.swap_used_gb} max={memory.ram_total_gb} unit=" GB" color="#e94560" />
          )}
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

        {/* Network */}
        <MetricCard title="Network">
          <div style={{ display: "flex", gap: 24 }}>
            <div style={{ flex: 1, textAlign: "center" }}>
              <div style={{ fontSize: 28, fontWeight: "bold", color: "#74c0fc" }}>{network.download_mbps}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Download MB/s</div>
            </div>
            <div style={{ flex: 1, textAlign: "center" }}>
              <div style={{ fontSize: 28, fontWeight: "bold", color: "#ffa94d" }}>{network.upload_mbps}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Upload MB/s</div>
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

        {/* Squeezarr Workload */}
        <MetricCard title="Squeezarr Workload">
          <div style={{ display: "flex", gap: 24 }}>
            <div style={{ flex: 1, textAlign: "center" }}>
              <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--accent)" }}>{squeezarr?.running_jobs || 0}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Running</div>
            </div>
            <div style={{ flex: 1, textAlign: "center" }}>
              <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--text-muted)" }}>{squeezarr?.pending_jobs || 0}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Pending</div>
            </div>
            <div style={{ flex: 1, textAlign: "center" }}>
              <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--text-primary)" }}>{squeezarr?.avg_fps || 0}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>Avg FPS</div>
            </div>
          </div>
        </MetricCard>
      </div>

      <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 12, textAlign: "center", opacity: 0.5 }}>
        Auto-refreshing every 3 seconds
      </div>
    </div>
  );
}
