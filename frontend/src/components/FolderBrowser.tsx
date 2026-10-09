import { useState, useEffect, useRef } from "react";
import { useDialog } from "../useDialog";
import { useTranslation } from "react-i18next";
import { browseDirectory } from "../api";
import { pressable } from "../utils/a11y";

interface FolderBrowserProps {
  isOpen: boolean;
  initialPath?: string;
  onSelect: (path: string) => void;
  onCancel: () => void;
}

export default function FolderBrowser({ isOpen, initialPath = "/media", onSelect, onCancel }: FolderBrowserProps) {
  const { t } = useTranslation(["dialogs", "common"]);
  const panelRef = useRef<HTMLDivElement>(null);
  const dialog = useDialog(panelRef, onCancel, t("dialogs:folderBrowser.title"), isOpen);
  const [currentPath, setCurrentPath] = useState(initialPath);
  const [parentPath, setParentPath] = useState<string | null>(null);
  const [dirs, setDirs] = useState<{ name: string; path: string }[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pathInput, setPathInput] = useState(initialPath);

  useEffect(() => {
    if (isOpen) {
      navigate(initialPath);
    }
  }, [isOpen]);

  const navigate = async (path: string) => {
    setLoading(true);
    setError(null);
    try {
      const result = await browseDirectory(path);
      setCurrentPath(result.path);
      setParentPath(result.parent);
      setDirs(result.dirs || []);
      setPathInput(result.path);
      if (result.error) setError(result.error);
    } catch (e: any) {
      setError(e.message);
    }
    setLoading(false);
  };

  const handlePathSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    navigate(pathInput);
  };

  if (!isOpen) return null;

  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 1000,
      background: "rgba(0,0,0,0.7)", backdropFilter: "blur(4px)",
      display: "flex", alignItems: "center", justifyContent: "center",
    }} onClick={onCancel}>
      <div ref={panelRef} {...dialog} style={{
        background: "var(--bg-card)", borderRadius: 8, width: 560, maxWidth: "calc(100vw - 24px)", maxHeight: "80vh",
        display: "flex", flexDirection: "column", border: "1px solid var(--border)",
      }} onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div style={{
          padding: "16px 20px", borderBottom: "1px solid var(--border)",
          display: "flex", justifyContent: "space-between", alignItems: "center",
        }}>
          <span style={{ color: "white", fontWeight: "bold", fontSize: 15 }}>{t("dialogs:folderBrowser.title")}</span>
          <button onClick={onCancel} aria-label={t("common:actions.close")} style={{
            background: "none", border: "none", color: "var(--text-muted)",
            cursor: "pointer", fontSize: 18, lineHeight: 1,
          }}>&times;</button>
        </div>

        {/* Path input */}
        <form onSubmit={handlePathSubmit} style={{ padding: "12px 20px", borderBottom: "1px solid var(--border)" }}>
          <div style={{ display: "flex", gap: 8 }}>
            <input
              type="text"
              value={pathInput}
              onChange={(e) => setPathInput(e.target.value)}
              style={{
                flex: 1, padding: "8px 10px", fontSize: 13,
                background: "var(--bg-primary)", color: "var(--text-secondary)",
                border: "1px solid var(--border)", borderRadius: 4,
              }}
              aria-label={t("common:labels.folderPath")} placeholder="/path/to/media"
            />
            <button type="submit" className="btn btn-secondary" style={{ fontSize: 12, padding: "8px 12px" }}>{t("dialogs:folderBrowser.go")}</button>
          </div>
        </form>

        {/* Current path breadcrumb */}
        <div style={{ padding: "8px 20px", fontSize: 12, color: "var(--text-muted)", display: "flex", alignItems: "center", gap: 4 }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
          </svg>
          <span style={{ color: "var(--accent-text)" }}>{currentPath}</span>
        </div>

        {/* Directory listing */}
        <div style={{ flex: 1, overflowY: "auto", padding: "0 20px 12px" }}>
          {loading && <div style={{ padding: 20, textAlign: "center", color: "var(--text-muted)" }}>{t("common:status.loading")}</div>}
          {error && <div style={{ padding: 12, color: "var(--danger)", fontSize: 12 }}>{error}</div>}

          {!loading && (
            <div style={{ display: "flex", flexDirection: "column" }}>
              {/* Go up */}
              {parentPath && (
                <div
                  onClick={() => navigate(parentPath)}
                  {...pressable(() => navigate(parentPath))}
                  aria-label={t("dialogs:folderBrowser.up")}
                  style={{
                    display: "flex", alignItems: "center", gap: 10, padding: "10px 8px",
                    cursor: "pointer", borderBottom: "1px solid var(--border)",
                    fontSize: 13, color: "var(--text-muted)",
                  }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = "var(--bg-primary)")}
                  onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="15 18 9 12 15 6"/>
                  </svg>
                  <span>..</span>
                </div>
              )}

              {dirs.map((dir) => (
                <div
                  key={dir.path}
                  onClick={() => navigate(dir.path)}
                  {...pressable(() => navigate(dir.path))}
                  style={{
                    display: "flex", alignItems: "center", gap: 10, padding: "10px 8px",
                    cursor: "pointer", borderBottom: "1px solid var(--border)",
                    fontSize: 13, color: "var(--text-secondary)",
                  }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = "var(--bg-primary)")}
                  onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
                    <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
                  </svg>
                  <span>{dir.name}</span>
                </div>
              ))}

              {!loading && dirs.length === 0 && !error && (
                <div style={{ padding: 20, textAlign: "center", color: "var(--text-muted)", fontSize: 12 }}>{t("dialogs:folderBrowser.noSubdirectories")}</div>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div style={{
          padding: "12px 20px", borderTop: "1px solid var(--border)",
          display: "flex", justifyContent: "space-between", alignItems: "center",
        }}>
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
            {t("dialogs:folderBrowser.selected")} <span style={{ color: "var(--accent-text)" }}>{currentPath}</span>
          </span>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn btn-secondary" onClick={onCancel} style={{ padding: "8px 16px" }}>{t("common:actions.cancel")}</button>
            <button className="btn btn-primary" onClick={() => onSelect(currentPath)} style={{ padding: "8px 16px" }}>{t("dialogs:folderBrowser.select")}</button>
          </div>
        </div>
      </div>
    </div>
  );
}
