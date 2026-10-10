import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { arrActionBulk, getReplacePlan, type ArrActionBulkResult, type ReplacePlanFile } from "../api";
import { useDialog } from "../useDialog";

/**
 * Scanner → Corrupt → "Replace via Sonarr/Radarr" (v0.10.0). A dry run of
 * every file the health check found corrupt: what Sonarr / Radarr would
 * blocklist, delete and search for. Nothing changes until the ticked ones
 * are replaced.
 */
export default function ReplaceCorruptModal({ onClose, onReplaced }: { onClose: () => void; onReplaced: () => void }) {
  const { t } = useTranslation(["scannerModals", "common"]);
  const panelRef = useRef<HTMLDivElement>(null);
  const dialog = useDialog(panelRef, onClose, t("scannerModals:replaceCorrupt.title"));
  const [plan, setPlan] = useState<{ files: ReplacePlanFile[]; truncated: boolean } | null>(null);
  const [error, setError] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<Record<string, { success: boolean; error?: string }>>({});

  useEffect(() => {
    getReplacePlan()
      .then(p => {
        setPlan(p);
        setPicked(new Set(p.files.filter(f => f.success).map(f => f.file_path)));
      })
      .catch(e => setError((e as Error).message));
  }, []);

  const ready = (plan?.files || []).filter(f => f.success && !done[f.file_path]);
  const toggle = (path: string) => setPicked(prev => {
    const next = new Set(prev);
    if (next.has(path)) next.delete(path); else next.add(path);
    return next;
  });
  const count = ready.filter(f => picked.has(f.file_path)).length;

  const replace = async () => {
    const paths = ready.filter(f => picked.has(f.file_path)).map(f => f.file_path);
    if (!paths.length || busy) return;
    setBusy(true);
    try {
      const res = await arrActionBulk(paths, "replace", true) as ArrActionBulkResult;
      setDone(prev => ({ ...prev, ...Object.fromEntries(res.results.map(r => [r.file_path, { success: !!r.success, error: r.error }])) }));
      onReplaced();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const target = (f: ReplacePlanFile) => f.service === "sonarr"
    ? t("scannerModals:replaceCorrupt.sonarr", { series: f.series, episodes: (f.episodes || []).join(", ") })
    : t("scannerModals:replaceCorrupt.radarr", { movie: f.year ? `${f.movie} (${f.year})` : f.movie });
  const name = (path: string) => path.split("/").pop() || path;

  return (
    <div onClick={busy ? undefined : onClose} style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.6)", zIndex: 1000,
      display: "flex", alignItems: "center", justifyContent: "center", padding: 20,
    }}>
      <div ref={panelRef} {...dialog} onClick={e => e.stopPropagation()} style={{
        background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 8,
        width: "100%", maxWidth: 760, maxHeight: "90vh", overflow: "hidden", display: "flex", flexDirection: "column",
      }}>
        <div style={{ padding: "14px 18px", borderBottom: "1px solid var(--border)", display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
          <div>
            <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>{t("scannerModals:replaceCorrupt.title")}</div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 4 }}>{t("scannerModals:replaceCorrupt.intro")}</div>
          </div>
          <button onClick={onClose} disabled={busy} aria-label={t("common:actions.close")} style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 20 }}>×</button>
        </div>

        <div style={{ padding: "10px 18px", overflowY: "auto", flex: 1, fontSize: 12 }}>
          {error && <div role="alert" style={{ color: "var(--danger)", marginBottom: 8 }}>{error}</div>}
          {!plan && !error && <div role="status" style={{ color: "var(--text-muted)" }}>{t("scannerModals:replaceCorrupt.loading")}</div>}
          {plan && plan.files.length === 0 && <div role="status" style={{ color: "var(--text-muted)" }}>{t("scannerModals:replaceCorrupt.none")}</div>}
          {plan?.truncated && <div style={{ color: "var(--caution)", marginBottom: 8 }}>{t("scannerModals:replaceCorrupt.truncated")}</div>}
          <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
            {plan?.files.map(f => {
              const result = done[f.file_path];
              return (
                <li key={f.file_path} style={{ display: "flex", gap: 10, padding: "8px 0", borderBottom: "1px solid var(--border)" }}>
                  <input type="checkbox" aria-label={name(f.file_path)} checked={f.success && picked.has(f.file_path)}
                    disabled={!f.success || !!result || busy} onChange={() => toggle(f.file_path)} style={{ marginTop: 2 }} />
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div title={f.file_path} style={{ color: "var(--text-primary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{name(f.file_path)}</div>
                    {f.success ? (
                      <div style={{ color: "var(--text-muted)", marginTop: 2 }}>
                        {target(f)}
                        {" · "}
                        {f.release ? t("scannerModals:replaceCorrupt.release", { release: f.release }) : t("scannerModals:replaceCorrupt.noRelease")}
                        {f.deletes === false && <> · <span style={{ color: "var(--caution)" }}>{t("scannerModals:replaceCorrupt.keepsFile")}</span></>}
                      </div>
                    ) : (
                      <div style={{ color: "var(--danger)", marginTop: 2 }}>{t("scannerModals:replaceCorrupt.cantReplace", { error: f.error })}</div>
                    )}
                    {result && (
                      <div role="status" style={{ marginTop: 2, color: result.success ? "var(--success)" : "var(--danger)" }}>
                        {result.success ? t("scannerModals:replaceCorrupt.replaced") : t("scannerModals:replaceCorrupt.failed", { error: result.error })}
                      </div>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </div>

        <div style={{ padding: "12px 18px", borderTop: "1px solid var(--border)", display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button className="btn btn-secondary" onClick={onClose} disabled={busy}>{t("common:actions.close")}</button>
          <button className="btn btn-primary" onClick={replace} disabled={busy || count === 0}
            style={count > 0 ? { background: "var(--danger)", borderColor: "var(--danger)" } : undefined}>
            {busy ? t("scannerModals:replaceCorrupt.replacing") : t("scannerModals:replaceCorrupt.replace", { count })}
          </button>
        </div>
      </div>
    </div>
  );
}
