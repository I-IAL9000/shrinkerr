import type { ChangelogEntry as ChangelogEntryData } from "../api";

/**
 * Per-section colour. Matches the conventional meanings in keep-a-changelog
 * so users can visually differentiate "new stuff" from "bug fixes" from
 * "security patches" at a glance.
 */
const SECTION_COLOR: Record<string, string> = {
  Added: "var(--success)",
  Changed: "var(--accent)",
  Fixed: "#ffa94d",
  Deprecated: "var(--text-muted)",
  Removed: "var(--text-muted)",
  Security: "#e94560",
};

/**
 * Render one changelog release (version + date + intro + sections) as a
 * card. Used by both the update-available modal in the sidebar and the
 * Updates section in Settings so both stay visually consistent.
 */
export default function ChangelogEntryView({
  entry, highlight = false,
}: {
  entry: ChangelogEntryData;
  /** When true, render with an accent border — used for the "latest release" card. */
  highlight?: boolean;
}) {
  return (
    <div
      style={{
        background: "var(--bg-card)",
        border: highlight ? "1px solid var(--accent)" : "1px solid var(--border)",
        borderRadius: 6,
        padding: 16,
        marginBottom: 12,
      }}
    >
      <div style={{ display: "flex", alignItems: "baseline", gap: 10, marginBottom: entry.intro ? 8 : 12, flexWrap: "wrap" }}>
        <span style={{ fontSize: 18, fontWeight: 700, color: "var(--text-primary)" }}>
          v{entry.version}
        </span>
        {entry.date && (
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
            {entry.date}
          </span>
        )}
        {highlight && (
          <span style={{
            fontSize: 10, fontWeight: 600, padding: "2px 8px", borderRadius: 3,
            background: "rgba(104,96,254,0.15)", color: "var(--accent)",
            textTransform: "uppercase", letterSpacing: 0.5,
          }}>
            Latest
          </span>
        )}
      </div>

      {entry.intro && (
        <div style={{ fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.6, marginBottom: 12 }}>
          {entry.intro}
        </div>
      )}

      {Object.entries(entry.sections).map(([sectionName, bullets]) => {
        if (!bullets || bullets.length === 0) return null;
        const color = SECTION_COLOR[sectionName] || "var(--text-secondary)";
        return (
          <div key={sectionName} style={{ marginBottom: 10 }}>
            <div style={{
              fontSize: 11, fontWeight: 700, textTransform: "uppercase",
              letterSpacing: 0.6, color, marginBottom: 4,
            }}>
              {sectionName}
            </div>
            <ul style={{ margin: 0, paddingLeft: 20, color: "var(--text-secondary)", fontSize: 12, lineHeight: 1.65 }}>
              {bullets.map((b, i) => (
                // Render bullet text as-is. Bold (`**text**`) and code
                // (`` `code` ``) markdown are common in changelogs; convert
                // them to matching HTML so the formatting survives. Keep
                // conversions narrow to avoid accidentally rendering
                // user-controlled HTML.
                <li key={i} dangerouslySetInnerHTML={{ __html: renderInlineMarkdown(b) }} />
              ))}
            </ul>
          </div>
        );
      })}
    </div>
  );
}

/**
 * Narrow inline-markdown renderer for changelog bullets.
 *
 * Converts:
 *   **bold**   → <strong>bold</strong>
 *   `code`     → <code>code</code>
 *   [text](url) → <a href="url" target="_blank">text</a>
 *
 * Everything else is HTML-escaped first, so the CHANGELOG can contain
 * arbitrary characters without risking injection when rendered via
 * dangerouslySetInnerHTML.
 */
function renderInlineMarkdown(text: string): string {
  const escaped = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
  return escaped
    // Links (do first so bold/code inside link text still work)
    .replace(
      /\[([^\]]+)\]\(([^)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer" style="color:var(--accent);text-decoration:none;">$1</a>',
    )
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, '<code style="background:var(--bg-primary);padding:1px 5px;border-radius:3px;font-size:11px;">$1</code>');
}
