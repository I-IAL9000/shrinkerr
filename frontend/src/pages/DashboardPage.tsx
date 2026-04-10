import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { getDashboardData, getStatsTimeline, getStatsSummary, dismissSetup } from "../api";
import {
  LineChart, Line, AreaChart, Area, BarChart as RBarChart, Bar,
  XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from "recharts";
import type { JobProgress } from "../types";

const cardStyle: React.CSSProperties = { background: "var(--bg-card)", padding: 20, borderRadius: 6 };
const headingStyle: React.CSSProperties = { color: "var(--text-primary)", fontSize: 14, marginBottom: 16 };
const donutColors = ["#9135ff", "#6882ff", "#40ceff", "#2cf4e8", "#18ffa5", "#ff6b9d", "#ffa94d"];

const tooltipStyle = {
  contentStyle: { background: "#1a1030", border: "1px solid var(--border)", borderRadius: 6, fontSize: 12 },
  labelStyle: { color: "var(--text-muted)" },
};

function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 4) return `${(bytes / (1024 ** 4)).toFixed(2)} TB`;
  if (bytes >= 1024 ** 3) return `${(bytes / (1024 ** 3)).toFixed(1)} GB`;
  return `${(bytes / (1024 ** 2)).toFixed(0)} MB`;
}

// Donut chart with optional center text
function Donut({ segments, size = 120, hole = 0.65, centerText }: {
  segments: { value: number; color: string; label: string }[];
  size?: number; hole?: number; centerText?: string;
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
    <div style={{ display: "flex", alignItems: "center", gap: 20 }}>
      <div style={{
        width: size, height: size, borderRadius: "50%",
        background: `conic-gradient(${gradientStops})`,
        display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
      }}>
        <div style={{
          width: size * hole, height: size * hole, borderRadius: "50%",
          background: "var(--bg-card)", display: "flex", alignItems: "center", justifyContent: "center",
        }}>
          {centerText && <span style={{ fontSize: 14, fontWeight: "bold", color: "var(--text-primary)" }}>{centerText}</span>}
        </div>
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {segments.filter(s => s.value > 0).map(seg => (
          <div key={seg.label} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
            <div style={{ width: 10, height: 10, borderRadius: 2, background: seg.color, flexShrink: 0 }} />
            <span style={{ color: "var(--text-muted)" }}>{seg.label}: <b style={{ color: "var(--text-secondary)" }}>{seg.value.toLocaleString()}</b></span>
          </div>
        ))}
      </div>
    </div>
  );
}

// Horizontal bar chart (renamed to avoid conflict with recharts BarChart)
function HBarChart({ items, colors }: { items: { label: string; value: number }[]; colors?: string[] }) {
  const max = Math.max(...items.map(i => i.value), 1);
  const defaultColors = ["#9135ff", "#7c5cff", "#6882ff", "#54a8ff", "#40ceff", "#2cf4e8", "#18ffa5"];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {items.filter(i => i.value > 0).map((item, idx) => (
        <div key={item.label} style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ width: 80, fontSize: 11, color: "var(--text-muted)", textAlign: "right", flexShrink: 0 }}>{item.label}</span>
          <div style={{ flex: 1, height: 18, background: "var(--bg-primary)", borderRadius: 3, overflow: "hidden" }}>
            <div style={{
              height: "100%", width: `${(item.value / max) * 100}%`,
              background: (colors || defaultColors)[idx % (colors || defaultColors).length],
              borderRadius: 3, transition: "width 0.3s",
            }} />
          </div>
          <span style={{ width: 50, fontSize: 11, color: "var(--text-secondary)", fontWeight: "bold", textAlign: "right", flexShrink: 0 }}>{item.value}</span>
        </div>
      ))}
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

// --- Dashboard (merged with Statistics) ---

export default function DashboardPage({ jobProgressMap }: { jobProgressMap: Map<number, JobProgress> }) {
  const [dash, setDash] = useState<any>(null);
  const [stats, setStats] = useState<any>(null);
  const [timeline, setTimeline] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([getDashboardData(), getStatsSummary(), getStatsTimeline(90)]).then(([d, s, t]) => {
      setDash(d);
      setStats(s);
      setTimeline(t.days || []);
      setLoading(false);
    }).catch(() => setLoading(false));
  }, []);

  // Poll dashboard data every 10s
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

  // Show setup wizard for fresh installs
  const setup = dash.setup;
  const forceSetup = window.location.search.includes("setup");
  const showWizard = forceSetup || (setup && !setup.dismissed && (!setup.has_dirs || setup.scan_count === 0));
  if (showWizard) {
    return <SetupWizard setup={setup} onDismiss={async () => {
      await dismissSetup();
      const d = await getDashboardData();
      setDash(d);
    }} />;
  }

  // Live status data
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
  const today = dash.today || {};

  // Stats shortcuts
  const s = stats;
  const totalCompleted = s?.files_processed || 0;
  const hasNoData = totalCompleted === 0 && liveJobs.length === 0 && (dash.queue?.pending || 0) === 0 && (!s || s.scan_total === 0);

  // Chart data from 90-day timeline
  const chartData = timeline.map((d: any) => ({
    ...d,
    date: d.date.slice(5),
    avg_fps: d.avg_fps > 0 ? Math.round(d.avg_fps) : null,
    saved_gb: +(d.space_saved / (1024 ** 3)).toFixed(1),
    cumulative_tb: +(d.cumulative_saved / (1024 ** 4)).toFixed(2),
  }));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h2 style={{ color: "var(--text-primary)", fontSize: 20, margin: 0 }}>Dashboard</h2>
        {totalCompleted > 0 && (
          <div style={{ display: "flex", gap: 8 }}>
            <a href="/api/jobs/export/csv" download style={{ textDecoration: "none" }}>
              <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}>Export CSV</button>
            </a>
            <a href="/api/jobs/export/json" download style={{ textDecoration: "none" }}>
              <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}>Export JSON</button>
            </a>
          </div>
        )}
      </div>

      {/* ===== LIVE STATUS ===== */}

      {/* Status cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12 }}>
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

      {/* Today's summary bar */}
      <div style={{ ...cardStyle, display: "flex", gap: 28, padding: "12px 20px", flexWrap: "wrap" }}>
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

      {/* ===== EMPTY STATE ===== */}
      {hasNoData && (
        <div style={{ textAlign: "center", padding: 60 }}>
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.4, marginBottom: 16 }}>
            <rect x="3" y="3" width="18" height="18" rx="2" />
            <path d="M3 9h18" />
            <path d="M9 21V9" />
          </svg>
          <div style={{ fontSize: 14, color: "var(--text-muted)", opacity: 0.6 }}>No conversion data yet</div>
          <div style={{ fontSize: 12, color: "var(--text-muted)", opacity: 0.4, marginTop: 8 }}>
            Start scanning and converting files to see statistics here.
          </div>
        </div>
      )}

      {/* Everything below the status cards is hidden when hasNoData */}
      {!hasNoData && s && <>

        {/* ===== OVERVIEW (from Statistics, loaded once) ===== */}

        {/* Processing Results donut + Summary card */}
        {totalCompleted > 0 && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(350px, 1fr))", gap: 12 }}>
            <div style={cardStyle}>
              <h3 style={headingStyle}>Processing Results</h3>
              <Donut
                segments={[
                  { value: s.files_with_savings, color: "var(--accent)", label: "Saved space" },
                  { value: s.files_no_savings, color: "var(--border)", label: "Ignored (no savings)" },
                ]}
                centerText={`${totalCompleted > 0 ? Math.round(s.files_with_savings / totalCompleted * 100) : 0}%`}
              />
            </div>

            <div style={cardStyle}>
              <h3 style={headingStyle}>Summary</h3>
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {([
                  ["Files with savings", s.files_with_savings, "var(--accent)"],
                  ["Files ignored", s.files_no_savings, "var(--text-secondary)"],
                  ["Pending", s.pending, "var(--text-secondary)"],
                  ["Failed", s.failed, "#e94560"],
                ] as const).map(([label, val, color]) => (
                  <div key={label} style={{ display: "flex", justifyContent: "space-between" }}>
                    <span style={{ color: "var(--text-muted)", fontSize: 13 }}>{label}</span>
                    <span style={{ color, fontWeight: "bold" }}>{val}</span>
                  </div>
                ))}
                {s.avg_time_minutes > 0 && (
                  <div style={{ display: "flex", justifyContent: "space-between" }}>
                    <span style={{ color: "var(--text-muted)", fontSize: 13 }}>Avg time per file</span>
                    <span style={{ color: "var(--text-secondary)", fontWeight: "bold" }}>
                      {s.avg_time_minutes >= 60 ? `${(s.avg_time_minutes / 60).toFixed(1)}h` : `${s.avg_time_minutes.toFixed(0)}m`}
                    </span>
                  </div>
                )}
                {s.est_remaining_hours > 0 && (
                  <div style={{ display: "flex", justifyContent: "space-between" }}>
                    <span style={{ color: "var(--text-muted)", fontSize: 13 }}>Est. time remaining</span>
                    <span style={{ color: "#ffa94d", fontWeight: "bold" }}>
                      {s.est_remaining_hours >= 24 ? `${(s.est_remaining_hours / 24).toFixed(1)} days` : `${s.est_remaining_hours.toFixed(1)}h`}
                    </span>
                  </div>
                )}
                <div style={{ borderTop: "1px solid var(--border)", paddingTop: 10, display: "flex", justifyContent: "space-between" }}>
                  <span style={{ color: "var(--text-muted)" }}>Total saved</span>
                  <span style={{ color: "var(--accent)", fontWeight: "bold", fontSize: 16 }}>{formatBytes(s.total_saved)}</span>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Storage Projection */}
        {dash.projection && dash.projection.projected_days > 0 && (
          <div style={{ ...cardStyle, display: "flex", gap: 24, alignItems: "center" }}>
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

        {/* Conversion Status + Audio Cleanup Status */}
        {s.scan_total > 0 && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(350px, 1fr))", gap: 12 }}>
            <div style={cardStyle}>
              <h3 style={headingStyle}>Conversion Status</h3>
              <Donut
                size={110}
                segments={[
                  { value: s.needs_conversion, color: "#e94560", label: "Needs converting" },
                  { value: s.already_converted, color: "var(--accent)", label: "Converted by Squeezarr" },
                ]}
                centerText={`${s.needs_conversion + s.already_converted > 0 ? Math.round(s.already_converted / (s.needs_conversion + s.already_converted) * 100) : 0}%`}
              />
            </div>

            <div style={cardStyle}>
              <h3 style={headingStyle}>Audio Cleanup Status</h3>
              <Donut
                size={110}
                segments={[
                  { value: s.files_needing_audio_cleanup, color: "#ffa94d", label: "Needs cleanup" },
                  { value: s.files_audio_cleaned, color: "var(--accent)", label: "Cleaned by Squeezarr" },
                ]}
                centerText={`${s.files_audio_cleaned + s.files_needing_audio_cleanup > 0 ? Math.round(s.files_audio_cleaned / (s.files_audio_cleaned + s.files_needing_audio_cleanup) * 100) : 0}%`}
              />
            </div>
          </div>
        )}

        {/* ===== LIBRARY BREAKDOWN ===== */}

        {/* Video Codecs donut + Avg Reduction by Source bars */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(350px, 1fr))", gap: 12 }}>
          {s.scan_total > 0 && (
            <div style={cardStyle}>
              <h3 style={headingStyle}>Video Codecs (All Scanned)</h3>
              <Donut
                size={110}
                segments={s.codecs.map(([label, value]: [string, number], i: number) => ({
                  value, label, color: ["#e94560", "#9135ff", "#40ceff", "#18ffa5", "#ffa94d"][i % 5],
                }))}
                centerText={`${s.scan_total}`}
              />
            </div>
          )}

          {Object.keys(s.savings_by_source || {}).length > 0 && (
            <div style={cardStyle}>
              <h3 style={headingStyle}>Avg Reduction by Source</h3>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {Object.entries(s.savings_by_source).map(([src, data]: [string, any]) => (
                  <div key={src}>
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 3 }}>
                      <span style={{ color: "var(--text-muted)" }}>{src} ({data.count})</span>
                      <span style={{ color: "var(--success)", fontWeight: "bold" }}>{data.percent}%</span>
                    </div>
                    <div style={{ height: 8, background: "var(--bg-primary)", borderRadius: 4, overflow: "hidden" }}>
                      <div style={{ height: "100%", width: `${Math.min(100, data.percent)}%`, background: "var(--accent)", borderRadius: 4 }} />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Source Types donut + Resolution donut */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(350px, 1fr))", gap: 12 }}>
          <div style={cardStyle}>
            <h3 style={headingStyle}>Source Types</h3>
            <Donut
              size={110}
              segments={(s.source_types || []).map(([label, value]: [string, number], i: number) => ({
                value, label, color: donutColors[i % donutColors.length],
              }))}
              centerText={`${totalCompleted}`}
            />
          </div>

          <div style={cardStyle}>
            <h3 style={headingStyle}>Resolution</h3>
            <Donut
              size={110}
              segments={(s.resolutions || []).map(([label, value]: [string, number], i: number) => ({
                value, label, color: ["#9135ff", "#40ceff", "#18ffa5", "#ffa94d"][i % 4],
              }))}
              centerText={`${totalCompleted}`}
            />
          </div>
        </div>

        {/* ===== QUALITY (VMAF) ===== */}
        {s.vmaf_stats?.count > 0 && (() => {
          const vm = s.vmaf_stats;
          const excellent = vm.excellent || 0;
          const good = vm.good || 0;
          const fair = vm.fair || 0;
          const poor = vm.poor || 0;
          return (
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(350px, 1fr))", gap: 12 }}>
              <div style={cardStyle}>
                <h3 style={headingStyle}>VMAF Scores</h3>
                <Donut
                  size={130}
                  segments={[
                    { value: excellent, color: "#18ffa5", label: "Excellent (93+)" },
                    { value: good, color: "var(--accent)", label: "Good (87-93)" },
                    { value: fair, color: "#ffa94d", label: "Fair (80-87)" },
                    { value: poor, color: "#e94560", label: "Poor (<80)" },
                  ]}
                  centerText={vm.avg?.toFixed(1) ?? ""}
                />
              </div>
              <div style={cardStyle}>
                <h3 style={headingStyle}>VMAF Details</h3>
                <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                  <div style={{ display: "flex", justifyContent: "space-between" }}>
                    <span style={{ color: "var(--text-muted)", fontSize: 13 }}>Total scored</span>
                    <span style={{ color: "var(--text-secondary)", fontWeight: "bold" }}>{vm.count}</span>
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between" }}>
                    <span style={{ color: "var(--text-muted)", fontSize: 13 }}>Average VMAF</span>
                    <span style={{ color: "var(--accent)", fontWeight: "bold" }}>{vm.avg?.toFixed(1)}</span>
                  </div>
                  <div style={{ borderTop: "1px solid var(--border)", paddingTop: 10, display: "flex", flexDirection: "column", gap: 6 }}>
                    {([
                      ["Excellent (93+)", excellent, "#18ffa5"],
                      ["Good (87-93)", good, "var(--accent)"],
                      ["Fair (80-87)", fair, "#ffa94d"],
                      ["Poor (<80)", poor, "#e94560"],
                    ] as const).map(([label, val, color]) => (
                      <div key={label} style={{ display: "flex", justifyContent: "space-between" }}>
                        <span style={{ color: "var(--text-muted)", fontSize: 12 }}>{label}</span>
                        <span style={{ color, fontWeight: "bold", fontSize: 12 }}>{val}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          );
        })()}

        {/* ===== TRENDS (90-day charts) ===== */}
        {chartData.length > 1 && <>
          <h3 style={{ color: "var(--text-primary)", fontSize: 16, margin: "12px 0 4px" }}>Trends</h3>

          {/* Row 1: Cumulative Space Saved + Avg FPS per Job */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(400px, 1fr))", gap: 12 }}>
            <div style={{ ...cardStyle, minHeight: 250 }}>
              <h3 style={headingStyle}>Cumulative Space Saved</h3>
              <ResponsiveContainer width="100%" height={200}>
                <AreaChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                  <XAxis dataKey="date" tick={{ fill: "var(--text-muted)", fontSize: 10 }} />
                  <YAxis tick={{ fill: "var(--text-muted)", fontSize: 10 }} unit=" TB" />
                  <Tooltip {...tooltipStyle} />
                  <Area type="monotone" dataKey="cumulative_tb" stroke="#9135ff" fill="rgba(145,53,255,0.2)" strokeWidth={2} name="TB Saved" />
                </AreaChart>
              </ResponsiveContainer>
            </div>

            <div style={{ ...cardStyle, minHeight: 250 }}>
              <h3 style={headingStyle}>Avg FPS per Job</h3>
              <ResponsiveContainer width="100%" height={200}>
                <LineChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                  <XAxis dataKey="date" tick={{ fill: "var(--text-muted)", fontSize: 10 }} />
                  <YAxis tick={{ fill: "var(--text-muted)", fontSize: 10 }} />
                  <Tooltip {...tooltipStyle} formatter={(v: any) => [`${Math.round(v)} fps`, "Avg FPS/Job"]} />
                  <Line type="monotone" dataKey="avg_fps" stroke="#40ceff" dot={false} strokeWidth={2} name="Avg FPS/Job" connectNulls />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>

          {/* Row 2: Daily Space Saved + Daily Conversions */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(400px, 1fr))", gap: 12 }}>
            <div style={{ ...cardStyle, minHeight: 250 }}>
              <h3 style={headingStyle}>Daily Space Saved</h3>
              <ResponsiveContainer width="100%" height={200}>
                <RBarChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                  <XAxis dataKey="date" tick={{ fill: "var(--text-muted)", fontSize: 10 }} />
                  <YAxis tick={{ fill: "var(--text-muted)", fontSize: 10 }} unit=" GB" />
                  <Tooltip {...tooltipStyle} />
                  <Bar dataKey="saved_gb" fill="#18ffa5" radius={[3, 3, 0, 0]} name="GB Saved" />
                </RBarChart>
              </ResponsiveContainer>
            </div>

            <div style={{ ...cardStyle, minHeight: 250 }}>
              <h3 style={headingStyle}>Daily Conversions</h3>
              <ResponsiveContainer width="100%" height={200}>
                <RBarChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                  <XAxis dataKey="date" tick={{ fill: "var(--text-muted)", fontSize: 10 }} />
                  <YAxis tick={{ fill: "var(--text-muted)", fontSize: 10 }} />
                  <Tooltip {...tooltipStyle} />
                  <Bar dataKey="jobs_completed" fill="#9135ff" radius={[3, 3, 0, 0]} name="Jobs" />
                </RBarChart>
              </ResponsiveContainer>
            </div>
          </div>
        </>}

        {/* ===== DEEP DIVE ===== */}

        {/* File Size Distribution + Saved by Library */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(350px, 1fr))", gap: 12 }}>
          {(s.size_distribution || []).length > 0 && (
            <div style={cardStyle}>
              <h3 style={headingStyle}>File Size Distribution</h3>
              <HBarChart items={s.size_distribution.map((r: any) => ({ label: r.label, value: r.count }))} />
            </div>
          )}

          {(s.top_folders || []).length > 0 && (
            <div style={cardStyle}>
              <h3 style={headingStyle}>Saved by Library</h3>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {s.top_folders.map((f: any, i: number) => (
                  <div key={f.label} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span style={{ width: 100, fontSize: 11, color: "var(--text-muted)", textAlign: "right", flexShrink: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {f.label}
                    </span>
                    <div style={{ flex: 1, height: 18, background: "var(--bg-primary)", borderRadius: 3, overflow: "hidden" }}>
                      <div style={{
                        height: "100%", width: `${(f.value / s.top_folders[0].value) * 100}%`,
                        background: donutColors[i % donutColors.length],
                        borderRadius: 3,
                      }} />
                    </div>
                    <span style={{ width: 60, fontSize: 11, color: "var(--success)", fontWeight: "bold", textAlign: "right", flexShrink: 0 }}>
                      {formatBytes(f.value)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Top 10 Biggest Savings */}
        {(s.top_savers || []).length > 0 && (() => {
          const maxSaved = s.top_savers[0]?.space_saved || 1;
          return (
            <div style={cardStyle}>
              <h3 style={headingStyle}>Top 10 Biggest Savings</h3>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {s.top_savers.map((job: any, idx: number) => (
                  <div key={idx} style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    <div style={{ width: 220, fontSize: 11, color: "var(--text-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flexShrink: 0 }}>
                      {job.file_name}
                    </div>
                    <div style={{ flex: 1, height: 20, background: "var(--bg-primary)", borderRadius: 3, overflow: "hidden" }}>
                      <div style={{
                        height: "100%", width: `${(job.space_saved / maxSaved) * 100}%`,
                        background: "linear-gradient(90deg, var(--accent), var(--success))",
                        borderRadius: 3,
                      }} />
                    </div>
                    <span style={{ fontSize: 11, color: "var(--success)", fontWeight: "bold", width: 70, textAlign: "right", flexShrink: 0 }}>
                      {formatBytes(job.space_saved)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          );
        })()}

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

        {/* Native Languages donut + Audio Track Languages bars */}
        {s.scan_total > 0 && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(350px, 1fr))", gap: 12 }}>
            <div style={cardStyle}>
              <h3 style={headingStyle}>Native Languages (Scanned Titles)</h3>
              <Donut
                size={110}
                segments={(s.native_langs || []).map(([label, value]: [string, number], i: number) => ({
                  value, label, color: donutColors[i % donutColors.length],
                }))}
                centerText={`${s.scan_total}`}
              />
            </div>

            <div style={cardStyle}>
              <h3 style={headingStyle}>Audio Track Languages (All Scanned)</h3>
              <HBarChart
                items={(s.audio_langs || []).map(([label, value]: [string, number]) => ({ label, value }))}
              />
              <div style={{ marginTop: 12, fontSize: 12, color: "var(--text-muted)" }}>
                {s.total_audio_tracks} total audio tracks across {s.scan_total} files
              </div>
            </div>
          </div>
        )}

        {/* Audio Track Removal + Tracks by Language */}
        {(s.audio_tracks_deleted > 0 || s.tracks_marked_removal > 0) && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(350px, 1fr))", gap: 12 }}>
            <div style={cardStyle}>
              <h3 style={headingStyle}>Audio Track Removal</h3>
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span style={{ color: "var(--text-muted)", fontSize: 13 }}>Tracks removed (completed)</span>
                  <span style={{ color: "#ff6b9d", fontWeight: "bold" }}>{s.audio_tracks_deleted}</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span style={{ color: "var(--text-muted)", fontSize: 13 }}>Tracks marked for removal</span>
                  <span style={{ color: "#ffa94d", fontWeight: "bold" }}>{s.tracks_marked_removal}</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span style={{ color: "var(--text-muted)", fontSize: 13 }}>Total tracks scanned</span>
                  <span style={{ color: "var(--text-secondary)", fontWeight: "bold" }}>{s.total_audio_tracks}</span>
                </div>
                {s.total_audio_tracks > 0 && (
                  <div style={{ borderTop: "1px solid var(--border)", paddingTop: 10, display: "flex", justifyContent: "space-between" }}>
                    <span style={{ color: "var(--text-muted)", fontSize: 13 }}>Avg tracks per file</span>
                    <span style={{ color: "var(--text-secondary)", fontWeight: "bold" }}>{(s.total_audio_tracks / s.scan_total).toFixed(1)}</span>
                  </div>
                )}
              </div>
            </div>

            {(s.removed_langs || []).length > 0 && (
              <div style={cardStyle}>
                <h3 style={headingStyle}>Tracks Marked for Removal by Language</h3>
                <HBarChart
                  items={s.removed_langs.map(([label, value]: [string, number]) => ({ label, value }))}
                  colors={["#e94560", "#ff6b9d", "#ff8fb0", "#ffa94d", "#ffc078", "#ffd8a8", "#ffe8cc"]}
                />
              </div>
            )}
          </div>
        )}

        {/* Cloud Storage Savings + Drives Saved */}
        {s.total_saved > 0 && (() => {
          const savedTB = s.total_saved / (1024 ** 4);
          const savedGB = s.total_saved / (1024 ** 3);
          const cloudCosts = [
            { name: "Amazon S3", perTB: 23 },
            { name: "Google Cloud", perTB: 20 },
            { name: "Azure Blob", perTB: 18 },
            { name: "Backblaze B2", perTB: 5 },
            { name: "Wasabi", perTB: 7 },
          ];
          const driveTypes = [
            { name: "NAS Drive (8TB)", size: 8, price: 200 },
            { name: "NAS Drive (16TB)", size: 16, price: 300 },
            { name: "NAS Drive (20TB)", size: 20, price: 400 },
            { name: "Desktop HDD (8TB)", size: 8, price: 140 },
          ];
          return (
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(350px, 1fr))", gap: 12 }}>
              <div style={cardStyle}>
                <h3 style={{ ...headingStyle, marginBottom: 6 }}>Cloud Storage Savings</h3>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 12 }}>
                  If your library were in the cloud, you'd save this much per month by reclaiming {savedTB.toFixed(1)} TB:
                </div>
                {cloudCosts.map(c => (
                  <div key={c.name} style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 6 }}>
                    <span style={{ color: "var(--text-muted)" }}>{c.name}</span>
                    <span style={{ color: "var(--success)", fontWeight: 600 }}>${(savedTB * c.perTB).toFixed(2)}/mo</span>
                  </div>
                ))}
              </div>
              <div style={cardStyle}>
                <h3 style={{ ...headingStyle, marginBottom: 6 }}>Drives Saved</h3>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 12 }}>
                  You've reclaimed <strong style={{ color: "var(--text-primary)" }}>{savedTB >= 1 ? `${savedTB.toFixed(1)} TB` : `${savedGB.toFixed(0)} GB`}</strong> — that's fewer drives you need:
                </div>
                {driveTypes.map(d => {
                  const drivesSaved = savedTB / d.size;
                  const moneySaved = Math.floor(drivesSaved) * d.price;
                  return (
                    <div key={d.name} style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 6 }}>
                      <span style={{ color: "var(--text-muted)" }}>{d.name} · ${d.price}</span>
                      <span style={{ display: "flex", gap: 12 }}>
                        <span style={{ color: "var(--accent)", fontWeight: 600 }}>
                          {drivesSaved >= 1 ? `${Math.floor(drivesSaved)} drive${Math.floor(drivesSaved) !== 1 ? "s" : ""} saved` : `${(drivesSaved * 100).toFixed(0)}% of a drive`}
                        </span>
                        {moneySaved > 0 && (
                          <span style={{ color: "var(--success)", fontWeight: 600 }}>${moneySaved} saved</span>
                        )}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })()}

        {/* Encoding Efficiency by Source */}
        {s.savings_by_source && Object.keys(s.savings_by_source).length > 0 && (
          <div style={cardStyle}>
            <h3 style={{ ...headingStyle, marginBottom: 6 }}>Encoding Efficiency by Source</h3>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 16 }}>
              Which source types compress best? Higher % = more efficient. Focus encoding efforts on the best performers.
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {Object.entries(s.savings_by_source)
                .sort(([, a]: [string, any], [, b]: [string, any]) => b.percent - a.percent)
                .map(([src, data]: [string, any], idx: number) => {
                  const maxPct = Math.max(...Object.values(s.savings_by_source).map((v: any) => v.percent));
                  return (
                    <div key={src}>
                      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4, fontSize: 12 }}>
                        <span style={{ color: "var(--text-secondary)", fontWeight: 500 }}>
                          {idx === 0 && "\u{1F947} "}{idx === 1 && "\u{1F948} "}{idx === 2 && "\u{1F949} "}
                          {src}
                        </span>
                        <span style={{ display: "flex", gap: 12, color: "var(--text-muted)" }}>
                          <span>{data.count} files</span>
                          <span>{formatBytes(data.saved)} saved</span>
                          <span style={{ color: "var(--success)", fontWeight: 600 }}>{data.percent}%</span>
                        </span>
                      </div>
                      <div style={{ height: 8, background: "var(--bg-primary)", borderRadius: 4, overflow: "hidden" }}>
                        <div style={{
                          height: "100%",
                          width: `${(data.percent / maxPct) * 100}%`,
                          background: idx === 0 ? "var(--success)" : idx === 1 ? "var(--accent)" : "var(--text-muted)",
                          borderRadius: 4,
                          transition: "width 0.3s",
                        }} />
                      </div>
                      <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>
                        {formatBytes(data.original)} original → {formatBytes(data.original - data.saved)} encoded
                      </div>
                    </div>
                  );
                })}
            </div>
          </div>
        )}

      </>}
    </div>
  );
}
