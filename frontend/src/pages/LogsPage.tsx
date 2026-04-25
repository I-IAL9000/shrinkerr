import { useEffect, useRef, useState, useCallback } from "react";
import { getStoredApiKey } from "../api";

interface LogEntry {
  timestamp: string;
  level: string;
  source: string;
  message: string;
}

const SOURCE_COLORS: Record<string, string> = {
  WORKER: "#00d4ff",
  CONVERT: "#4caf50",
  WATCHER: "#fdd835",
  PLEX: "#ffa726",
  METADATA: "#ce93d8",
  SCANNER: "#64b5f6",
  CLEANUP: "#78909c",
  QUEUE: "#4dd0e1",
  API: "#aed581",
  SYSTEM: "#78909c",
};

const SOURCE_OPTIONS = [
  "All",
  "Worker",
  "Convert",
  "Watcher",
  "Plex",
  "Metadata",
  "Scanner",
  "System",
];

const MAX_DOM_LINES = 1000;

export default function LogsPage() {
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [sourceFilter, setSourceFilter] = useState("All");
  const [search, setSearch] = useState("");
  const [autoScroll, setAutoScroll] = useState(true);
  const [showJumpBtn, setShowJumpBtn] = useState(false);

  const containerRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const autoScrollRef = useRef(autoScroll);
  autoScrollRef.current = autoScroll;

  // Fetch initial history
  useEffect(() => {
    const headers: Record<string, string> = {};
    const apiKey = getStoredApiKey();
    if (apiKey) headers["X-Api-Key"] = apiKey;
    fetch("/api/logs?limit=500", { headers })
      .then((r) => r.json())
      .then((data: LogEntry[]) => {
        setLogs(data.slice(-MAX_DOM_LINES));
      })
      .catch(() => {});
  }, []);

  // WebSocket connection
  useEffect(() => {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const apiKey = getStoredApiKey();
    const wsUrl = `${proto}//${window.location.host}/ws/logs${apiKey ? `?api_key=${encodeURIComponent(apiKey)}` : ""}`;
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onmessage = (evt) => {
      try {
        const entry: LogEntry = JSON.parse(evt.data);
        setLogs((prev) => {
          const next = [...prev, entry];
          return next.length > MAX_DOM_LINES
            ? next.slice(next.length - MAX_DOM_LINES)
            : next;
        });
      } catch {
        // ignore malformed messages
      }
    };

    ws.onclose = () => {
      // Attempt reconnect after 3 seconds
      setTimeout(() => {
        if (wsRef.current === ws) {
          // trigger effect re-run by updating state
        }
      }, 3000);
    };

    return () => {
      ws.close();
      wsRef.current = null;
    };
  }, []);

  // Auto-scroll
  useEffect(() => {
    if (autoScrollRef.current && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [logs]);

  const handleScroll = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    setShowJumpBtn(!atBottom && !autoScrollRef.current);
  }, []);

  const jumpToLatest = useCallback(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
    setShowJumpBtn(false);
  }, []);

  const clearView = useCallback(() => {
    setLogs([]);
  }, []);

  // Filter logs for display
  const displayed = logs.filter((entry) => {
    if (sourceFilter !== "All" && entry.source !== sourceFilter.toUpperCase()) {
      return false;
    }
    if (search && !entry.message.toLowerCase().includes(search.toLowerCase())) {
      return false;
    }
    return true;
  });

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", gap: 0 }}>
      {/* Toolbar */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          padding: "12px 16px",
          background: "var(--bg-secondary)",
          borderBottom: "1px solid var(--border)",
          flexShrink: 0,
          flexWrap: "wrap",
        }}
      >
        <h2 style={{ color: "var(--text-primary)", fontSize: 16, fontWeight: 600, margin: 0, marginRight: 8 }}>
          Logs
        </h2>

        {/* Source filter */}
        <select
          value={sourceFilter}
          onChange={(e) => setSourceFilter(e.target.value)}
          style={{
            backgroundColor: "var(--bg-card)",
            color: "var(--text-secondary)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius-sm)",
            padding: "5px 8px",
            fontSize: 12,
            outline: "none",
          }}
        >
          {SOURCE_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>

        {/* Search */}
        <input
          type="text"
          placeholder="Search logs..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{
            background: "var(--bg-card)",
            color: "var(--text-secondary)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius-sm)",
            padding: "5px 10px",
            fontSize: 12,
            width: 200,
            outline: "none",
          }}
        />

        {/* Auto-scroll toggle */}
        <label
          style={{
            display: "flex",
            alignItems: "center",
            gap: 5,
            fontSize: 12,
            color: "var(--text-muted)",
            cursor: "pointer",
            userSelect: "none",
          }}
        >
          <input
            type="checkbox"
            checked={autoScroll}
            onChange={(e) => setAutoScroll(e.target.checked)}
            style={{ accentColor: "var(--accent)" }}
          />
          Auto-scroll
        </label>

        {/* Clear button */}
        <button
          onClick={clearView}
          style={{
            background: "transparent",
            color: "var(--text-muted)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius-sm)",
            padding: "4px 10px",
            fontSize: 12,
            cursor: "pointer",
          }}
        >
          Clear
        </button>

        <span style={{ marginLeft: "auto", fontSize: 11, color: "var(--text-dim)" }}>
          {displayed.length} lines
        </span>
      </div>

      {/* Log output */}
      <div
        ref={containerRef}
        onScroll={handleScroll}
        style={{
          flex: 1,
          overflow: "auto",
          background: "var(--bg-primary)",
          padding: "8px 0",
          fontFamily: "var(--font-mono)",
          fontSize: 12,
          lineHeight: 1.65,
          position: "relative",
        }}
      >
        {displayed.length === 0 && (
          <div
            style={{
              color: "var(--text-dim)",
              textAlign: "center",
              padding: 40,
              fontSize: 13,
            }}
          >
            No log entries yet. Logs will appear here in real time.
          </div>
        )}
        {displayed.map((entry, i) => (
          <LogLine key={i} entry={entry} />
        ))}
      </div>

      {/* Jump to latest button */}
      {showJumpBtn && (
        <button
          onClick={jumpToLatest}
          style={{
            position: "absolute",
            bottom: 24,
            right: 32,
            background: "var(--accent)",
            color: "#fff",
            border: "none",
            borderRadius: "var(--radius)",
            padding: "6px 14px",
            fontSize: 12,
            fontWeight: 600,
            cursor: "pointer",
            boxShadow: "0 2px 8px rgba(0,0,0,0.4)",
            zIndex: 10,
          }}
        >
          Jump to latest
        </button>
      )}
    </div>
  );
}

function LogLine({ entry }: { entry: LogEntry }) {
  const sourceColor = SOURCE_COLORS[entry.source] || SOURCE_COLORS.SYSTEM;

  return (
    <div
      style={{
        padding: "1px 16px",
        whiteSpace: "pre-wrap",
        wordBreak: "break-all",
      }}
    >
      <span style={{ color: "var(--text-dim)", marginRight: 10 }}>
        {entry.timestamp.replace("T", " ")}
      </span>
      <span
        style={{
          color: sourceColor,
          fontWeight: 600,
          minWidth: 80,
          display: "inline-block",
        }}
      >
        [{entry.source}]
      </span>{" "}
      <span
        style={{
          color:
            entry.level === "error"
              ? "#ef5350"
              : entry.level === "warn"
              ? "#ffa726"
              : "var(--text-secondary)",
        }}
      >
        {entry.message}
      </span>
    </div>
  );
}
