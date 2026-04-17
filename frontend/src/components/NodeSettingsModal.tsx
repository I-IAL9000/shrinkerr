import { useState } from "react";
import { updateNodeSettings } from "../api";
import type { WorkerNode, NodeSettings } from "../types";

interface Props {
  node: WorkerNode;
  onClose: () => void;
  onSaved: () => void;
}

// NVENC <-> libx265 equivalence used by the worker's translation logic
const CQ_CRF_TABLE: { nvenc_preset: string; libx265_preset: string; nvenc_cq: number; libx265_crf: number }[] = [
  { nvenc_preset: "p1", libx265_preset: "veryfast", nvenc_cq: 20, libx265_crf: 16 },
  { nvenc_preset: "p2", libx265_preset: "faster",   nvenc_cq: 22, libx265_crf: 18 },
  { nvenc_preset: "p3", libx265_preset: "fast",     nvenc_cq: 24, libx265_crf: 20 },
  { nvenc_preset: "p4", libx265_preset: "medium",   nvenc_cq: 26, libx265_crf: 22 },
  { nvenc_preset: "p5", libx265_preset: "slow",     nvenc_cq: 27, libx265_crf: 23 },
  { nvenc_preset: "p6", libx265_preset: "slower",   nvenc_cq: 28, libx265_crf: 24 },
  { nvenc_preset: "p7", libx265_preset: "veryslow", nvenc_cq: 30, libx265_crf: 26 },
];

export default function NodeSettingsModal({ node, onClose, onSaved }: Props) {
  const [settings, setSettings] = useState<NodeSettings>({
    paused: node.paused ?? false,
    max_jobs: node.max_jobs ?? 1,
    job_affinity: node.job_affinity ?? "any",
    translate_encoder: node.translate_encoder ?? true,
    schedule_enabled: node.schedule_enabled ?? false,
    schedule_hours: node.schedule_hours ?? [],
  });
  const [saving, setSaving] = useState(false);
  const [showTable, setShowTable] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await updateNodeSettings(node.id, settings);
      onSaved();  // closes modal + refreshes
    } catch (e: any) {
      setError(e?.message || "Save failed");
      setSaving(false);
    }
  };

  const toggleHour = (h: number) => {
    const hrs = new Set(settings.schedule_hours || []);
    if (hrs.has(h)) hrs.delete(h); else hrs.add(h);
    setSettings({ ...settings, schedule_hours: [...hrs].sort((a, b) => a - b) });
  };

  const hasNvenc = node.capabilities.includes("nvenc");
  const hasLibx265 = node.capabilities.includes("libx265");

  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 1000,
      display: "flex", alignItems: "center", justifyContent: "center", padding: 20,
    }} onClick={onClose}>
      <div
        onClick={e => e.stopPropagation()}
        style={{
          background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 8,
          width: "100%", maxWidth: 520, maxHeight: "90vh", overflow: "auto",
          display: "flex", flexDirection: "column",
        }}
      >
        {/* Header */}
        <div style={{ padding: "14px 18px", borderBottom: "1px solid var(--border)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ display: "flex", flexDirection: "column" }}>
            <span style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>Node settings</span>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{node.name}</span>
          </div>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 18 }}>×</button>
        </div>

        <div style={{ padding: 18, display: "flex", flexDirection: "column", gap: 20 }}>
          {/* Pause */}
          <section>
            <Label title="Pause" hint="When paused, this node won't pick up new jobs. Already-running jobs continue." />
            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={!!settings.paused}
                onChange={e => setSettings({ ...settings, paused: e.target.checked })}
              />
              <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>
                Pause this node
              </span>
            </label>
          </section>

          {/* Parallel jobs */}
          <section>
            <Label title="Parallel jobs" hint="Max number of jobs this node processes concurrently." />
            <input
              type="number"
              min={1}
              max={32}
              value={settings.max_jobs ?? 1}
              onChange={e => setSettings({ ...settings, max_jobs: parseInt(e.target.value || "1") })}
              style={{
                width: 80, padding: "6px 10px", fontSize: 12,
                background: "var(--bg-primary)", border: "1px solid var(--border)",
                borderRadius: 4, color: "var(--text-primary)",
              }}
            />
          </section>

          {/* Job affinity */}
          <section>
            <Label title="Job affinity" hint="Control what jobs this node accepts. Useful for dedicating the Mac to CPU-only work or forcing NVENC jobs to the GPU node." />
            <select
              value={settings.job_affinity ?? "any"}
              onChange={e => setSettings({ ...settings, job_affinity: e.target.value as any })}
              style={{
                width: "100%", padding: "6px 10px", fontSize: 12,
                background: "var(--bg-primary)", border: "1px solid var(--border)",
                borderRadius: 4, color: "var(--text-primary)",
              }}
            >
              <option value="any">Any job (default)</option>
              <option value="cpu_only">Only CPU / libx265 jobs</option>
              <option value="nvenc_only">Only NVENC / GPU jobs</option>
            </select>
          </section>

          {/* Encoder translation */}
          <section>
            <Label title="Encoder translation" hint="When enabled, this node will transparently translate jobs between NVENC and libx265 based on its own capabilities." />
            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={!!settings.translate_encoder}
                onChange={e => setSettings({ ...settings, translate_encoder: e.target.checked })}
              />
              <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>
                Translate {hasNvenc && hasLibx265 ? "jobs between NVENC and libx265" : hasNvenc ? "libx265 jobs to NVENC" : "NVENC jobs to libx265"}
              </span>
            </label>
            <button
              onClick={() => setShowTable(!showTable)}
              style={{
                marginTop: 8, fontSize: 11, color: "var(--accent)", background: "none",
                border: "none", cursor: "pointer", padding: 0,
              }}
            >
              {showTable ? "Hide" : "Show"} NVENC ↔ libx265 comparison table
            </button>
            {showTable && (
              <div style={{ marginTop: 8, background: "var(--bg-primary)", borderRadius: 4, border: "1px solid var(--border)", overflow: "hidden" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
                  <thead>
                    <tr style={{ background: "rgba(255,255,255,0.02)" }}>
                      <th style={thStyle}>NVENC preset</th>
                      <th style={thStyle}>libx265 preset</th>
                      <th style={thStyle}>NVENC CQ</th>
                      <th style={thStyle}>libx265 CRF</th>
                    </tr>
                  </thead>
                  <tbody>
                    {CQ_CRF_TABLE.map(row => (
                      <tr key={row.nvenc_preset} style={{ borderTop: "1px solid var(--border)" }}>
                        <td style={tdStyle}>{row.nvenc_preset}</td>
                        <td style={tdStyle}>{row.libx265_preset}</td>
                        <td style={tdStyle}>CQ {row.nvenc_cq}</td>
                        <td style={tdStyle}>CRF {row.libx265_crf}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {/* Per-node schedule */}
          <section>
            <Label title="Schedule" hint="Restrict when this node accepts jobs. When disabled, the node runs 24/7." />
            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginBottom: 10 }}>
              <input
                type="checkbox"
                checked={!!settings.schedule_enabled}
                onChange={e => setSettings({ ...settings, schedule_enabled: e.target.checked })}
              />
              <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>Enable schedule</span>
            </label>
            {settings.schedule_enabled && (
              <div>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6 }}>
                  Click hours when this node is allowed to process jobs:
                </div>
                <div style={{ display: "grid", gridTemplateColumns: "repeat(12, 1fr)", gap: 3 }}>
                  {Array.from({ length: 24 }, (_, h) => {
                    const active = settings.schedule_hours?.includes(h);
                    return (
                      <button
                        key={h}
                        onClick={() => toggleHour(h)}
                        style={{
                          padding: "4px 0", fontSize: 10, cursor: "pointer",
                          background: active ? "var(--accent)" : "var(--bg-primary)",
                          color: active ? "white" : "var(--text-muted)",
                          border: "1px solid var(--border)", borderRadius: 3,
                        }}
                      >{h}</button>
                    );
                  })}
                </div>
                <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 6 }}>
                  {settings.schedule_hours && settings.schedule_hours.length > 0
                    ? `Active ${settings.schedule_hours.length} hour${settings.schedule_hours.length === 1 ? "" : "s"} per day`
                    : "No hours selected — node will never run while schedule is enabled"}
                </div>
              </div>
            )}
          </section>
        </div>

        {/* Footer */}
        <div style={{ padding: "12px 18px", borderTop: "1px solid var(--border)", display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 11, color: "var(--danger)" }}>
            {error || ""}
          </span>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn btn-secondary" onClick={onClose}>Cancel</button>
            <button className="btn btn-primary" onClick={save} disabled={saving}>
              {saving ? "Saving..." : "Save"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function Label({ title, hint }: { title: string; hint: string }) {
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-primary)" }}>{title}</div>
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2, lineHeight: 1.4 }}>{hint}</div>
    </div>
  );
}

const thStyle: React.CSSProperties = {
  padding: "6px 10px", textAlign: "left", fontWeight: 600,
  color: "var(--text-secondary)", fontSize: 10,
};
const tdStyle: React.CSSProperties = {
  padding: "6px 10px", color: "var(--text-secondary)", fontSize: 11,
};
