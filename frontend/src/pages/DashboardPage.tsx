import { memo, useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Trans, useTranslation } from "react-i18next";
import { getDashboardData, getStatsTimeline, getStatsSummary, dismissSetup } from "../api";
import SetupWizard from "../components/SetupWizard";
import { fmtNum, fmtBytes } from "../fmt";
import { tierColor, vmafLabelWithRange } from "../utils/vmaf";
import { useVisibleInterval } from "../useVisibleInterval";
import {
  LineChart, Line, AreaChart, Area, BarChart as RBarChart, Bar,
  XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from "recharts";
import { useJobProgressMap } from "../jobProgressStore";

const cardStyle: React.CSSProperties = { background: "var(--bg-card)", padding: 20, borderRadius: 6 };
const headingStyle: React.CSSProperties = { color: "var(--text-primary)", fontSize: 14, marginBottom: 16 };
const donutColors = ["var(--accent)", "#6882ff", "var(--info)", "#2cf4e8", "var(--success)", "var(--pink)", "var(--caution)"];

const tooltipStyle = {
  contentStyle: { background: "var(--bg-tertiary)", border: "1px solid var(--border)", borderRadius: 6, fontSize: 12 },
  labelStyle: { color: "var(--text-muted)" },
  cursor: { fill: "var(--bg-tertiary)", opacity: 0.5 },
};

// Donut chart with optional center text
// Codec labels from /stats (stats._codec_label) → Scanner filter ids.
const CODEC_FILTERS: Record<string, string> = { "H.264": "x264", "H.265": "x265", "AV1": "av1" };

function Donut({ segments, size = 120, hole = 0.65, centerText }: {
  /** `to`: a page the legend entry links to (e.g. the filtered Scanner). */
  segments: { value: number; color: string; label: string; to?: string }[];
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
            {seg.to ? (
              <Link to={seg.to} style={{ color: "var(--text-muted)" }}>{seg.label}: <b style={{ color: "var(--text-secondary)" }}>{seg.value.toLocaleString()}</b></Link>
            ) : (
              <span style={{ color: "var(--text-muted)" }}>{seg.label}: <b style={{ color: "var(--text-secondary)" }}>{seg.value.toLocaleString()}</b></span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// Horizontal bar chart (renamed to avoid conflict with recharts BarChart)
function HBarChart({ items, colors }: { items: { label: string; value: number }[]; colors?: string[] }) {
  const max = Math.max(...items.map(i => i.value), 1);
  const defaultColors = ["var(--accent)", "#7c5cff", "#6882ff", "#54a8ff", "var(--info)", "#2cf4e8", "var(--success)"];
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
        background: "linear-gradient(90deg, var(--accent), var(--info))",
        transition: "width 0.5s",
      }} />
    </div>
  );
}

// Live "Converting" card — the only part of the dashboard that depends on
// jobProgressMap. Extracted so WebSocket progress ticks re-render only this
// card, not the big Recharts surfaces below (which cost ~60% CPU in Chrome
// when they re-render every ~500ms).
const LiveConvertingCard = memo(function LiveConvertingCard({
  activeJobs,
}: {
  activeJobs: any[];
}) {
  const { t } = useTranslation(["dashboard", "common"]);
  const jobProgressMap = useJobProgressMap();
  const liveJobs = activeJobs.map((j: any) => {
    const ws = jobProgressMap.get(j.id);
    return { ...j, progress: ws?.progress ?? j.progress, fps: ws?.fps ?? j.fps };
  });
  const combinedFps = liveJobs.reduce((s: number, j: any) => s + (j.fps || 0), 0);
  return (
    <div style={cardStyle}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: liveJobs.length > 0 ? 10 : 0 }}>
        <div>
          <span style={{ fontSize: 28, fontWeight: "bold", color: liveJobs.length > 0 ? "var(--accent-text)" : "var(--text-muted)" }}>
            {liveJobs.length > 0 ? liveJobs.length : t("dashboard:live.idle")}
          </span>
          <span style={{ fontSize: 11, color: "var(--text-muted)", marginLeft: 8 }}>
            {liveJobs.length > 0 ? t("dashboard:live.converting") : t("dashboard:live.noActiveJobs")}
          </span>
        </div>
        {combinedFps > 0 && (
          <span style={{ fontSize: 13, color: "var(--info)", fontWeight: 600 }}>{t("dashboard:live.fpsCombined", { fps: combinedFps.toFixed(0) })}</span>
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
  );
});

// --- Dashboard (merged with Statistics) ---

export default function DashboardPage() {
  const { t } = useTranslation(["dashboard", "common"]);
  const [dash, setDash] = useState<any>(null);
  const [stats, setStats] = useState<any>(null);
  const [timeline, setTimeline] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  // Decided once, when the data first arrives: the wizard's own steps add
  // folders and start a scan, which mustn't close it halfway (v0.10.0).
  const [wizardOpen, setWizardOpen] = useState<boolean | null>(null);
  useEffect(() => {
    if (!dash || wizardOpen !== null) return;
    const setup = dash.setup;
    const forced = new URLSearchParams(window.location.search).has("setup");
    setWizardOpen(forced || !!(setup && !setup.dismissed && (!setup.has_dirs || setup.scan_count === 0)));
  }, [dash, wizardOpen]);

  useEffect(() => {
    Promise.all([getDashboardData(), getStatsSummary(), getStatsTimeline(90)]).then(([d, s, t]) => {
      setDash(d);
      setStats(s);
      setTimeline(t.days || []);
      setLoading(false);
    }).catch(() => setLoading(false));
  }, []);

  // Poll dashboard data every 10s, pausing when the tab is hidden so Chrome
  // doesn't wake the page to rebuild state nothing can see.
  const pollDash = useCallback(() => {
    getDashboardData().then(setDash).catch(() => {});
  }, []);
  useVisibleInterval(pollDash, 10000);

  // Chart data derived from the 90-day timeline. Memoized so that a new array
  // reference is only created when `timeline` actually changes — otherwise
  // every jobProgressMap tick would blow out Recharts' internal memoization
  // and force a full SVG re-render (primary cause of ~60% CPU here).
  //
  // NOTE: This hook MUST live above any conditional return. Placing it after
  // the `loading` early-return caused a rules-of-hooks violation (different
  // hook count between renders) that crashed the whole app to a blank page.
  const chartData = useMemo(() => timeline.map((d: any) => ({
    ...d,
    date: d.date.slice(5),
    avg_fps: d.avg_fps > 0 ? Math.round(d.avg_fps) : null,
    saved_gb: +(d.space_saved / (1024 ** 3)).toFixed(1),
    cumulative_tb: +(d.cumulative_saved / (1024 ** 4)).toFixed(2),
  })), [timeline]);

  if (loading || !dash) {
    return (
      <div>
        <h2 style={{ color: "var(--text-primary)", fontSize: 20, marginBottom: 20 }}>{t("dashboard:title")}</h2>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", padding: 60 }}>
          <div className="spinner" />
          <div style={{ marginTop: 12, fontSize: 13, color: "var(--text-muted)" }}>{t("dashboard:loading")}</div>
        </div>
      </div>
    );
  }

  // Setup wizard for fresh installs (and /?setup)
  if (wizardOpen) {
    return <SetupWizard setup={dash.setup || {}} onClose={async () => {
      await dismissSetup().catch(() => {});
      setWizardOpen(false);
      getDashboardData().then(setDash).catch(() => {});
    }} onChanged={() => { getDashboardData().then(setDash).catch(() => {}); }} />;
  }

  // Live status data — only the LiveConvertingCard actually uses this.
  const activeJobs = dash.running_jobs || [];
  const diskColor = (free: number) => {
    const gb = free / (1024 ** 3);
    if (gb > 100) return "var(--success)";
    if (gb > 50) return "var(--caution)";
    return "var(--danger)";
  };
  const totalFree = dash.total_free || 0;
  const today = dash.today || {};

  // Stats shortcuts
  const s = stats;
  const totalCompleted = s?.files_processed || 0;
  const hasNoData = totalCompleted === 0 && activeJobs.length === 0 && (dash.queue?.pending || 0) === 0 && (!s || s.scan_total === 0);

  // "Combined fps" still used by the "Today's summary bar"; derive locally
  // (without the jobProgressMap override) — the summary bar doesn't need
  // real-time precision and keeping it out of the top-level render path
  // means the rest of the dashboard stops depending on jobProgressMap.
  const combinedFpsForSummary = activeJobs.reduce((sum: number, j: any) => sum + (j.fps || 0), 0);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h2 style={{ color: "var(--text-primary)", fontSize: 20, margin: 0 }}>{t("dashboard:title")}</h2>
        {totalCompleted > 0 && (
          <div style={{ display: "flex", gap: 8 }}>
            <a href="/api/jobs/export/csv" download style={{ textDecoration: "none" }}>
              <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}>{t("dashboard:exportCsv")}</button>
            </a>
            <a href="/api/jobs/export/json" download style={{ textDecoration: "none" }}>
              <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}>{t("dashboard:exportJson")}</button>
            </a>
          </div>
        )}
      </div>

      {/* ===== LIVE STATUS ===== */}

      {/* Status cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(180px, 100%), 1fr))", gap: 12 }}>
        {/* Converting — memoed, re-renders on progress ticks only */}
        <LiveConvertingCard activeJobs={activeJobs} />

        {/* Queue depth */}
        <div style={cardStyle}>
          <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--text-primary)" }}>{fmtNum(dash.queue?.pending)}</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>{t("dashboard:cards.pendingInQueue")}</div>
          {(dash.queue?.failed || 0) > 0 && (
            <div style={{ fontSize: 12, color: "var(--danger)", marginTop: 6 }}>{t("dashboard:cards.failed", { count: dash.queue.failed, num: fmtNum(dash.queue.failed) })}</div>
          )}
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 6 }}>{t("dashboard:cards.completed", { count: dash.queue?.completed || 0, num: dash.queue?.completed?.toLocaleString() || 0 })}</div>
        </div>

        {/* Total saved */}
        <div style={cardStyle}>
          <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--accent-text)" }}>{fmtBytes(dash.total_saved || 0)}</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>{t("dashboard:cards.totalSpaceSaved")}</div>
          {dash.bandwidth_pct > 0 && (
            <div style={{ fontSize: 12, color: "var(--success)", marginTop: 6 }}>{t("dashboard:cards.smallerFiles", { pct: dash.bandwidth_pct })}</div>
          )}
        </div>

        {/* Disk space */}
        <div style={cardStyle}>
          <div style={{ fontSize: 28, fontWeight: "bold", color: diskColor(totalFree) }}>
            {fmtBytes(totalFree)}
          </div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>{t("dashboard:cards.totalFreeDisk")}</div>
          {(dash.disk || []).length > 0 && (
            <div style={{ marginTop: 8, fontSize: 11 }}>
              {(dash.disk || []).map((d: any, i: number) => (
                <div key={i} style={{ display: "flex", justifyContent: "space-between", color: diskColor(d.free), marginTop: 2 }}>
                  <span>{d.label}</span>
                  <span>{fmtBytes(d.free)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Today's summary bar */}
      <div style={{ ...cardStyle, display: "flex", gap: 28, padding: "12px 20px", flexWrap: "wrap" }}>
        <span style={{ fontSize: 12, color: "var(--text-muted)", fontWeight: 600 }}>{t("dashboard:today.label")}</span>
        <span style={{ fontSize: 12 }}><b style={{ color: "var(--accent-text)" }}>{fmtNum(today.jobs_completed)}</b> <span style={{ color: "var(--text-muted)" }}>{t("dashboard:today.jobs")}</span></span>
        <span style={{ fontSize: 12 }}><b style={{ color: "var(--success)" }}>{fmtBytes(today.space_saved || 0)}</b> <span style={{ color: "var(--text-muted)" }}>{t("dashboard:today.saved")}</span></span>
        {(today.avg_fps || 0) > 0 && (
          <span style={{ fontSize: 12 }}><b style={{ color: "var(--info)" }}>{today.avg_fps.toFixed(0)}</b> <span style={{ color: "var(--text-muted)" }}>{t("dashboard:today.avgFpsPerJob")}</span></span>
        )}
        {combinedFpsForSummary > 0 && (
          <span style={{ fontSize: 12 }}><b style={{ color: "var(--info)" }}>{combinedFpsForSummary.toFixed(0)}</b> <span style={{ color: "var(--text-muted)" }}>{t("dashboard:today.combinedFps")}</span></span>
        )}
        {(today.original_size || 0) > 0 && (today.space_saved || 0) > 0 && (
          <span style={{ fontSize: 12 }}><b style={{ color: "var(--success)" }}>{((today.space_saved / today.original_size) * 100).toFixed(0)}%</b> <span style={{ color: "var(--text-muted)" }}>{t("dashboard:today.avgReduction")}</span></span>
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
          <div style={{ fontSize: 14, color: "var(--text-muted)" }}>{t("dashboard:empty.title")}</div>
          <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 8 }}>
            {t("dashboard:empty.hint")}
          </div>
        </div>
      )}

      {/* Everything below the status cards is hidden when hasNoData */}
      {!hasNoData && s && <>

        {/* ===== OVERVIEW (from Statistics, loaded once) ===== */}

        {/* Processing Results donut + Summary card */}
        {totalCompleted > 0 && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(350px, 100%), 1fr))", gap: 12 }}>
            <div style={cardStyle}>
              <h3 style={headingStyle}>{t("dashboard:results.title")}</h3>
              <Donut
                segments={[
                  { value: s.files_with_savings, color: "var(--accent-text)", label: t("dashboard:results.savedSpace") },
                  { value: s.files_no_savings, color: "var(--border)", label: t("dashboard:results.ignoredNoSavings") },
                ]}
                centerText={`${totalCompleted > 0 ? Math.round(s.files_with_savings / totalCompleted * 100) : 0}%`}
              />
            </div>

            <div style={cardStyle}>
              <h3 style={headingStyle}>{t("dashboard:summary.title")}</h3>
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                {([
                  [t("dashboard:summary.filesWithSavings"), s.files_with_savings, "var(--accent-text)"],
                  [t("dashboard:summary.filesIgnored"), s.files_no_savings, "var(--text-secondary)"],
                  [t("common:status.pending"), s.pending, "var(--text-secondary)"],
                  [t("common:status.failed"), s.failed, "var(--danger)"],
                ] as const).map(([label, val, color]) => (
                  <div key={label} style={{ display: "flex", justifyContent: "space-between" }}>
                    <span style={{ color: "var(--text-muted)", fontSize: 13 }}>{label}</span>
                    <span style={{ color, fontWeight: "bold" }}>{val}</span>
                  </div>
                ))}
                {s.avg_time_minutes > 0 && (
                  <div style={{ display: "flex", justifyContent: "space-between" }}>
                    <span style={{ color: "var(--text-muted)", fontSize: 13 }}>{t("dashboard:summary.avgTimePerFile")}</span>
                    <span style={{ color: "var(--text-secondary)", fontWeight: "bold" }}>
                      {s.avg_time_minutes >= 60 ? `${(s.avg_time_minutes / 60).toFixed(1)}h` : `${s.avg_time_minutes.toFixed(0)}m`}
                    </span>
                  </div>
                )}
                {s.est_remaining_hours > 0 && (
                  <div style={{ display: "flex", justifyContent: "space-between" }}>
                    <span style={{ color: "var(--text-muted)", fontSize: 13 }}>{t("dashboard:summary.estRemaining")}</span>
                    <span style={{ color: "var(--caution)", fontWeight: "bold" }}>
                      {s.est_remaining_hours >= 24 ? t("dashboard:summary.days", { value: (s.est_remaining_hours / 24).toFixed(1) }) : `${s.est_remaining_hours.toFixed(1)}h`}
                    </span>
                  </div>
                )}
                <div style={{ borderTop: "1px solid var(--border)", paddingTop: 10, display: "flex", justifyContent: "space-between" }}>
                  <span style={{ color: "var(--text-muted)" }}>{t("dashboard:summary.totalSaved")}</span>
                  <span style={{ color: "var(--accent-text)", fontWeight: "bold", fontSize: 16 }}>{fmtBytes(s.total_saved)}</span>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Storage Projection */}
        {dash.projection && dash.projection.projected_days > 0 && (
          <div style={{ ...cardStyle, display: "flex", gap: 24, alignItems: "center" }}>
            <div style={{ textAlign: "center", minWidth: 100 }}>
              <div style={{ fontSize: 28, fontWeight: "bold", color: "var(--accent-text)" }}>~{fmtBytes(dash.projection.projected_savings)}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{t("dashboard:projection.projectedSavings")}</div>
            </div>
            <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.8 }}>
              <Trans
                t={t}
                i18nKey="dashboard:projection.sentence"
                values={{
                  rate: fmtBytes(dash.projection.avg_daily_savings),
                  jobsPerDay: dash.projection.avg_jobs_per_day,
                  files: dash.projection.remaining_files.toLocaleString(),
                  size: fmtBytes(dash.projection.remaining_size),
                  duration: dash.projection.projected_days > 365
                    ? t("dashboard:projection.years", { value: (dash.projection.projected_days / 365).toFixed(1) })
                    : dash.projection.projected_days > 30
                    ? t("dashboard:projection.months", { value: (dash.projection.projected_days / 30).toFixed(1) })
                    : t("dashboard:projection.days", { value: dash.projection.projected_days }),
                }}
                components={{
                  accent: <b style={{ color: "var(--accent-text)" }} />,
                  b: <b />,
                  success: <b style={{ color: "var(--success)" }} />,
                }}
              />
            </div>
          </div>
        )}

        {/* Conversion Status + Audio Cleanup Status */}
        {s.scan_total > 0 && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(350px, 100%), 1fr))", gap: 12 }}>
            <div style={cardStyle}>
              <h3 style={headingStyle}>{t("dashboard:conversion.title")}</h3>
              <Donut
                size={110}
                segments={[
                  { value: s.needs_conversion, color: "var(--danger)", label: t("dashboard:conversion.needsConverting") },
                  { value: s.already_converted, color: "var(--accent-text)", label: t("dashboard:conversion.converted") },
                ]}
                centerText={`${s.needs_conversion + s.already_converted > 0 ? Math.round(s.already_converted / (s.needs_conversion + s.already_converted) * 100) : 0}%`}
              />
            </div>

            <div style={cardStyle}>
              <h3 style={headingStyle}>{t("dashboard:audioCleanup.title")}</h3>
              <Donut
                size={110}
                segments={[
                  { value: s.files_needing_audio_cleanup, color: "var(--caution)", label: t("dashboard:audioCleanup.needsCleanup") },
                  { value: s.files_audio_cleaned, color: "var(--accent-text)", label: t("dashboard:audioCleanup.cleaned") },
                ]}
                centerText={`${s.files_audio_cleaned + s.files_needing_audio_cleanup > 0 ? Math.round(s.files_audio_cleaned / (s.files_audio_cleaned + s.files_needing_audio_cleanup) * 100) : 0}%`}
              />
            </div>
          </div>
        )}

        {/* ===== LIBRARY BREAKDOWN ===== */}

        {/* Video Codecs donut + Avg Reduction by Source bars */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(350px, 100%), 1fr))", gap: 12 }}>
          {s.scan_total > 0 && (
            <div style={cardStyle}>
              <h3 style={headingStyle}>{t("dashboard:library.videoCodecs")}</h3>
              <Donut
                size={110}
                segments={s.codecs.map(([label, value]: [string, number], i: number) => ({
                  value, label, color: ["var(--danger)", "var(--accent)", "var(--info)", "var(--success)", "var(--caution)"][i % 5],
                  to: CODEC_FILTERS[label] && `/scanner?filter=${CODEC_FILTERS[label]}`,
                }))}
                centerText={`${s.scan_total}`}
              />
            </div>
          )}

          {Object.keys(s.savings_by_source || {}).length > 0 && (
            <div style={cardStyle}>
              <h3 style={headingStyle}>{t("dashboard:library.avgReductionBySource")}</h3>
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {Object.entries(s.savings_by_source).map(([src, data]: [string, any]) => (
                  <div key={src}>
                    <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 3 }}>
                      <span style={{ color: "var(--text-muted)" }}>{src} ({fmtNum(data.count)})</span>
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
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(350px, 100%), 1fr))", gap: 12 }}>
          <div style={cardStyle}>
            <h3 style={headingStyle}>{t("dashboard:library.sourceTypes")}</h3>
            <Donut
              size={110}
              segments={(s.source_types || []).map(([label, value]: [string, number], i: number) => ({
                value, label, color: donutColors[i % donutColors.length],
              }))}
              centerText={`${totalCompleted}`}
            />
          </div>

          <div style={cardStyle}>
            <h3 style={headingStyle}>{t("dashboard:library.resolution")}</h3>
            <Donut
              size={110}
              segments={(s.resolutions || []).map(([label, value]: [string, number], i: number) => ({
                value, label, color: ["var(--accent)", "var(--info)", "var(--success)", "var(--caution)"][i % 4],
              }))}
              centerText={`${totalCompleted}`}
            />
          </div>
        </div>

        {/* ===== QUALITY (VMAF) =====
            Tiers come from utils/vmaf — single source of truth across the
            app. Backend's vmaf_stats sends `excellent` / `good` / `poor`
            counts (Fair was folded into Poor in v0.3.32). */}
        {s.vmaf_stats?.count > 0 && (() => {
          const vm = s.vmaf_stats;
          const excellent = vm.excellent || 0;
          const good = vm.good || 0;
          const poor = vm.poor || 0;
          const tierRows = [
            { tier: "excellent" as const, count: excellent },
            { tier: "good"      as const, count: good      },
            { tier: "poor"      as const, count: poor      },
          ];
          return (
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(350px, 100%), 1fr))", gap: 12 }}>
              <div style={cardStyle}>
                <h3 style={headingStyle}>{t("dashboard:vmaf.scores")}</h3>
                <Donut
                  size={130}
                  segments={tierRows.map(r => ({
                    value: r.count,
                    color: tierColor(r.tier),
                    label: vmafLabelWithRange(r.tier),
                  }))}
                  centerText={vm.avg?.toFixed(1) ?? ""}
                />
              </div>
              <div style={cardStyle}>
                <h3 style={headingStyle}>{t("dashboard:vmaf.details")}</h3>
                <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                  <div style={{ display: "flex", justifyContent: "space-between" }}>
                    <span style={{ color: "var(--text-muted)", fontSize: 13 }}>{t("dashboard:vmaf.totalScored")}</span>
                    <span style={{ color: "var(--text-secondary)", fontWeight: "bold" }}>{vm.count}</span>
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between" }}>
                    <span style={{ color: "var(--text-muted)", fontSize: 13 }}>{t("dashboard:vmaf.average")}</span>
                    <span style={{ color: "var(--accent-text)", fontWeight: "bold" }}>{vm.avg?.toFixed(1)}</span>
                  </div>
                  <div style={{ borderTop: "1px solid var(--border)", paddingTop: 10, display: "flex", flexDirection: "column", gap: 6 }}>
                    {tierRows.map(r => (
                      <div key={r.tier} style={{ display: "flex", justifyContent: "space-between" }}>
                        <span style={{ color: "var(--text-muted)", fontSize: 12 }}>{vmafLabelWithRange(r.tier)}</span>
                        <span style={{ color: tierColor(r.tier), fontWeight: "bold", fontSize: 12 }}>{r.count}</span>
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
          <h3 style={{ color: "var(--text-primary)", fontSize: 16, margin: "12px 0 4px" }}>{t("dashboard:trends.title")}</h3>

          {/* Row 1: Cumulative Space Saved + Avg FPS per Job */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(400px, 100%), 1fr))", gap: 12 }}>
            <div style={{ ...cardStyle, minHeight: 250 }}>
              <h3 style={headingStyle}>{t("dashboard:trends.cumulativeSaved")}</h3>
              <ResponsiveContainer width="100%" height={200}>
                <AreaChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                  <XAxis dataKey="date" tick={{ fill: "var(--text-muted)", fontSize: 10 }} />
                  <YAxis tick={{ fill: "var(--text-muted)", fontSize: 10 }} unit=" TB" />
                  <Tooltip {...tooltipStyle} />
                  <Area type="monotone" dataKey="cumulative_tb" stroke="var(--accent)" fill="rgba(104,96,254,0.2)" strokeWidth={2} name={t("dashboard:trends.tbSaved")} />
                </AreaChart>
              </ResponsiveContainer>
            </div>

            <div style={{ ...cardStyle, minHeight: 250 }}>
              <h3 style={headingStyle}>{t("dashboard:trends.avgFpsPerJob")}</h3>
              <ResponsiveContainer width="100%" height={200}>
                <LineChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                  <XAxis dataKey="date" tick={{ fill: "var(--text-muted)", fontSize: 10 }} />
                  <YAxis tick={{ fill: "var(--text-muted)", fontSize: 10 }} />
                  <Tooltip {...tooltipStyle} formatter={(v: any) => [t("dashboard:trends.fpsValue", { value: Math.round(v) }), t("dashboard:trends.avgFpsJobSeries")]} />
                  <Line type="monotone" dataKey="avg_fps" stroke="var(--info)" dot={false} strokeWidth={2} name={t("dashboard:trends.avgFpsJobSeries")} connectNulls />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>

          {/* Row 2: Daily Space Saved + Daily Conversions */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(400px, 100%), 1fr))", gap: 12 }}>
            <div style={{ ...cardStyle, minHeight: 250 }}>
              <h3 style={headingStyle}>{t("dashboard:trends.dailySaved")}</h3>
              <ResponsiveContainer width="100%" height={200}>
                <RBarChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                  <XAxis dataKey="date" tick={{ fill: "var(--text-muted)", fontSize: 10 }} />
                  <YAxis tick={{ fill: "var(--text-muted)", fontSize: 10 }} unit=" GB" />
                  <Tooltip {...tooltipStyle} />
                  <Bar dataKey="saved_gb" fill="var(--success)" radius={[3, 3, 0, 0]} name={t("dashboard:trends.gbSaved")} />
                </RBarChart>
              </ResponsiveContainer>
            </div>

            <div style={{ ...cardStyle, minHeight: 250 }}>
              <h3 style={headingStyle}>{t("dashboard:trends.dailyConversions")}</h3>
              <ResponsiveContainer width="100%" height={200}>
                <RBarChart data={chartData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                  <XAxis dataKey="date" tick={{ fill: "var(--text-muted)", fontSize: 10 }} />
                  <YAxis tick={{ fill: "var(--text-muted)", fontSize: 10 }} />
                  <Tooltip {...tooltipStyle} />
                  <Bar dataKey="jobs_completed" fill="var(--accent)" radius={[3, 3, 0, 0]} name={t("dashboard:trends.jobs")} />
                </RBarChart>
              </ResponsiveContainer>
            </div>
          </div>
        </>}

        {/* ===== DEEP DIVE ===== */}

        {/* File Size Distribution + Saved by Library */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(350px, 100%), 1fr))", gap: 12 }}>
          {(s.size_distribution || []).length > 0 && (
            <div style={cardStyle}>
              <h3 style={headingStyle}>{t("dashboard:deepDive.sizeDistribution")}</h3>
              <HBarChart items={s.size_distribution.map((r: any) => ({ label: r.label, value: r.count }))} />
            </div>
          )}

          {(s.top_folders || []).length > 0 && (
            <div style={cardStyle}>
              <h3 style={headingStyle}>{t("dashboard:deepDive.savedByLibrary")}</h3>
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
                      {fmtBytes(f.value)}
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
              <h3 style={headingStyle}>{t("dashboard:deepDive.topSavings")}</h3>
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
                      {fmtBytes(job.space_saved)}
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
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{t("dashboard:bandwidth.avgReduction")}</div>
            </div>
            <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.7 }}>
              {t("dashboard:bandwidth.description", { pct: dash.bandwidth_pct })}
            </div>
          </div>
        )}

        {/* Native Languages donut + Audio Track Languages bars */}
        {s.scan_total > 0 && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(350px, 100%), 1fr))", gap: 12 }}>
            <div style={cardStyle}>
              <h3 style={headingStyle}>{t("dashboard:languages.native")}</h3>
              <Donut
                size={110}
                segments={(s.native_langs || []).map(([label, value]: [string, number], i: number) => ({
                  value, label, color: donutColors[i % donutColors.length],
                }))}
                centerText={`${s.scan_total}`}
              />
            </div>

            <div style={cardStyle}>
              <h3 style={headingStyle}>{t("dashboard:languages.audioTracks")}</h3>
              <HBarChart
                items={(s.audio_langs || []).map(([label, value]: [string, number]) => ({ label, value }))}
              />
              <div style={{ marginTop: 12, fontSize: 12, color: "var(--text-muted)" }}>
                {t("dashboard:languages.tracksAcrossFiles", { tracks: s.total_audio_tracks, files: s.scan_total })}
              </div>
            </div>
          </div>
        )}

        {/* Audio Track Removal + Tracks by Language */}
        {(s.audio_tracks_deleted > 0 || s.tracks_marked_removal > 0) && (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(350px, 100%), 1fr))", gap: 12 }}>
            <div style={cardStyle}>
              <h3 style={headingStyle}>{t("dashboard:audioRemoval.title")}</h3>
              <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span style={{ color: "var(--text-muted)", fontSize: 13 }}>{t("dashboard:audioRemoval.removedCompleted")}</span>
                  <span style={{ color: "var(--pink)", fontWeight: "bold" }}>{s.audio_tracks_deleted}</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span style={{ color: "var(--text-muted)", fontSize: 13 }}>{t("dashboard:audioRemoval.markedForRemoval")}</span>
                  <span style={{ color: "var(--caution)", fontWeight: "bold" }}>{s.tracks_marked_removal}</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between" }}>
                  <span style={{ color: "var(--text-muted)", fontSize: 13 }}>{t("dashboard:audioRemoval.totalScanned")}</span>
                  <span style={{ color: "var(--text-secondary)", fontWeight: "bold" }}>{s.total_audio_tracks}</span>
                </div>
                {s.total_audio_tracks > 0 && (
                  <div style={{ borderTop: "1px solid var(--border)", paddingTop: 10, display: "flex", justifyContent: "space-between" }}>
                    <span style={{ color: "var(--text-muted)", fontSize: 13 }}>{t("dashboard:audioRemoval.avgPerFile")}</span>
                    <span style={{ color: "var(--text-secondary)", fontWeight: "bold" }}>{(s.total_audio_tracks / s.scan_total).toFixed(1)}</span>
                  </div>
                )}
              </div>
            </div>

            {(s.removed_langs || []).length > 0 && (
              <div style={cardStyle}>
                <h3 style={headingStyle}>{t("dashboard:audioRemoval.byLanguage")}</h3>
                <HBarChart
                  items={s.removed_langs.map(([label, value]: [string, number]) => ({ label, value }))}
                  colors={["var(--danger)", "var(--pink)", "#ff8fb0", "var(--caution)", "#ffc078", "#ffd8a8", "#ffe8cc"]}
                />
              </div>
            )}
          </div>
        )}

        {/* Cloud Storage Savings + Drives Saved */}
        {s.total_saved > 0 && (() => {
          const savedTB = s.total_saved / (1024 ** 4);
          const cloudCosts = [
            { name: "Amazon S3", perTB: 23 },
            { name: "Google Cloud", perTB: 20 },
            { name: "Azure Blob", perTB: 18 },
            { name: "Backblaze B2", perTB: 5 },
            { name: "Wasabi", perTB: 7 },
          ];
          // Prices sourced from Amazon/Newegg for Seagate IronWolf NAS drives.
          // Review every ~6 months and update — drive prices drift noticeably.
          const driveTypes = [
            { name: "IronWolf 4TB",  size: 4,  price: 159.99 },
            { name: "IronWolf 8TB",  size: 8,  price: 279.99 },
            { name: "IronWolf 12TB", size: 12, price: 349.99 },
            { name: "IronWolf 16TB", size: 16, price: 449.99 },
            { name: "IronWolf 20TB", size: 20, price: 569.99 },
          ];
          return (
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(min(350px, 100%), 1fr))", gap: 12 }}>
              <div style={cardStyle}>
                <h3 style={{ ...headingStyle, marginBottom: 6 }}>{t("dashboard:cloud.title")}</h3>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 12 }}>
                  {t("dashboard:cloud.description", { size: fmtBytes(s.total_saved) })}
                </div>
                {cloudCosts.map(c => (
                  <div key={c.name} style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 6 }}>
                    <span style={{ color: "var(--text-muted)" }}>{c.name}</span>
                    <span style={{ color: "var(--success)", fontWeight: 600 }}>{t("dashboard:cloud.perMonth", { amount: (savedTB * c.perTB).toFixed(2) })}</span>
                  </div>
                ))}
              </div>
              <div style={cardStyle}>
                <h3 style={{ ...headingStyle, marginBottom: 6 }}>{t("dashboard:drives.title")}</h3>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 12 }}>
                  <Trans
                    t={t}
                    i18nKey="dashboard:drives.description"
                    values={{ amount: fmtBytes(s.total_saved) }}
                    components={{ strong: <strong style={{ color: "var(--text-primary)" }} /> }}
                  />
                </div>
                {driveTypes.map(d => {
                  const drivesSaved = savedTB / d.size;
                  // Round to whole dollars for a cleaner display — these are
                  // approximations anyway, and decimal cents on "$559.98 saved"
                  // looks visually noisy in the table.
                  const moneySaved = Math.round(Math.floor(drivesSaved) * d.price);
                  return (
                    <div key={d.name} style={{ display: "flex", justifyContent: "space-between", fontSize: 12, marginBottom: 6 }}>
                      <span style={{ color: "var(--text-muted)" }}>{d.name} · ${d.price}</span>
                      <span style={{ display: "flex", gap: 12 }}>
                        <span style={{ color: "var(--accent-text)", fontWeight: 600 }}>
                          {drivesSaved >= 1 ? t("dashboard:drives.drivesSaved", { count: Math.floor(drivesSaved) }) : t("dashboard:drives.pctOfDrive", { pct: (drivesSaved * 100).toFixed(0) })}
                        </span>
                        {moneySaved > 0 && (
                          <span style={{ color: "var(--success)", fontWeight: 600 }}>{t("dashboard:drives.moneySaved", { amount: moneySaved })}</span>
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
            <h3 style={{ ...headingStyle, marginBottom: 6 }}>{t("dashboard:efficiency.title")}</h3>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 16 }}>
              {t("dashboard:efficiency.description")}
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
                          <span>{t("dashboard:efficiency.files", { count: data.count, num: fmtNum(data.count) })}</span>
                          <span>{t("dashboard:efficiency.saved", { size: fmtBytes(data.saved) })}</span>
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
                        {t("dashboard:efficiency.originalToEncoded", { original: fmtBytes(data.original), encoded: fmtBytes(data.original - data.saved) })}
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
