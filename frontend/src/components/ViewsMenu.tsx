import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { deleteView, getViews, saveView, type SavedView } from "../api";
import { encodeAdvanced } from "../advancedSearch";
import type { SearchPredicate } from "../api";
import { useToast } from "../useToast";

// Views Advanced Search kept in this browser before views moved to the server.
const OLD_VIEWS_KEY = "shrinkerr_saved_search_views";

async function moveBrowserViews(): Promise<void> {
  let old: { name: string; predicates: SearchPredicate[] }[] = [];
  try {
    old = JSON.parse(localStorage.getItem(OLD_VIEWS_KEY) || "[]");
  } catch {
    return;
  }
  if (!Array.isArray(old) || old.length === 0) return;
  for (const v of old) {
    if (v?.name && Array.isArray(v.predicates) && v.predicates.length) {
      await saveView(v.name, encodeAdvanced({ m: "all", p: v.predicates })).catch(() => {});
    }
  }
  try { localStorage.removeItem(OLD_VIEWS_KEY); } catch { /* ignore */ }
}

/**
 * Saved views (v0.10.0): name the Scanner's current filter — pills and
 * Advanced Search — and apply it again in one click. Kept on the server, so
 * rules and the auto-queue can use them too.
 */
export default function ViewsMenu({ filter, onApply }: { filter: string; onApply: (filter: string) => void }) {
  const { t } = useTranslation(["scanner", "common"]);
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [views, setViews] = useState<SavedView[]>([]);
  const [name, setName] = useState("");
  const ref = useRef<HTMLDivElement>(null);

  const load = () => getViews().then(setViews).catch(() => {});
  useEffect(() => { moveBrowserViews().finally(load); }, []);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [open]);

  const active = views.find(v => v.filter === filter);
  const save = async () => {
    const n = name.trim();
    if (!n) return;
    try {
      await saveView(n, filter);
      setName("");
      toast(t("scanner:views.saved", { name: n }), "success");
      load();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };
  const remove = async (v: SavedView) => {
    try {
      await deleteView(v.id);
      load();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  };

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button className="sort-pill" aria-expanded={open} aria-haspopup="true" onClick={() => setOpen(o => !o)}
        title={t("scanner:views.title")}
        style={{
          display: "inline-flex", alignItems: "center", gap: 5, whiteSpace: "nowrap",
          background: active ? "var(--accent-bg)" : undefined,
          color: active ? "var(--accent-text)" : undefined,
          borderColor: active ? "var(--accent-text)" : undefined,
        }}>
        {active ? active.name : t("scanner:views.button")} ▾
      </button>
      {open && (
        <div role="dialog" aria-label={t("scanner:views.title")}
          style={{ position: "absolute", top: "calc(100% + 4px)", left: 0, zIndex: 100, minWidth: 260, padding: 8,  // above the sticky selection bar (50)
                   background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 8,
                   boxShadow: "0 8px 24px rgba(0,0,0,0.3)" }}>
          {views.length === 0 && (
            <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "4px 6px 8px" }}>{t("scanner:views.none")}</div>
          )}
          {views.map(v => (
            <div key={v.id} style={{ display: "flex", alignItems: "center", gap: 4 }}>
              <button onClick={() => { onApply(v.filter); setOpen(false); }}
                style={{ flex: 1, textAlign: "left", background: v.id === active?.id ? "var(--accent-bg)" : "none", border: "none",
                         color: "var(--text-primary)", cursor: "pointer", fontSize: 12, padding: "5px 6px", borderRadius: 4 }}>
                {v.name}
              </button>
              <button aria-label={t("scanner:views.delete", { name: v.name })} title={t("scanner:views.delete", { name: v.name })}
                onClick={() => remove(v)}
                style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: 14, padding: "0 6px" }}>
                &times;
              </button>
            </div>
          ))}
          <div style={{ display: "flex", gap: 6, marginTop: 8, paddingTop: 8, borderTop: "1px solid var(--border)" }}>
            <input value={name} onChange={e => setName(e.target.value)} onKeyDown={e => { if (e.key === "Enter") save(); }}
              aria-label={t("scanner:views.namePlaceholder")} placeholder={t("scanner:views.namePlaceholder")}
              disabled={filter === "all"}
              style={{ flex: 1, minWidth: 0, padding: "5px 8px", fontSize: 12, background: "var(--bg-primary)",
                       color: "var(--text-secondary)", border: "1px solid var(--border)", borderRadius: 4 }} />
            <button className="btn btn-secondary" onClick={save} disabled={filter === "all" || !name.trim()}
              style={{ fontSize: 11, padding: "4px 10px", opacity: filter === "all" || !name.trim() ? 0.5 : 1 }}>
              {t("scanner:views.save")}
            </button>
          </div>
          {filter === "all" && (
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 6 }}>{t("scanner:views.pickFirst")}</div>
          )}
        </div>
      )}
    </div>
  );
}
