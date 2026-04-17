import { useState, useEffect } from "react";
import { previewRename, applyRename } from "../api";
import type { RenamePlan } from "../api";

interface Props {
  filePaths: string[];
  onClose: () => void;
  onApplied: () => void;
}

export default function RenameModal({ filePaths, onClose, onApplied }: Props) {
  const [plans, setPlans] = useState<RenamePlan[]>([]);
  const [loading, setLoading] = useState(true);
  const [applying, setApplying] = useState(false);
  const [rescanArr, setRescanArr] = useState(true);
  const [rescanPlex, setRescanPlex] = useState(true);
  const [result, setResult] = useState<any>(null);

  useEffect(() => {
    setLoading(true);
    previewRename(filePaths)
      .then(r => setPlans(r.plans))
      .finally(() => setLoading(false));
  }, [filePaths]);

  const changedPlans = plans.filter(p => p.changed);
  const noopCount = plans.length - changedPlans.length;

  const apply = async () => {
    setApplying(true);
    try {
      const r = await applyRename(
        changedPlans.map(p => p.old_path),
        { rescan_arr: rescanArr, rescan_plex: rescanPlex },
      );
      setResult(r);
      setApplying(false);
      // Leave the modal open briefly so user sees the result summary
      setTimeout(() => {
        onApplied();
      }, 1500);
    } catch (e: any) {
      setApplying(false);
      setResult({ error: e?.message || "Apply failed" });
    }
  };

  const basename = (p: string) => p.split("/").pop() || p;

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 1000,
        display: "flex", alignItems: "center", justifyContent: "center", padding: 20,
      }}
    >
      <div
        onClick={e => e.stopPropagation()}
        style={{
          background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 8,
          width: "100%", maxWidth: 900, maxHeight: "90vh",
          display: "flex", flexDirection: "column",
        }}
      >
        <div style={{ padding: "14px 18px", borderBottom: "1px solid var(--border)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>Rename preview</div>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
              {loading ? "Resolving metadata..." : `${changedPlans.length} file${changedPlans.length === 1 ? "" : "s"} will change · ${noopCount} already match the pattern`}
            </div>
          </div>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 20 }}>×</button>
        </div>

        <div style={{ padding: 14, overflow: "auto", flex: 1 }}>
          {loading ? (
            <div style={{ display: "flex", alignItems: "center", gap: 8, padding: 20, color: "var(--text-muted)" }}>
              <div className="spinner" style={{ width: 14, height: 14 }} /> Resolving renames via Sonarr/Radarr/TMDB...
            </div>
          ) : plans.length === 0 ? (
            <div style={{ padding: 20, color: "var(--text-muted)", textAlign: "center" }}>No files selected</div>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ borderBottom: "1px solid var(--border)" }}>
                  <th style={{ padding: "6px 8px", textAlign: "left", color: "var(--text-muted)", fontSize: 10, textTransform: "uppercase" }}>Current</th>
                  <th style={{ padding: "6px 8px", textAlign: "left", color: "var(--text-muted)", fontSize: 10, textTransform: "uppercase" }}>New</th>
                  <th style={{ padding: "6px 8px", width: 80, color: "var(--text-muted)", fontSize: 10, textTransform: "uppercase" }}>Status</th>
                </tr>
              </thead>
              <tbody>
                {plans.map((p, i) => (
                  <tr key={i} style={{ borderBottom: "1px solid var(--border)", opacity: p.changed ? 1 : 0.5 }}>
                    <td style={{ padding: "6px 8px", fontFamily: "var(--font-mono)", wordBreak: "break-all" }} title={p.old_path}>
                      {basename(p.old_path)}
                    </td>
                    <td style={{ padding: "6px 8px", fontFamily: "var(--font-mono)", wordBreak: "break-all", color: p.changed ? "var(--accent)" : "var(--text-muted)" }} title={p.new_path}>
                      {p.error ? <span style={{ color: "var(--danger)" }}>{p.error}</span> : basename(p.new_path)}
                    </td>
                    <td style={{ padding: "6px 8px", fontSize: 10 }}>
                      {p.error ? (
                        <span style={{ color: "var(--danger)" }}>Error</span>
                      ) : p.changed ? (
                        <span style={{ color: "var(--success)" }}>Rename</span>
                      ) : (
                        <span style={{ color: "var(--text-muted)" }}>Unchanged</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {result && (
            <div style={{ marginTop: 14, padding: 12, background: result.error ? "rgba(239,68,68,0.1)" : "rgba(16,185,129,0.1)", borderRadius: 4, fontSize: 12 }}>
              {result.error ? (
                <span style={{ color: "var(--danger)" }}>{result.error}</span>
              ) : (
                <span style={{ color: "var(--success)" }}>
                  Renamed {result.results?.filter((r: any) => r.applied).length || 0} file(s).{" "}
                  {result.rescans?.arr?.length > 0 && `Sonarr/Radarr rescan triggered. `}
                  {result.rescans?.plex?.length > 0 && `Plex scan triggered.`}
                </span>
              )}
            </div>
          )}
        </div>

        <div style={{ padding: "12px 18px", borderTop: "1px solid var(--border)", display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
          <div style={{ display: "flex", gap: 16, fontSize: 11, color: "var(--text-secondary)" }}>
            <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
              <input type="checkbox" checked={rescanArr} onChange={e => setRescanArr(e.target.checked)} />
              Rescan Sonarr/Radarr
            </label>
            <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
              <input type="checkbox" checked={rescanPlex} onChange={e => setRescanPlex(e.target.checked)} />
              Rescan Plex
            </label>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn btn-secondary" onClick={onClose}>Cancel</button>
            <button
              className="btn btn-primary"
              onClick={apply}
              disabled={applying || loading || changedPlans.length === 0 || !!result}
            >
              {applying ? "Renaming..." : result ? "Done" : `Rename ${changedPlans.length} file${changedPlans.length === 1 ? "" : "s"}`}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
