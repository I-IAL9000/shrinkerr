import { useState, useEffect } from "react";
import { getStatsSummary, getStatsTimeline } from "../api";
import { LineChart, Line, AreaChart, Area, BarChart as RBarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";

function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 4) return `${(bytes / (1024 ** 4)).toFixed(2)} TB`;
  if (bytes >= 1024 ** 3) return `${(bytes / (1024 ** 3)).toFixed(1)} GB`;
  return `${(bytes / (1024 ** 2)).toFixed(0)} MB`;
}

// Simple donut component
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
            <span style={{ color: "var(--text-muted)" }}>{seg.label}: <b style={{ color: "var(--text-secondary)" }}>{seg.value}</b></span>
          </div>
        ))}
      </div>
    </div>
  );
}

// Horizontal bar chart
function BarChart({ items, colors }: { items: { label: string; value: number }[]; colors?: string[] }) {
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

export default function SavedSpacePage() {
  const [s, setS] = useState<any>(null);
  const [timeline, setTimeline] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([getStatsSummary(), getStatsTimeline(90)]).then(([data, t]) => {
      setS(data);
      setTimeline(t.days || []);
      setLoading(false);
    }).catch(() => setLoading(false));
  }, []);

  const donutColors = ["#9135ff", "#6882ff", "#40ceff", "#2cf4e8", "#18ffa5", "#ff6b9d", "#ffa94d"];

  if (loading || !s) {
    return (
      <div>
        <h2 style={{ color: "var(--text-primary)", fontSize: 20 }}>Statistics</h2>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: 60 }}>
          <div className="spinner" />
          <div style={{ marginTop: 12, fontSize: 13, opacity: 0.5 }}>Loading statistics...</div>
        </div>
      </div>
    );
  }

  const totalCompleted = s.files_processed;
  const avgTime = s.avg_time_minutes;
  const estRemaining = s.est_remaining_hours;
  const maxSaved = s.top_savers[0]?.space_saved || 1;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h2 style={{ color: "var(--text-primary)", fontSize: 20 }}>Statistics</h2>
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

      {/* Overview stat cards */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12 }}>
        <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6, textAlign: "center" }}>
          <div style={{ fontSize: 26, fontWeight: "bold", color: "var(--accent)" }}>{formatBytes(s.total_saved)}</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>Total saved</div>
        </div>
        <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6, textAlign: "center" }}>
          <div style={{ fontSize: 26, fontWeight: "bold", color: "var(--success)" }}>{s.percent_saved}%</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>Avg reduction</div>
        </div>
        <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6, textAlign: "center" }}>
          <div style={{ fontSize: 26, fontWeight: "bold", color: "var(--text-primary)" }}>{formatBytes(s.avg_per_file)}</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>Avg per file</div>
        </div>
        <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6, textAlign: "center" }}>
          <div style={{ fontSize: 26, fontWeight: "bold", color: "var(--text-primary)" }}>{totalCompleted}</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>Files processed</div>
        </div>
        <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6, textAlign: "center" }}>
          <div style={{ fontSize: 26, fontWeight: "bold", color: "#ff6b9d" }}>{s.audio_tracks_deleted}</div>
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>Audio tracks removed</div>
        </div>
      </div>

      {/* Row 1: Processing Results + Summary */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(350px, 1fr))", gap: 16, marginBottom: 16 }}>
        <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
          <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Processing Results</h3>
          <Donut
            segments={[
              { value: s.files_with_savings, color: "var(--accent)", label: "Saved space" },
              { value: s.files_no_savings, color: "var(--border)", label: "Ignored (no savings)" },
            ]}
            centerText={`${totalCompleted > 0 ? Math.round(s.files_with_savings / totalCompleted * 100) : 0}%`}
          />
        </div>

        <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
          <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Summary</h3>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {[
              ["Files with savings", s.files_with_savings, "var(--accent)"],
              ["Files ignored", s.files_no_savings, "var(--text-secondary)"],
              ["Pending", s.pending, "var(--text-secondary)"],
              ["Failed", s.failed, "#e94560"],
            ].map(([label, val, color]) => (
              <div key={label as string} style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-muted)", fontSize: 13 }}>{label}</span>
                <span style={{ color: color as string, fontWeight: "bold" }}>{val}</span>
              </div>
            ))}
            {avgTime > 0 && (
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-muted)", fontSize: 13 }}>Avg time per file</span>
                <span style={{ color: "var(--text-secondary)", fontWeight: "bold" }}>
                  {avgTime >= 60 ? `${(avgTime / 60).toFixed(1)}h` : `${avgTime.toFixed(0)}m`}
                </span>
              </div>
            )}
            {estRemaining > 0 && (
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <span style={{ color: "var(--text-muted)", fontSize: 13 }}>Est. time remaining</span>
                <span style={{ color: "#ffa94d", fontWeight: "bold" }}>
                  {estRemaining >= 24 ? `${(estRemaining / 24).toFixed(1)} days` : `${estRemaining.toFixed(1)}h`}
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

      {/* Row 2: Source Type + Resolution */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(350px, 1fr))", gap: 16, marginBottom: 16 }}>
        <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
          <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Source Types</h3>
          <Donut
            size={110}
            segments={s.source_types.map(([label, value]: [string, number], i: number) => ({
              value, label, color: donutColors[i % donutColors.length],
            }))}
            centerText={`${totalCompleted}`}
          />
        </div>

        <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
          <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Resolution</h3>
          <Donut
            size={110}
            segments={s.resolutions.map(([label, value]: [string, number], i: number) => ({
              value, label, color: ["#9135ff", "#40ceff", "#18ffa5", "#ffa94d"][i % 4],
            }))}
            centerText={`${totalCompleted}`}
          />
        </div>
      </div>

      {/* Row 3: Conversion Status + Audio Cleanup Status */}
      {s.scan_total > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(350px, 1fr))", gap: 16, marginBottom: 16 }}>
          <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
            <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Conversion Status</h3>
            <Donut
              size={110}
              segments={[
                { value: s.needs_conversion, color: "#e94560", label: "Needs converting" },
                { value: s.already_converted, color: "var(--accent)", label: "Converted by Squeezarr" },
              ]}
              centerText={`${s.needs_conversion + s.already_converted > 0 ? Math.round(s.already_converted / (s.needs_conversion + s.already_converted) * 100) : 0}%`}
            />
          </div>

          <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
            <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Audio Cleanup Status</h3>
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

      {/* Row 4: Codec Distribution + Savings by Source */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(350px, 1fr))", gap: 16, marginBottom: 16 }}>
        {s.scan_total > 0 && (
          <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
            <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Video Codecs (All Scanned)</h3>
            <Donut
              size={110}
              segments={s.codecs.map(([label, value]: [string, number], i: number) => ({
                value, label, color: ["#e94560", "#9135ff", "#40ceff", "#18ffa5", "#ffa94d"][i % 5],
              }))}
              centerText={`${s.scan_total}`}
            />
          </div>
        )}

        {Object.keys(s.savings_by_source).length > 0 && (
          <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
            <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Avg Reduction by Source</h3>
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

      {/* Row 5: File Size Distribution + Saved by Library */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(350px, 1fr))", gap: 16, marginBottom: 16 }}>
        <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
          <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>File Size Distribution</h3>
          <BarChart items={s.size_distribution.map((r: any) => ({ label: r.label, value: r.count }))} />
        </div>

        {s.top_folders.length > 0 && (
          <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
            <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Saved by Library</h3>
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

      {/* Row 6: Native Languages + Audio Track Languages */}
      {s.scan_total > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(350px, 1fr))", gap: 16, marginBottom: 16 }}>
          <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
            <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Native Languages (Scanned Titles)</h3>
            <Donut
              size={110}
              segments={s.native_langs.map(([label, value]: [string, number], i: number) => ({
                value, label, color: donutColors[i % donutColors.length],
              }))}
              centerText={`${s.scan_total}`}
            />
          </div>

          <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
            <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Audio Track Languages (All Scanned)</h3>
            <BarChart
              items={s.audio_langs.map(([label, value]: [string, number]) => ({ label, value }))}
            />
            <div style={{ marginTop: 12, fontSize: 12, color: "var(--text-muted)" }}>
              {s.total_audio_tracks} total audio tracks across {s.scan_total} files
            </div>
          </div>
        </div>
      )}

      {/* Row 7: Audio Tracks Removed + Removed by Language */}
      {(s.audio_tracks_deleted > 0 || s.tracks_marked_removal > 0) && (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(350px, 1fr))", gap: 16, marginBottom: 16 }}>
          <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
            <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Audio Track Removal</h3>
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

          {s.removed_langs.length > 0 && (
            <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
              <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Tracks Marked for Removal by Language</h3>
              <BarChart
                items={s.removed_langs.map(([label, value]: [string, number]) => ({ label, value }))}
                colors={["#e94560", "#ff6b9d", "#ff8fb0", "#ffa94d", "#ffc078", "#ffd8a8", "#ffe8cc"]}
              />
            </div>
          )}
        </div>
      )}

      {/* Top 10 Biggest Savings */}
      {s.top_savers.length > 0 && (
        <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
          <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Top 10 Biggest Savings</h3>
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
      )}

      {/* Cloud Storage Savings + Drives Saved (side by side) */}
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
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
            <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
              <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 6 }}>Cloud Storage Savings</h3>
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
            <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
              <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 6 }}>Drives Saved</h3>
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

      {/* Encoding Efficiency Ranking */}
      {s.savings_by_source && Object.keys(s.savings_by_source).length > 0 && (
        <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
          <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 6 }}>Encoding Efficiency by Source</h3>
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
                        {idx === 0 && "🥇 "}{idx === 1 && "🥈 "}{idx === 2 && "🥉 "}
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

      {/* Trend Charts */}
      {timeline.length > 1 && (() => {
        const tooltipStyle = {
          contentStyle: { background: "#1a1030", border: "1px solid var(--border)", borderRadius: 6, fontSize: 12 },
          labelStyle: { color: "var(--text-muted)" },
        };
        const chartData = timeline.map((d: any) => ({
          ...d,
          date: d.date.slice(5),
          avg_fps: d.avg_fps > 0 ? Math.round(d.avg_fps) : null,
          saved_gb: +(d.space_saved / (1024 ** 3)).toFixed(1),
          cumulative_tb: +(d.cumulative_saved / (1024 ** 4)).toFixed(2),
        }));
        return <>
          <h3 style={{ color: "var(--text-primary)", fontSize: 16, marginTop: 24, marginBottom: 16 }}>Trends</h3>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(350px, 1fr))", gap: 16, marginBottom: 16 }}>
            <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
              <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Cumulative Space Saved</h3>
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
            <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
              <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Avg FPS per Job Trend</h3>
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
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(350px, 1fr))", gap: 16, marginBottom: 16 }}>
            <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
              <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Daily Space Saved</h3>
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
            <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
              <h3 style={{ color: "var(--text-primary)", fontSize: 14, marginBottom: 16 }}>Daily Conversions</h3>
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
        </>;
      })()}

      {totalCompleted === 0 && (
        <div style={{ textAlign: "center", padding: 60, opacity: 0.5 }}>
          No completed jobs yet. Start converting files to see statistics.
        </div>
      )}
    </div>
  );
}
