import { useEffect, useState } from "react";
import { getActivity, type FileEvent } from "../api";
import EventTimeline from "../components/EventTimeline";

const EVENT_OPTIONS = [
  { value: "all", label: "All events" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "queued", label: "Queued" },
  { value: "health_check", label: "Health checks" },
  { value: "vmaf", label: "VMAF analysis" },
  { value: "ignored,unignored", label: "Ignore changes" },
  { value: "reverted", label: "Reverts" },
  { value: "arr_action", label: "Sonarr / Radarr actions" },
];

const PAGE_SIZE = 100;

export default function ActivityPage() {
  const [events, setEvents] = useState<FileEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [eventType, setEventType] = useState<string>("all");
  const [search, setSearch] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [offset, setOffset] = useState(0);

  useEffect(() => {
    setLoading(true);
    getActivity({
      event_type: eventType !== "all" ? eventType : undefined,
      search: appliedSearch || undefined,
      limit: PAGE_SIZE,
      offset,
    })
      .then(d => {
        setEvents(d.events);
        setTotal(d.total);
      })
      .finally(() => setLoading(false));
  }, [eventType, appliedSearch, offset]);

  // Reset offset when filters change
  useEffect(() => {
    setOffset(0);
  }, [eventType, appliedSearch]);

  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="main-content">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16, gap: 16, flexWrap: "wrap" }}>
        <h1 style={{ color: "var(--text-primary)", fontSize: 22 }}>Activity</h1>
        <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
          {loading ? "Loading…" : `${total.toLocaleString()} event${total === 1 ? "" : "s"}`}
        </div>
      </div>

      {/* Filters */}
      <div style={{
        display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap",
        background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: 6,
        padding: "10px 12px", marginBottom: 16,
      }}>
        <select
          value={eventType}
          onChange={e => setEventType(e.target.value)}
          style={{ background: "var(--bg-primary)", color: "var(--text-primary)", border: "1px solid var(--border)", padding: "6px 10px", borderRadius: 4, fontSize: 12 }}
        >
          {EVENT_OPTIONS.map(o => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>

        <form
          onSubmit={(e) => { e.preventDefault(); setAppliedSearch(search.trim()); }}
          style={{ display: "flex", gap: 6, flex: "1 1 280px" }}
        >
          <input
            type="text"
            placeholder="Search file path…"
            value={search}
            onChange={e => setSearch(e.target.value)}
            style={{
              flex: 1, background: "var(--bg-primary)", color: "var(--text-primary)",
              border: "1px solid var(--border)", padding: "6px 10px", borderRadius: 4, fontSize: 12,
            }}
          />
          <button type="submit" className="btn btn-secondary" style={{ fontSize: 12, padding: "6px 12px" }}>
            Search
          </button>
          {appliedSearch && (
            <button
              type="button"
              className="btn btn-secondary"
              style={{ fontSize: 12, padding: "6px 12px" }}
              onClick={() => { setSearch(""); setAppliedSearch(""); }}
            >Clear</button>
          )}
        </form>
      </div>

      {/* Timeline */}
      {loading ? (
        <div style={{ display: "flex", alignItems: "center", gap: 8, padding: 16 }}>
          <div className="spinner" style={{ width: 16, height: 16 }} />
          <span style={{ color: "var(--text-muted)" }}>Loading activity…</span>
        </div>
      ) : (
        <EventTimeline events={events} showFilePath />
      )}

      {/* Pagination */}
      {total > PAGE_SIZE && (
        <div style={{ display: "flex", justifyContent: "center", alignItems: "center", gap: 8, marginTop: 16 }}>
          <button
            className="btn btn-secondary"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            style={{ fontSize: 12, padding: "6px 12px", opacity: offset === 0 ? 0.5 : 1 }}
          >
            Prev
          </button>
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
            Page {page} of {totalPages}
          </span>
          <button
            className="btn btn-secondary"
            disabled={offset + PAGE_SIZE >= total}
            onClick={() => setOffset(offset + PAGE_SIZE)}
            style={{ fontSize: 12, padding: "6px 12px", opacity: offset + PAGE_SIZE >= total ? 0.5 : 1 }}
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}
