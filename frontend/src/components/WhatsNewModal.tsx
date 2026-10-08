import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { getWhatsNew, markWhatsNewSeen, type WhatsNew } from "../api";
import ChangelogModal from "./ChangelogModal";

/**
 * Shown once after an update (v0.10.0): one line per new feature, then the
 * headline fixes and a count of the smaller ones. The server decides when
 * there is something to show; any way of closing marks it seen.
 */
export default function WhatsNewModal() {
  const { t } = useTranslation(["dialogs", "common"]);
  const [notes, setNotes] = useState<WhatsNew | null>(null);
  const [fullNotes, setFullNotes] = useState(false);

  // `?whats-new` in the address bar previews the dialog without marking it
  // seen — on a development build with the unreleased notes.
  const preview = new URLSearchParams(window.location.search).has("whats-new");

  useEffect(() => {
    getWhatsNew(preview).then(n => { if (n.show) setNotes(n); }).catch(() => {});
  }, []);

  const close = () => {
    setNotes(null);
    if (!preview) markWhatsNewSeen().catch(() => {});
  };

  useEffect(() => {
    if (!notes) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [notes]);

  if (fullNotes) {
    return <ChangelogModal open onClose={() => setFullNotes(false)} showLatestOnly={false} />;
  }
  if (!notes) return null;

  const section = (title: string, items: string[]) => items.length > 0 && (
    <div style={{ marginBottom: 16 }}>
      <div style={{ fontSize: 12, fontWeight: 600, color: "var(--accent)", textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>
        {title}
      </div>
      <ul style={{ margin: 0, paddingLeft: 18, display: "flex", flexDirection: "column", gap: 4 }}>
        {items.map((item, i) => (
          <li key={i} style={{ fontSize: 13, color: "var(--text-secondary)" }}>{item}</li>
        ))}
      </ul>
    </div>
  );

  return (
    <div
      onClick={(e) => { if (e.target === e.currentTarget) close(); }}
      style={{
        position: "fixed", inset: 0, zIndex: 1000, background: "rgba(0,0,0,0.6)",
        display: "flex", alignItems: "center", justifyContent: "center", padding: 20,
      }}
    >
      <div style={{
        background: "var(--bg-primary)", border: "1px solid var(--border)", borderRadius: 8,
        width: "100%", maxWidth: 560, maxHeight: "90vh", display: "flex", flexDirection: "column",
      }}>
        <div style={{ padding: "16px 20px", borderBottom: "1px solid var(--border)" }}>
          <div style={{ fontSize: 16, fontWeight: 600, color: "var(--text-primary)" }}>
            {t("dialogs:whatsNew.title", { version: notes.version })}
          </div>
        </div>
        <div style={{ padding: "16px 20px", overflowY: "auto", flex: 1 }}>
          {section(t("dialogs:whatsNew.new"), notes.new)}
          {section(t("dialogs:whatsNew.fixed"), notes.fixed)}
          {notes.more_fixes > 0 && (
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              {t(notes.fixed.length > 0 ? "dialogs:whatsNew.moreFixes" : "dialogs:whatsNew.fixes", { count: notes.more_fixes })}
            </div>
          )}
        </div>
        <div style={{
          padding: "12px 20px", borderTop: "1px solid var(--border)",
          display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
        }}>
          <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 14px" }}
            onClick={() => { close(); setFullNotes(true); }}>
            {t("dialogs:whatsNew.fullNotes")}
          </button>
          <button className="btn btn-primary" style={{ fontSize: 12, padding: "6px 18px" }} onClick={close}>
            {t("dialogs:whatsNew.gotIt")}
          </button>
        </div>
      </div>
    </div>
  );
}
