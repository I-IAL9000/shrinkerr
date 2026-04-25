import type { FileEvent } from "../api";
import { vmafColor } from "../utils/vmaf";

interface EventMeta { color: string; label: string; }

const EVENT_META: Record<string, EventMeta> = {
  scanned:      { color: "var(--text-muted)", label: "Scanned" },
  rescanned:    { color: "var(--text-muted)", label: "Rescanned" },
  queued:       { color: "var(--accent)",     label: "Queued" },
  started:      { color: "var(--accent)",     label: "Started" },
  completed:    { color: "var(--success)",    label: "Completed" },
  failed:       { color: "var(--danger)",     label: "Failed" },
  skipped:      { color: "var(--text-muted)", label: "Skipped" },
  ignored:      { color: "var(--text-muted)", label: "Ignored" },
  unignored:    { color: "var(--accent)",     label: "Unignored" },
  health_check: { color: "var(--success)",    label: "Health check" },
  vmaf:         { color: "var(--accent)",     label: "VMAF" },
  reverted:     { color: "var(--warning)",    label: "Reverted" },
  arr_action:   { color: "#e5a00d",           label: "*arr action" },
};

// For event types whose colour depends on the outcome stored in `details`.
// Without this, every health_check or VMAF entry in the activity log uses
// the same default colour regardless of result, so a corrupt file or a
// "Poor" VMAF score is indistinguishable at a glance from the dozens of
// healthy / excellent rows surrounding it. VMAF tiers come from the
// canonical helper (utils/vmaf.ts).
function metaFor(ev: FileEvent): EventMeta {
  const base = EVENT_META[ev.event_type] || { color: "var(--text-muted)", label: ev.event_type };
  const details = (ev.details && typeof ev.details === "object") ? ev.details : null;

  if (ev.event_type === "health_check") {
    const status = details?.status as "healthy" | "warnings" | "corrupt" | null | undefined;
    if (status === "corrupt") return { color: "var(--danger)",  label: "Health check" };
    if (status === "warnings") return { color: "var(--warning)", label: "Health check" };
    // healthy / unknown → green default
    return base;
  }

  if (ev.event_type === "vmaf" && details) {
    // Failure / rejection paths — score may be missing, but the event is
    // unambiguously bad. Red.
    if (details.vmaf_error || details.rejected) {
      return { color: "var(--danger)", label: "VMAF" };
    }
    const score = typeof details.vmaf_score === "number" ? details.vmaf_score : null;
    if (score != null) {
      // When libvmaf desynced on every analysis window (vmaf_uncertain),
      // the score isn't trustworthy. Show as amber rather than the
      // score's actual tier colour so a 39.5 ⚠ doesn't look like a real
      // quality crisis.
      if (details.vmaf_uncertain) {
        return { color: "var(--warning)", label: "VMAF" };
      }
      return { color: vmafColor(score), label: "VMAF" };
    }
    // No score, no failure — fall through to default accent colour.
  }

  return base;
}

function EventIcon({ type, color, size = 14 }: { type: string; color: string; size?: number }) {
  const common = {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: color,
    strokeWidth: 2,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
  };
  switch (type) {
    case "scanned":
      return (
        <svg {...common}><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
      );
    case "rescanned":
      return (
        <svg {...common}><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
      );
    case "queued":
    case "started":
      return (
        <svg {...common}><polygon points="6 4 20 12 6 20 6 4"/></svg>
      );
    case "completed":
      return (
        <svg {...common}><polyline points="20 6 9 17 4 12"/></svg>
      );
    case "failed":
      return (
        <svg {...common}><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      );
    case "skipped":
      return (
        <svg {...common}><polygon points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19"/></svg>
      );
    case "ignored":
      return (
        <svg {...common}><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>
      );
    case "unignored":
      return (
        <svg {...common}><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
      );
    case "health_check":
      // Heart-pulse / activity line — clean line icon, no stethoscope emoji
      return (
        <svg {...common}><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
      );
    case "vmaf":
      // Bar chart
      return (
        <svg {...common}><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/><line x1="3" y1="20" x2="21" y2="20"/></svg>
      );
    case "reverted":
      return (
        <svg {...common}><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>
      );
    case "arr_action":
      // Download-rotating icon — evokes "ask the *arr for a fresh grab"
      return (
        <svg {...common}><path d="M21 12a9 9 0 11-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>
      );
    default:
      return (
        <svg {...common}><circle cx="12" cy="12" r="2"/></svg>
      );
  }
}

function fmtDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function fmtRelative(iso: string): string {
  try {
    const d = new Date(iso).getTime();
    const diff = (Date.now() - d) / 1000;
    if (diff < 60) return `${Math.round(diff)}s ago`;
    if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
    return `${Math.round(diff / 86400)}d ago`;
  } catch {
    return "";
  }
}

interface Props {
  events: FileEvent[];
  compact?: boolean;
  showFilePath?: boolean;
}

export default function EventTimeline({ events, compact = false, showFilePath = false }: Props) {
  if (!events || events.length === 0) {
    return (
      <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "8px 0", fontStyle: "italic" }}>
        No events recorded yet.
      </div>
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: compact ? 4 : 6 }}>
      {events.map(ev => {
        const meta = metaFor(ev);
        return (
          <div
            key={ev.id}
            style={{
              display: "grid",
              gridTemplateColumns: compact ? "20px 1fr auto" : "22px 110px 1fr auto",
              alignItems: "center",
              gap: 8,
              fontSize: compact ? 11 : 12,
              padding: compact ? "4px 6px" : "6px 8px",
              borderRadius: 4,
              background: "var(--bg-card)",
              border: "1px solid var(--border)",
            }}
          >
            <span style={{ display: "inline-flex", alignItems: "center", justifyContent: "center" }}>
              <EventIcon type={ev.event_type} color={meta.color} size={compact ? 13 : 14} />
            </span>
            {!compact && (
              <span style={{ color: meta.color, fontWeight: 600, fontSize: 11 }}>{meta.label}</span>
            )}
            <span style={{ color: "var(--text-secondary)" }}>
              {ev.summary}
              {showFilePath && (
                <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2, wordBreak: "break-all" }}>
                  {ev.file_path}
                </div>
              )}
            </span>
            <span title={fmtDate(ev.occurred_at)} style={{ fontSize: 10, color: "var(--text-muted)", whiteSpace: "nowrap" }}>
              {compact ? fmtRelative(ev.occurred_at) : fmtDate(ev.occurred_at)}
            </span>
          </div>
        );
      })}
    </div>
  );
}
