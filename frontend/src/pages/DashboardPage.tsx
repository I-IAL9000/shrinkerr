import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { getDashboardData, getStatsTimeline, dismissSetup } from "../api";
import { LineChart, Line, AreaChart, Area, BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import type { JobProgress } from "../types";

const cardStyle: React.CSSProperties = { background: "var(--bg-card)", padding: 20, borderRadius: 6 };
const chartCardStyle: React.CSSProperties = { ...cardStyle, minHeight: 250 };

function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 4) return `${(bytes / (1024 ** 4)).toFixed(2)} TB`;
  if (bytes >= 1024 ** 3) return `${(bytes / (1024 ** 3)).toFixed(1)} GB`;
  return `${(bytes / (1024 ** 2)).toFixed(0)} MB`;
}

// Donut chart matching the stats page style
function Donut({ segments, size = 130, hole = 0.6 }: {
  segments: { value: number; color: string; label: string }[];
  size?: number; hole?: number;
}) {
  const total = segments.reduce((s, seg) => s + seg.value, 0);
  if (total === 0) return null;
  let cumDeg = 0;
  const gradientStops = segments.map(seg => {
    const start = cumDeg;
    cumDeg += (seg.value / total) * 360;
    return `${seg.color} ${start}deg ${cumDeg}deg`;
  }).join(", ");

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 24 }}>
      <div style={{
        width: size, height: size, borderRadius: "50%",
        background: `conic-gradient(${gradientStops})`,
        display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
      }}>
        <div style={{
          width: size * hole, height: size * hole, borderRadius: "50%",
          background: "var(--bg-card)", display: "flex", alignItems: "center", justifyContent: "center",
        }}>
          <span style={{ fontSize: 13, fontWeight: "bold", color: "var(--text-primary)" }}>{total.toLocaleString()}</span>
        </div>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {segments.filter(s => s.value > 0).map(seg => (
          <div key={seg.label} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
            <div style={{ width: 10, height: 10, borderRadius: 2, background: seg.color, flexShrink: 0 }} />
            <span style={{ color: "var(--text-muted)" }}>{seg.label}: <b style={{ color: "var(--text-secondary)" }}>{seg.value.toLocaleString()}</b> <span style={{ opacity: 0.5 }}>({(seg.value / total * 100).toFixed(1)}%)</span></span>
          </div>
        ))}
      </div>
    </div>
  );
}

// Mini progress bar for active jobs
function MiniProgress({ progress }: { progress: number }) {
  return (
    <div style={{ height: 4, background: "var(--bg-primary)", borderRadius: 2, overflow: "hidden", flex: 1 }}>
      <div style={{
        height: "100%", borderRadius: 2,
        width: `${Math.min(100, progress)}%`,
        background: "linear-gradient(90deg, var(--accent), #40ceff)",
        transition: "width 0.5s",
      }} />
    </div>
  );
}

// --- Setup Wizard ---

function SetupWizard({ setup, onDismiss }: { setup: any; onDismiss: () => void }) {
  const navigate = useNavigate();
  const steps = [
    {
      key: "dirs",
      title: "Add media directories",
      description: "Tell Squeezarr where your media files are stored so it can scan them.",
      done: setup.has_dirs,
      action: () => navigate("/settings"),
      actionLabel: "Go to Settings",
      icon: (
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/>
        </svg>
      ),
    },
    {
      key: "scan",
      title: "Scan your library",
      description: setup.scan_count > 0
        ? `${setup.scan_count.toLocaleString()} files scanned. Run another scan to find new files.`
        : "Scan your media directories to find files that can be optimized.",
      done: setup.scan_count > 0,
      action: () => navigate("/scanner"),
      actionLabel: setup.scan_count > 0 ? "Open Scanner" : "Start Scanning",
      icon: (
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
        </svg>
      ),
    },
    {
      key: "plex",
      title: "Connect Plex (optional)",
      description: "Link your Plex server to enable label-based rules, automatic library scans, and trash cleanup.",
      done: setup.has_plex,
      action: () => navigate("/settings"),
      actionLabel: "Configure Plex",
      optional: true,
      icon: (
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>
        </svg>
      ),
    },
    {
      key: "queue",
      title: "Start converting",
      description: "Add files to the queue and start converting to save disk space.",
      done: setup.has_jobs,
      action: () => navigate("/queue"),
      actionLabel: "Open Queue",
      icon: (
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polygon points="5 3 19 12 5 21"/>
        </svg>
      ),
    },
  ];

  const requiredDone = steps.filter(s => s.done && !s.optional).length;
  const requiredTotal = steps.filter(s => !s.optional).length;

  return (
    <div>
      <div style={{ textAlign: "center", padding: "40px 20px 20px" }}>
        <img src="/squeezarr-logo.svg" alt="" width="48" height="48" style={{ marginBottom: 16 }} />
        <h1 style={{
          fontSize: 28, fontWeight: "bold", margin: "0 0 8px",
          background: "linear-gradient(90deg, #9135ff, #5089F7)",
          WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent", backgroundClip: "text",
        }}>
          Welcome to Squeezarr
        </h1>
        <p style={{ color: "var(--text-muted)", fontSize: 14, margin: 0, maxWidth: 500, marginInline: "auto" }}>
          Convert your media library from x264 to x265 with NVENC hardware encoding.
          Follow these steps to get started.
        </p>
      </div>

      {/* Progress indicator */}
      <div style={{ display: "flex", justifyContent: "center", gap: 6, margin: "24px 0" }}>
        {steps.map((step) => (
          <div key={step.key} style={{
            width: 40, height: 4, borderRadius: 2,
            background: step.done ? "var(--accent)" : "var(--border)",
            transition: "background 0.3s",
          }} />
        ))}
      </div>

      {/* Steps */}
      <div style={{ display: "flex", flexDirection: "column", gap: 10, maxWidth: 600, margin: "0 auto", padding: "0 20px" }}>
        {steps.map((step, i) => (
          <div key={step.key} style={{
            background: "var(--bg-card)", borderRadius: 8, padding: "16px 20px",
            display: "flex", alignItems: "center", gap: 16,
            border: step.done ? "1px solid rgba(145,53,255,0.2)" : "1px solid var(--border)",
            opacity: step.done ? 0.6 : 1,
          }}>
            {/* Step number / checkmark */}
            <div style={{
              width: 36, height: 36, borderRadius: "50%", flexShrink: 0,
              display: "flex", alignItems: "center", justifyContent: "center",
              background: step.done ? "rgba(24,255,165,0.15)" : "rgba(145,53,255,0.15)",
              color: step.done ? "#18ffa5" : "var(--accent)",
              fontSize: 14, fontWeight: "bold",
            }}>
              {step.done ? (
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="20 6 9 17 4 12"/>
                </svg>
              ) : (
                <span style={{ opacity: 0.7 }}>{step.icon}</span>
              )}
            </div>

            {/* Content */}
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)", display: "flex", alignItems: "center", gap: 8 }}>
                {step.title}
                {step.optional && <span style={{ fontSize: 10, color: "var(--text-muted)", fontWeight: "normal" }}>optional</span>}
              </div>
              <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>{step.description}</div>
            </div>

            {/* Action button */}
            {!step.done && (
              <button
                className={i === steps.findIndex(s => !s.done) ? "btn btn-primary" : "btn btn-secondary"}
                style={{ fontSize: 12, padding: "6px 14px", whiteSpace: "nowrap", flexShrink: 0 }}
                onClick={step.action}
              >
                {step.actionLabel}
              </button>
            )}
          </div>
        ))}
      </div>

      {/* Skip / dismiss */}
      <div style={{ textAlign: "center", marginTop: 24, paddingBottom: 20 }}>
        {requiredDone >= requiredTotal ? (
          <button className="btn btn-primary" style={{ padding: "8px 24px" }}
            onClick={onDismiss}
          >
            Go to Dashboard
          </button>
        ) : (
          <button style={{
            background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer",
            fontSize: 12, padding: "8px 16px",
          }}
            onClick={onDismiss}
          >
            Skip setup — I know what I'm doing
          </button>
        )}
      </div>
    </div>
  );
}

// --- Dashboard ---

interface DashboardPageProps {
  jobProgressMap: Map<number, JobProgress>;
}

export default function DashboardPage({ jobProgressMap }: DashboardPageProps) {
  const [dash, setDash] = useState<any>(null);
  const [timeline, setTimeline] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([getDashboardData(), getStatsTimeline(30)]).then(([d, t]) => {
      setDash(d);
      setTimeline(t.days || []);
      setLoading(false);
    }).catch(() => setLoading(false));
  }, []);

  useEffect(() => {
    const interval = setInterval(() => {
      getDashboardData().then(setDash).catch(() => {});
    }, 10000);
    return () => clearInterval(interval);
  }, []);

  if (loading || !dash) {
    return (
      <div>
        <h2 style={{ color: "var(--text-primary)", fontSize: 20, marginBottom: 20 }}>Dashboard</h2>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", padding: 60 }}>
          <div className="spinner" />
          <div style={{ marginTop: 12, fontSize: 13, opacity: 0.5 }}>Loading dashboard...</div>
        </div>
      </div>
    );
  }

  // Show setup wizard for fresh installs (add ?setup to URL to force preview)
  const setup = dash.setup;
  const forceSetup = window.location.search.includes("setup");
  const showWizard = forceSetup || (setup && !setup.dismissed && (!setup.has_dirs || setup.scan_count === 0));
  if (showWizard) {
    return <SetupWizard setup={setup} onDismiss={async () => {
      await dismissSetup();
      // Refresh dashboard to hide wizard
      const d = await getDashboardData();
      setDash(d);
    }} />;
  }

  const activeJobs = dash.running_jobs || [];
  const liveJobs = activeJobs.map((j: any) => {
    const ws = jobProgressMap.get(j.id);
    return { ...j, progress: ws?.progress ?? j.progress, fps: ws?.fps ?? j.fps };
  });
  const combinedFps = liveJobs.reduce((s: number, j: any) => s + (j.fps || 0), 0);

  const diskColor = (free: number) => {
    const gb = free / (1024 ** 3);
    if (gb > 100) return "#18ffa5";
    if (gb > 50) return "#ffa94d";
    return "#e94560";
  };

  const totalFree = dash.total_free || 0;

  const chartData = timeline.map((d: any) => ({
    ...d,
    date: d.date.slice(5),
    // Use null for 0 fps so the line chart skips days with no encoding (connectNulls bridges gaps)
    avg_fps: d.avg_fps > 0 ? Math.round(d.avg_fps) : null,
    saved_gb: +(d.space_saved / (1024 ** 3)).toFixed(1),
    cumulative_tb: +(d.cumulative_saved / (1024 ** 4)).toFixed(2),
  }));

  const codecData = [
    { label: "H.264", value: dash.codecs?.x264 || 0, color: "#e94560" },
    { label: "H.265", value: dash.codecs?.x265 || 0, color: "#9135ff" },
    { label: "AV1", value: dash.codecs?.av1 || 0, color: "#40ceff" },
    { label: "Other", value: dash.codecs?.other || 0, color: "#18ffa5" },
  ].filter(d => d.value > 0);

  const tooltipStyle = {
    contentStyle: { background: "#1a1030", border: "1px solid var(--border)", borderRadius: 6, fontSize: 12 },
    labelStyle: { color: "var(--text-muted)" },
    cursor: { fill: "rgba(145,53,255,0.15)" },
  };

  const today = dash.today || {};

  return (
    <div>
      <h2 style={{ color: "var(--text-primary)", fontSize: 20, marginBottom: 20 }}>Dashboard</h2>

      {/* Status cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12, marginBottom: 16 }}>
        {/* Converting */}
        <div style={cardStyle}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: liveJobs.length > 0 ? 10 : 0 }}>
            <div>
              <span style={{ fontSize: 28, fontWeight: "bold", color: liveJobs.length > 0 ? "var(--accent)" : "var(--text-muted)" }}>
                {liveJobs.length > 0 ? liveJobs.length : "Idle"}
              </span>
              <span style={{ fontSize: 11, color: "var(--text-muted)", marginLeft: 8 }}>
                {liveJobs.length > 0 ? "Converting" : "No active jobs"}
              </span>
            </div>
            {combinedFps > 0 && (
              <span style={{ fontSize: 13, color: "#40ceff", fontWeight: 600 }}>{combinedFps.toFixed(0)} fps combined</span>
            )}
          </div>
          {liveJobs.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
              {liveJobs.map((j: any) => {
                const shortName = j.file_name.length > 55 ? j.file_name.slice(0, 52) + "..." : j.file_name;
                return (
                  <div key={j.id}>
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, marginBottom: 2 }}>
                      <span style={{ color: "var(--text-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{shortName}</span>
                      <span style={{ color: "var(--success)", flexShrink: 0, marginLeft: 8 }}>
                        {j.progress.toFixed(0)}%{j.fps ? ` ${j.fps.toFixed(0)}fps` : ""}
                      </span>
                    </div>
                    <MiniProgress progress={j.progress} />
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Queue depth */}
        <div style={cardStyle}>
          <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--text-primary)" }}>{dash.queue?.pending || 0}</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>Pending in queue</div>
          {(dash.queue?.failed || 0) > 0 && (
            <div style={{ fontSize: 12, color: "#e94560", marginTop: 6 }}>{dash.queue.failed} failed</div>
          )}
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 6 }}>{dash.queue?.completed?.toLocaleString() || 0} completed</div>
        </div>

        {/* Total saved */}
        <div style={cardStyle}>
          <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--accent)" }}>{formatBytes(dash.total_saved || 0)}</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>Total space saved</div>
          {dash.bandwidth_pct > 0 && (
            <div style={{ fontSize: 12, color: "var(--success)", marginTop: 6 }}>{dash.bandwidth_pct}% smaller files</div>
          )}
        </div>

        {/* Disk space */}
        <div style={cardStyle}>
          <div style={{ fontSize: 28, fontWeight: "bold", color: diskColor(totalFree) }}>
            {formatBytes(totalFree)}
          </div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>Total free disk space</div>
          {(dash.disk || []).length > 0 && (
            <div style={{ marginTop: 8, fontSize: 11 }}>
              {(dash.disk || []).map((d: any, i: number) => (
                <div key={i} style={{ display: "flex", justifyContent: "space-between", color: diskColor(d.free), marginTop: 2 }}>
                  <span>{d.label}</span>
                  <span>{formatBytes(d.free)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Today's summary */}
      <div style={{ ...cardStyle, display: "flex", gap: 28, marginBottom: 16, padding: "12px 20px", flexWrap: "wrap" }}>
        <span style={{ fontSize: 12, color: "var(--text-muted)", fontWeight: 600 }}>Today</span>
        <span style={{ fontSize: 12 }}><b style={{ color: "var(--accent)" }}>{today.jobs_completed || 0}</b> <span style={{ color: "var(--text-muted)" }}>jobs</span></span>
        <span style={{ fontSize: 12 }}><b style={{ color: "var(--success)" }}>{formatBytes(today.space_saved || 0)}</b> <span style={{ color: "var(--text-muted)" }}>saved</span></span>
        {(today.avg_fps || 0) > 0 && (
          <span style={{ fontSize: 12 }}><b style={{ color: "#40ceff" }}>{today.avg_fps.toFixed(0)}</b> <span style={{ color: "var(--text-muted)" }}>avg fps/job</span></span>
        )}
        {combinedFps > 0 && (
          <span style={{ fontSize: 12 }}><b style={{ color: "#40ceff" }}>{combinedFps.toFixed(0)}</b> <span style={{ color: "var(--text-muted)" }}>combined fps</span></span>
        )}
        {(today.original_size || 0) > 0 && (today.space_saved || 0) > 0 && (
          <span style={{ fontSize: 12 }}><b style={{ color: "var(--success)" }}>{((today.space_saved / today.original_size) * 100).toFixed(0)}%</b> <span style={{ color: "var(--text-muted)" }}>avg reduction</span></span>
        )}
      </div>

      {/* Charts row 1 */}
      {chartData.length > 1 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(400px, 1fr))", gap: 16, marginBottom: 16 }}>
          <div style={chartCardStyle}>
            <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Avg FPS per Job (30 days)</h3>
            <ResponsiveContainer width="100%" height={180}>
              <LineChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                <XAxis dataKey="date" tick={{ fill: "var(--text-muted)", fontSize: 10 }} />
                <YAxis tick={{ fill: "var(--text-muted)", fontSize: 10 }} />
                <Tooltip {...tooltipStyle} formatter={(v: any) => [`${Math.round(v)} fps`, "Avg FPS/Job"]} />
                <Line type="monotone" dataKey="avg_fps" stroke="#40ceff" dot={false} strokeWidth={2} name="Avg FPS/Job" connectNulls />
              </LineChart>
            </ResponsiveContainer>
          </div>

          <div style={chartCardStyle}>
            <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Cumulative Savings</h3>
            <ResponsiveContainer width="100%" height={180}>
              <AreaChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                <XAxis dataKey="date" tick={{ fill: "var(--text-muted)", fontSize: 10 }} />
                <YAxis tick={{ fill: "var(--text-muted)", fontSize: 10 }} unit=" TB" />
                <Tooltip {...tooltipStyle} />
                <Area type="monotone" dataKey="cumulative_tb" stroke="#9135ff" fill="rgba(145,53,255,0.2)" strokeWidth={2} name="TB Saved" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* Charts row 2 */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(400px, 1fr))", gap: 16, marginBottom: 16 }}>
        {chartData.length > 1 && (
          <div style={chartCardStyle}>
            <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Daily Jobs Completed</h3>
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                <XAxis dataKey="date" tick={{ fill: "var(--text-muted)", fontSize: 10 }} />
                <YAxis tick={{ fill: "var(--text-muted)", fontSize: 10 }} />
                <Tooltip {...tooltipStyle} cursor={{ fill: "rgba(145,53,255,0.1)" }} />
                <Bar dataKey="jobs_completed" fill="#9135ff" radius={[3, 3, 0, 0]} name="Jobs"
                  onMouseOver={() => {}} /* prevent default hover */
                />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}

        {codecData.length > 0 && (
          <div style={chartCardStyle}>
            <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Library Codec Composition</h3>
            <Donut segments={codecData} size={140} />
          </div>
        )}
      </div>

      {/* Storage projection */}
      {dash.projection && dash.projection.projected_days > 0 && (
        <div style={{ ...cardStyle, display: "flex", gap: 24, alignItems: "center", marginBottom: 16 }}>
          <div style={{ textAlign: "center", minWidth: 100 }}>
            <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--accent)" }}>~{formatBytes(dash.projection.projected_savings)}</div>
            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>projected savings</div>
          </div>
          <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.8 }}>
            At your current rate of <b style={{ color: "var(--accent)" }}>{formatBytes(dash.projection.avg_daily_savings)}/day</b> ({dash.projection.avg_jobs_per_day} jobs/day),
            converting the remaining <b>{dash.projection.remaining_files.toLocaleString()}</b> files
            ({formatBytes(dash.projection.remaining_size)}) will take approximately <b style={{ color: "var(--success)" }}>{
              dash.projection.projected_days > 365
                ? `${(dash.projection.projected_days / 365).toFixed(1)} years`
                : dash.projection.projected_days > 30
                ? `${(dash.projection.projected_days / 30).toFixed(1)} months`
                : `${dash.projection.projected_days} days`
            }</b>.
          </div>
        </div>
      )}

      {/* Bandwidth savings */}
      {dash.bandwidth_pct > 0 && (
        <div style={{ ...cardStyle, display: "flex", gap: 24, alignItems: "center" }}>
          <div style={{ textAlign: "center", minWidth: 90 }}>
            <div style={{ fontSize: 32, fontWeight: "bold", color: "var(--success)" }}>{dash.bandwidth_pct}%</div>
            <div style={{ fontSize: 11, color: "var(--text-muted)" }}>avg reduction</div>
          </div>
          <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.7 }}>
            Converted files use {dash.bandwidth_pct}% less streaming bandwidth per viewer.
            For remote Plex streaming this means fewer buffering issues and lower upload usage.
          </div>
        </div>
      )}
    </div>
  );
}
