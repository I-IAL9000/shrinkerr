import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { previewRename, applyRename } from "../api";
import type { RenamePlan } from "../api";

interface Props {
  filePaths: string[];
  onClose: () => void;
  onApplied: () => void;
}

export default function RenameModal({ filePaths, onClose, onApplied }: Props) {
  const { t } = useTranslation(["scannerModals", "common"]);
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
      setResult({ error: e?.message || t("scannerModals:rename.applyFailed") });
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
            <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>{t("scannerModals:rename.title")}</div>
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
              {loading ? t("scannerModals:rename.resolvingMetadata") : `${t("scannerModals:rename.willChange", { count: changedPlans.length })} · ${t("scannerModals:rename.alreadyMatch", { count: noopCount })}`}
            </div>
          </div>
          <button onClick={onClose} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 20 }}>×</button>
        </div>

        <div style={{ padding: 14, overflow: "auto", flex: 1 }}>
          {loading ? (
            <div style={{ display: "flex", alignItems: "center", gap: 8, padding: 20, color: "var(--text-muted)" }}>
              <div className="spinner" style={{ width: 14, height: 14 }} /> {t("scannerModals:rename.resolving")}
            </div>
          ) : plans.length === 0 ? (
            <div style={{ padding: 20, color: "var(--text-muted)", textAlign: "center" }}>{t("scannerModals:rename.noFiles")}</div>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
              <thead>
                <tr style={{ borderBottom: "1px solid var(--border)" }}>
                  <th style={{ padding: "6px 8px", textAlign: "left", color: "var(--text-muted)", fontSize: 10, textTransform: "uppercase" }}>{t("scannerModals:rename.colCurrent")}</th>
                  <th style={{ padding: "6px 8px", textAlign: "left", color: "var(--text-muted)", fontSize: 10, textTransform: "uppercase" }}>{t("scannerModals:rename.colNew")}</th>
                  <th style={{ padding: "6px 8px", width: 80, color: "var(--text-muted)", fontSize: 10, textTransform: "uppercase" }}>{t("scannerModals:rename.colStatus")}</th>
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
                        <span style={{ color: "var(--danger)" }}>{t("scannerModals:rename.statusError")}</span>
                      ) : p.changed ? (
                        <span style={{ color: "var(--success)" }}>{t("scannerModals:rename.statusRename")}</span>
                      ) : (
                        <span style={{ color: "var(--text-muted)" }}>{t("scannerModals:rename.statusUnchanged")}</span>
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
                  {t("scannerModals:rename.renamed", { count: result.results?.filter((r: any) => r.applied).length || 0 })}{" "}
                  {result.rescans?.arr?.length > 0 && `${t("scannerModals:rename.arrRescanTriggered")} `}
                  {result.rescans?.plex?.length > 0 && t("scannerModals:rename.plexScanTriggered")}
                </span>
              )}
            </div>
          )}
        </div>

        <div style={{ padding: "12px 18px", borderTop: "1px solid var(--border)", display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
          <div style={{ display: "flex", gap: 16, fontSize: 11, color: "var(--text-secondary)" }}>
            <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
              <input type="checkbox" checked={rescanArr} onChange={e => setRescanArr(e.target.checked)} />
              {t("scannerModals:rename.rescanArr")}
            </label>
            <label style={{ display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
              <input type="checkbox" checked={rescanPlex} onChange={e => setRescanPlex(e.target.checked)} />
              {t("scannerModals:rename.rescanPlex")}
            </label>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn btn-secondary" onClick={onClose}>{t("common:actions.cancel")}</button>
            <button
              className="btn btn-primary"
              onClick={apply}
              disabled={applying || loading || changedPlans.length === 0 || !!result}
            >
              {applying ? t("scannerModals:rename.renaming") : result ? t("scannerModals:rename.done") : t("scannerModals:rename.renameFiles", { count: changedPlans.length })}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
