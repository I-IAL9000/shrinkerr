import { useEffect, useState } from "react";
import { getChangelog, getUpstreamChangelog, getVersion, type ChangelogEntry } from "../api";
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
  /** When true, show upstream entries newer than current. When false, show the whole local file. */
  showLatestOnly?: boolean;
}) {
  const [entries, setEntries] = useState<ChangelogEntry[] | null>(null);
  const [current, setCurrent] = useState<string>("");
  // Latest version reported by the backend at the time the modal opened.
  // Pre-v0.3.66 we used the prop value, which could be stale (cached for
  // up to 30 min by the background version-check). Now we force-refresh
  // when the modal opens so a hard refresh shows the truly-latest tag.
  const [resolvedLatest, setResolvedLatest] = useState<string | null>(latestVersion ?? null);
  // "github" when we got upstream release entries; "local" when GitHub
  // was unreachable and we fell back to the installed CHANGELOG.md.
  const [source, setSource] = useState<"github" | "local" | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;

    if (showLatestOnly) {
      // "Update available" path: fetch GitHub release entries newer than
      // current, AND force-refresh the version check so the header shows
      // the freshest "latest is vX.Y.Z" value (not the cached one that
      // made the user see v0.3.64 when v0.3.65 was already out). Both
      // are server-side cached separately so the GitHub round-trip is
      // amortised across users hitting refresh in close succession.
      Promise.all([
        getUpstreamChangelog(true),
        getVersion(true).catch(() => null),
      ])
        .then(([r, v]) => {
          if (cancelled) return;
          setEntries(r.entries || []);
          setCurrent(r.current || "");
          setSource(r.source);
          if (v?.latest) setResolvedLatest(v.latest);
        })
        .catch(() => { if (!cancelled) setError(true); });
    } else {
      // "Release notes" path (Settings → Updates → View full history):
      // local file is correct here — the user wants to see what's in
      // the version they're running.
      getChangelog(0)
        .then((r) => {
          if (cancelled) return;
          setEntries(r.entries || []);
          setCurrent(r.current || "");
          setSource("local");
        })
        .catch(() => { if (!cancelled) setError(true); });
    }
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
  const headerLatest = resolvedLatest ?? latestVersion ?? null;
  const latestReleaseUrl = headerLatest
    ? `${repoUrl}/releases/tag/v${headerLatest}`
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
              {showLatestOnly && headerLatest ? "Update available" : "Release notes"}
            </div>
            {showLatestOnly && headerLatest && current && (
              <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
                You're on <strong>v{current}</strong> · latest is <strong style={{ color: "var(--accent)" }}>v{headerLatest}</strong>
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
          {/* Visible notice when we couldn't reach GitHub and fell back to
              the locally-bundled CHANGELOG.md. Pre-v0.3.66 the modal
              silently rendered local entries with the topmost one
              labelled "LATEST", which lied to users on stale containers
              (current=0.3.63, top entry=0.3.63 — but a real newer release
              existed upstream). */}
          {!error && showLatestOnly && source === "local" && (
            <div style={{
              fontSize: 12, color: "var(--text-muted)",
              padding: "8px 10px", marginBottom: 12, borderRadius: 4,
              background: "rgba(255,169,77,0.06)",
              border: "1px solid rgba(255,169,77,0.25)",
            }}>
              Couldn't reach GitHub to fetch the new release notes — showing your installed
              CHANGELOG.md instead. The <a href={releasesUrl} target="_blank" rel="noopener noreferrer" style={{ color: "var(--accent)" }}>releases page</a> has the latest entries.
            </div>
          )}
          {/* The LATEST badge lights up on the entry whose version equals
              the resolved upstream latest. Pre-v0.3.66 it was hardcoded to
              `i === 0`, which on a stale container marked the user's
              own installed version as LATEST — confusing. */}
          {!error && entries && entries.length > 0 && entries.map((e) => (
            <ChangelogEntryView
              key={e.version}
              entry={e}
              highlight={showLatestOnly && headerLatest != null && e.version === headerLatest}
            />
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
