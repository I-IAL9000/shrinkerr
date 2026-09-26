import { useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { updateNodeSettings, rotateNodeToken } from "../api";
import type { WorkerNode, NodeSettings } from "../types";
import { fmtDateTime } from "../fmt";

interface Props {
  node: WorkerNode;
  onClose: () => void;
  onSaved: () => void;
}

// NVENC <-> libx265 equivalence used by the worker's translation logic.
// Targets similar *perceptual quality*: CRF = CQ one-to-one, so libx265's
// extra per-bit efficiency shows up as a smaller file at the same visual
// quality. Presets are capped at `slow` because libx265 preset perf scales
// exponentially (unlike NVENC p1..p7 which barely change GPU cost).
const CQ_CRF_TABLE: { nvenc_preset: string; libx265_preset: string; nvenc_cq: number; libx265_crf: number }[] = [
  { nvenc_preset: "p1", libx265_preset: "ultrafast", nvenc_cq: 20, libx265_crf: 20 },
  { nvenc_preset: "p2", libx265_preset: "superfast", nvenc_cq: 22, libx265_crf: 22 },
  { nvenc_preset: "p3", libx265_preset: "veryfast",  nvenc_cq: 24, libx265_crf: 24 },
  { nvenc_preset: "p4", libx265_preset: "fast",      nvenc_cq: 26, libx265_crf: 26 },
  { nvenc_preset: "p5", libx265_preset: "fast",      nvenc_cq: 27, libx265_crf: 27 },
  { nvenc_preset: "p6", libx265_preset: "medium",    nvenc_cq: 28, libx265_crf: 28 },
  { nvenc_preset: "p7", libx265_preset: "slow",      nvenc_cq: 30, libx265_crf: 30 },
];

export default function NodeSettingsModal({ node, onClose, onSaved }: Props) {
  const { t } = useTranslation(["nodes", "common"]);
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
  const [rotating, setRotating] = useState(false);
  const [rotateMessage, setRotateMessage] = useState<{ text: string; failed: boolean } | null>(null);

  // Path mappings override — tri-state.
  //   overrideActive=false → don't send the field; server keeps whatever
  //                          it currently has (override or null).
  //   overrideActive=true, touched=true → send the current rows array, OR
  //                                       null if the admin clicked "clear".
  // Starts populated from the node's current override (if set).
  const initialOverride = node.path_mappings_override ?? null;
  const [overrideActive, setOverrideActive] = useState(initialOverride !== null);
  const [overrideRows, setOverrideRows] = useState<{ server: string; worker: string }[]>(
    initialOverride ?? []
  );
  const [overrideTouched, setOverrideTouched] = useState(false);
  const markOverrideTouched = () => setOverrideTouched(true);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      // Build payload. path_mappings_override is tri-state — see declaration.
      const payload: NodeSettings = { ...settings };
      if (overrideTouched) {
        if (overrideActive) {
          // Drop blank rows and trim on client before sending.
          payload.path_mappings_override = overrideRows
            .map(r => ({ server: r.server.trim(), worker: r.worker.trim() }))
            .filter(r => r.server && r.worker);
        } else {
          payload.path_mappings_override = null;  // clear override on server
        }
      }
      await updateNodeSettings(node.id, payload);
      onSaved();  // closes modal + refreshes
    } catch (e: any) {
      setError(e?.message || t("nodes:settings.saveFailed"));
      setSaving(false);
    }
  };

  const rotateToken = async () => {
    if (!confirm(t("nodes:settings.token.rotateConfirm"))) return;
    setRotating(true);
    setRotateMessage(null);
    try {
      await rotateNodeToken(node.id);
      setRotateMessage({ text: t("nodes:settings.token.rotated"), failed: false });
    } catch (e: any) {
      setRotateMessage({ text: t("nodes:settings.token.rotateFailed", { error: e?.message || t("nodes:settings.token.unknownError") }), failed: true });
    } finally {
      setRotating(false);
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
            <span style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>{t("nodes:settings.title")}</span>
            <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{node.name}</span>
          </div>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 18 }}>×</button>
        </div>

        <div style={{ padding: 18, display: "flex", flexDirection: "column", gap: 20 }}>
          {/* Pause */}
          <section>
            <Label title={t("nodes:settings.pause.title")} hint={t("nodes:settings.pause.hint")} />
            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={!!settings.paused}
                onChange={e => setSettings({ ...settings, paused: e.target.checked })}
              />
              <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>
                {t("nodes:settings.pause.checkbox")}
              </span>
            </label>
          </section>

          {/* Parallel jobs */}
          <section>
            <Label title={t("nodes:settings.parallel.title")} hint={t("nodes:settings.parallel.hint")} />
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
            <Label title={t("nodes:settings.affinity.title")} hint={t("nodes:settings.affinity.hint")} />
            <select
              value={settings.job_affinity ?? "any"}
              onChange={e => setSettings({ ...settings, job_affinity: e.target.value as any })}
              style={{
                width: "100%", padding: "6px 10px", fontSize: 12,
                backgroundColor: "var(--bg-primary)", border: "1px solid var(--border)",
                borderRadius: 4, color: "var(--text-primary)",
              }}
            >
              <option value="any">{t("nodes:settings.affinity.any")}</option>
              <option value="cpu_only">{t("nodes:settings.affinity.cpuOnly")}</option>
              <option value="nvenc_only">{t("nodes:settings.affinity.nvencOnly")}</option>
            </select>
          </section>

          {/* Encoder translation */}
          <section>
            <Label title={t("nodes:settings.translation.title")} hint={t("nodes:settings.translation.hint")} />
            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer" }}>
              <input
                type="checkbox"
                checked={!!settings.translate_encoder}
                onChange={e => setSettings({ ...settings, translate_encoder: e.target.checked })}
              />
              <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>
                {hasNvenc && hasLibx265 ? t("nodes:settings.translation.both") : hasNvenc ? t("nodes:settings.translation.toNvenc") : t("nodes:settings.translation.toLibx265")}
              </span>
            </label>
            <button
              onClick={() => setShowTable(!showTable)}
              style={{
                marginTop: 8, fontSize: 11, color: "var(--accent)", background: "none",
                border: "none", cursor: "pointer", padding: 0,
              }}
            >
              {showTable ? t("nodes:settings.translation.hideTable") : t("nodes:settings.translation.showTable")}
            </button>
            {showTable && (
              <div style={{ marginTop: 8, background: "var(--bg-primary)", borderRadius: 4, border: "1px solid var(--border)", overflow: "hidden" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
                  <thead>
                    <tr style={{ background: "rgba(255,255,255,0.02)" }}>
                      <th style={thStyle}>{t("nodes:settings.translation.nvencPreset")}</th>
                      <th style={thStyle}>{t("nodes:settings.translation.libx265Preset")}</th>
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

          {/* Auth token (remote nodes only — local node runs in-process) */}
          {node.id !== "local" && (
            <section>
              <Label
                title={t("nodes:settings.token.title")}
                hint={t("nodes:settings.token.hint")}
              />
              <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 8, lineHeight: 1.5 }}>
                {node.has_token ? (
                  <>
                    <span style={{ color: "var(--success, #4caf50)" }}>● {t("nodes:settings.token.active")}</span>
                    {node.token_issued_at && (
                      <> &middot; {t("nodes:settings.token.issued", { date: fmtDateTime(node.token_issued_at) })}</>
                    )}
                  </>
                ) : (
                  <span style={{ color: "var(--text-muted)" }}>
                    {t("nodes:settings.token.none")}
                  </span>
                )}
              </div>
              <button
                onClick={rotateToken}
                disabled={rotating}
                className="btn btn-secondary"
                style={{ fontSize: 12 }}
              >
                {rotating ? t("nodes:settings.token.rotating") : t("nodes:settings.token.rotate")}
              </button>
              {rotateMessage && (
                <div style={{
                  marginTop: 8, fontSize: 11,
                  color: rotateMessage.failed ? "var(--danger)" : "var(--text-secondary)",
                }}>
                  {rotateMessage.text}
                </div>
              )}
            </section>
          )}

          {/* Path mappings override (remote nodes only). The local node runs
              in-process, so its paths are always identical to the server's. */}
          {node.id !== "local" && (
            <section>
              <Label
                title={t("nodes:settings.paths.title")}
                hint={t("nodes:settings.paths.hint")}
              />

              {/* Show the worker-reported mappings as informational — these
                  come from the worker's PATH_MAPPINGS env var on each
                  heartbeat. Useful to confirm the worker is alive + what it
                  thinks the mappings are even when you're about to override. */}
              {node.path_mappings && node.path_mappings.length > 0 && (
                <div style={{
                  fontSize: 11, color: "var(--text-muted)", marginBottom: 8,
                  padding: "6px 8px", background: "var(--bg-primary)",
                  border: "1px solid var(--border)", borderRadius: 4,
                }}>
                  <div style={{ fontWeight: 600, marginBottom: 2 }}>
                    {t("nodes:settings.paths.reported")}
                  </div>
                  {node.path_mappings.map((m, i) => (
                    <div key={i} style={{ fontFamily: "var(--font-mono)", fontSize: 10 }}>
                      {m.server} &rarr; {m.worker}
                    </div>
                  ))}
                </div>
              )}

              <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginBottom: 8 }}>
                <input
                  type="checkbox"
                  checked={overrideActive}
                  onChange={e => {
                    setOverrideActive(e.target.checked);
                    markOverrideTouched();
                    // When enabling for the first time with no existing rows,
                    // seed with a blank row so the user has somewhere to type.
                    if (e.target.checked && overrideRows.length === 0) {
                      setOverrideRows([{ server: "", worker: "" }]);
                    }
                  }}
                />
                <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>
                  {t("nodes:settings.paths.override")}
                </span>
              </label>

              {overrideActive && (
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  {overrideRows.map((row, i) => (
                    <div key={i} style={{ display: "flex", gap: 6, alignItems: "center" }}>
                      <input
                        type="text"
                        placeholder="/server/path"
                        value={row.server}
                        onChange={e => {
                          const next = [...overrideRows];
                          next[i] = { ...next[i], server: e.target.value };
                          setOverrideRows(next);
                          markOverrideTouched();
                        }}
                        style={{
                          flex: 1, padding: "4px 8px", fontSize: 11,
                          fontFamily: "var(--font-mono)",
                          background: "var(--bg-primary)", border: "1px solid var(--border)",
                          borderRadius: 4, color: "var(--text-primary)",
                        }}
                      />
                      <span style={{ color: "var(--text-muted)", fontSize: 12 }}>→</span>
                      <input
                        type="text"
                        placeholder="/worker/path"
                        value={row.worker}
                        onChange={e => {
                          const next = [...overrideRows];
                          next[i] = { ...next[i], worker: e.target.value };
                          setOverrideRows(next);
                          markOverrideTouched();
                        }}
                        style={{
                          flex: 1, padding: "4px 8px", fontSize: 11,
                          fontFamily: "var(--font-mono)",
                          background: "var(--bg-primary)", border: "1px solid var(--border)",
                          borderRadius: 4, color: "var(--text-primary)",
                        }}
                      />
                      <button
                        onClick={() => {
                          setOverrideRows(overrideRows.filter((_, idx) => idx !== i));
                          markOverrideTouched();
                        }}
                        title={t("common:actions.remove")}
                        style={{
                          background: "none", border: "none", color: "var(--text-muted)",
                          cursor: "pointer", fontSize: 16, padding: "0 6px",
                        }}
                      >×</button>
                    </div>
                  ))}
                  <button
                    onClick={() => {
                      setOverrideRows([...overrideRows, { server: "", worker: "" }]);
                      markOverrideTouched();
                    }}
                    style={{
                      alignSelf: "flex-start", fontSize: 11, color: "var(--accent)",
                      background: "none", border: "none", cursor: "pointer", padding: "4px 0",
                    }}
                  >{t("nodes:settings.paths.add")}</button>
                  <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4, lineHeight: 1.4 }}>
                    <Trans i18nKey="nodes:settings.paths.help" components={{ code: <code />, em: <em /> }} />
                  </div>
                </div>
              )}
            </section>
          )}

          {/* Per-node schedule */}
          <section>
            <Label title={t("nodes:settings.schedule.title")} hint={t("nodes:settings.schedule.hint")} />
            <label style={{ display: "flex", alignItems: "center", gap: 10, cursor: "pointer", marginBottom: 10 }}>
              <input
                type="checkbox"
                checked={!!settings.schedule_enabled}
                onChange={e => setSettings({ ...settings, schedule_enabled: e.target.checked })}
              />
              <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>{t("nodes:settings.schedule.enable")}</span>
            </label>
            {settings.schedule_enabled && (
              <div>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6 }}>
                  {t("nodes:settings.schedule.clickHours")}
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
                    ? t("nodes:settings.schedule.activeHours", { count: settings.schedule_hours.length })
                    : t("nodes:settings.schedule.noHours")}
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
            <button className="btn btn-secondary" onClick={onClose}>{t("common:actions.cancel")}</button>
            <button className="btn btn-primary" onClick={save} disabled={saving}>
              {saving ? t("nodes:settings.saving") : t("common:actions.save")}
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
