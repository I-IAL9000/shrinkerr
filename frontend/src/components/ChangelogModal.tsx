import { useEffect, useState } from "react";
import { getChangelog, type ChangelogEntry } from "../api";
import ChangelogEntryView from "./ChangelogEntry";

/**
 * Modal dialog that shows the latest release's changelog entries, plus a
 * clear "update available" banner and a link to the GitHub release page.
 *
 * Opens from the sidebar's "Update available" button (VersionBadge) and
 * closes on Escape or backdrop click. The "Release notes" variant
 * (showLatestOnly=false) shows the full parsed changelog for use by the
 * Settings → Updates section's "View full history" affordance.
 */
export default function ChangelogModal({
  open,
  onClose,
  latestVersion,
  showLatestOnly = true,
}: {
  open: boolean;
  onClose: () => void;
  /** The version string we think is newer than what's currently running. */
  latestVersion?: string | null;
  /** When true, show only the first (latest) entry. When false, show the whole file. */
  showLatestOnly?: boolean;
}) {
  const [entries, setEntries] = useState<ChangelogEntry[] | null>(null);
  const [current, setCurrent] = useState<string>("");
  const [error, setError] = useState(false);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    getChangelog(showLatestOnly ? 1 : 0)
      .then((r) => { if (!cancelled) { setEntries(r.entries || []); setCurrent(r.current || ""); } })
      .catch(() => { if (!cancelled) setError(true); });
    return () => { cancelled = true; };
  }, [open, showLatestOnly]);

  // Close on Escape.
  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);

  if (!open) return null;

  const repoUrl = "https://github.com/I-IAL9000/shrinkerr";
  const releasesUrl = `${repoUrl}/releases`;
  const latestReleaseUrl = latestVersion
    ? `${repoUrl}/releases/tag/v${latestVersion}`
    : releasesUrl;

  return (
    <div
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
      style={{
        position: "fixed", inset: 0, zIndex: 1000,
        background: "rgba(0,0,0,0.6)",
        display: "flex", alignItems: "center", justifyContent: "center",
        padding: 20,
      }}
    >
      <div
        style={{
          background: "var(--bg-primary)",
          border: "1px solid var(--border)",
          borderRadius: 8,
          width: "100%", maxWidth: 640,
          maxHeight: "90vh", display: "flex", flexDirection: "column",
        }}
      >
        {/* Header */}
        <div style={{
          padding: "16px 20px", borderBottom: "1px solid var(--border)",
          display: "flex", alignItems: "center", justifyContent: "space-between",
        }}>
          <div>
            <div style={{ fontSize: 16, fontWeight: 600, color: "var(--text-primary)" }}>
              {showLatestOnly && latestVersion ? "Update available" : "Release notes"}
            </div>
            {showLatestOnly && latestVersion && current && (
              <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
                You're on <strong>v{current}</strong> · latest is <strong style={{ color: "var(--accent)" }}>v{latestVersion}</strong>
              </div>
            )}
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            style={{
              background: "none", border: "none", cursor: "pointer",
              color: "var(--text-muted)", fontSize: 22, padding: 4, lineHeight: 1,
            }}
          >&times;</button>
        </div>

        {/* Body */}
        <div style={{ padding: "16px 20px", overflowY: "auto", flex: 1 }}>
          {error && (
            <div style={{ color: "var(--text-muted)", fontSize: 13, padding: 24, textAlign: "center" }}>
              Couldn't load the changelog. The <a href={releasesUrl} target="_blank" rel="noopener noreferrer" style={{ color: "var(--accent)" }}>GitHub releases page</a> has the full history.
            </div>
          )}
          {!error && entries === null && (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 10, padding: 40 }}>
              <div className="spinner" style={{ width: 18, height: 18 }} />
              <span style={{ color: "var(--text-muted)", fontSize: 13 }}>Loading release notes…</span>
            </div>
          )}
          {!error && entries && entries.length === 0 && (
            <div style={{ color: "var(--text-muted)", fontSize: 13, padding: 24, textAlign: "center" }}>
              No changelog entries found. Check the <a href={releasesUrl} target="_blank" rel="noopener noreferrer" style={{ color: "var(--accent)" }}>GitHub releases page</a>.
            </div>
          )}
          {!error && entries && entries.length > 0 && entries.map((e, i) => (
            <ChangelogEntryView key={e.version} entry={e} highlight={i === 0 && showLatestOnly} />
          ))}
        </div>

        {/* Footer */}
        <div style={{
          padding: "12px 20px", borderTop: "1px solid var(--border)",
          display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12,
        }}>
          <a
            href={latestReleaseUrl}
            target="_blank"
            rel="noopener noreferrer"
            style={{ fontSize: 12, color: "var(--text-muted)", textDecoration: "none" }}
          >
            View on GitHub ↗
          </a>
          <button className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 14px" }} onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
