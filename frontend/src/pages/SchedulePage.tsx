import { useState, useEffect } from "react";
import { getSchedule, setSchedule, cancelSchedule, startQueue, pauseQueue, setRunHours, getEncodingSettings, updateEncodingSettings } from "../api";
import { useToast } from "../useToast";

const inputStyle: React.CSSProperties = {
  background: "var(--bg-primary)", color: "var(--text-secondary)",
  border: "1px solid var(--border)", padding: "8px 10px", borderRadius: 4, fontSize: 13,
  height: 36, boxSizing: "border-box" as const,
};

export default function SchedulePage() {
  const [scheduledTime, setScheduledTime] = useState<string | null>(null);
  const [inputTime, setInputTime] = useState("");
  const [runHoursEnabled, setRunHoursEnabled] = useState(false);
  const [activeHours, setActiveHours] = useState<boolean[]>(
    // Default: 10 PM to 8 AM
    Array.from({ length: 24 }, (_, i) => i >= 22 || i < 8)
  );
  const [isDragging, setIsDragging] = useState(false);
  const [dragValue, setDragValue] = useState(true);
  const [quietEnabled, setQuietEnabled] = useState(false);
  const [quietStart, setQuietStart] = useState(22);
  const [quietEnd, setQuietEnd] = useState(8);
  const [quietParallel, setQuietParallel] = useState(1);
  const [quietNice, setQuietNice] = useState(true);
  const [plexPauseEnabled, setPlexPauseEnabled] = useState(false);
  const [plexPauseThreshold, setPlexPauseThreshold] = useState(1);
  const [plexPauseTranscodeOnly, setPlexPauseTranscodeOnly] = useState(true);
  const toast = useToast();

  useEffect(() => {
    getEncodingSettings().then((enc: any) => {
      if (enc) {
        setQuietEnabled(enc.quiet_hours_enabled ?? false);
        setQuietStart(enc.quiet_hours_start ?? 22);
        setQuietEnd(enc.quiet_hours_end ?? 8);
        setQuietParallel(enc.quiet_hours_parallel ?? 1);
        setQuietNice(enc.quiet_hours_nice ?? true);
        setPlexPauseEnabled(enc.plex_pause_on_stream ?? false);
        setPlexPauseThreshold(enc.plex_pause_stream_threshold ?? 1);
        setPlexPauseTranscodeOnly(enc.plex_pause_transcode_only ?? true);
      }
    }).catch(() => {});
    getSchedule().then((r: any) => {
      if (r.scheduled_start) setScheduledTime(r.scheduled_start);
      if (r.run_hours) {
        setRunHoursEnabled(r.run_hours.enabled || false);
        if (Array.isArray(r.run_hours.hours)) {
          const hrs = Array(24).fill(false);
          r.run_hours.hours.forEach((h: number) => { if (h >= 0 && h < 24) hrs[h] = true; });
          setActiveHours(hrs);
        } else if (r.run_hours.start !== undefined) {
          // Migrate from old start/end format
          const s = r.run_hours.start ?? 22;
          const e = r.run_hours.end ?? 8;
          setActiveHours(Array.from({ length: 24 }, (_, i) =>
            s > e ? (i >= s || i < e) : (i >= s && i < e)
          ));
        }
      }
    });
  }, []);

  const handleSchedule = async () => {
    if (!inputTime) return;
    await setSchedule(new Date(inputTime).toISOString());
    setScheduledTime(inputTime);
    toast("Queue start scheduled", "success");
  };

  const handleCancel = async () => {
    await cancelSchedule();
    setScheduledTime(null);
    toast("Schedule cancelled");
  };

  const formatHour = (h: number) => {
    if (h === 0) return "12AM";
    if (h === 12) return "12PM";
    return h < 12 ? `${h}AM` : `${h - 12}PM`;
  };

  const toggleHour = (i: number, forceValue?: boolean) => {
    setActiveHours(prev => {
      const next = [...prev];
      next[i] = forceValue !== undefined ? forceValue : !next[i];
      return next;
    });
  };

  const activeCount = activeHours.filter(Boolean).length;

  // Build summary text
  const buildSummary = () => {
    if (activeCount === 0) return "No hours selected. Queue will not run.";
    if (activeCount === 24) return "All hours selected. Queue runs 24/7.";

    // Find contiguous ranges
    const ranges: string[] = [];
    let i = 0;
    while (i < 24) {
      if (activeHours[i]) {
        const start = i;
        while (i < 24 && activeHours[i]) i++;
        ranges.push(`${formatHour(start)}-${formatHour(i % 24)}`);
      } else {
        i++;
      }
    }
    // Handle wrap-around: if first and last ranges connect
    if (ranges.length >= 2 && activeHours[0] && activeHours[23]) {
      const last = ranges.pop()!;
      const first = ranges.shift()!;
      ranges.unshift(`${last.split("-")[0]}-${first.split("-")[1]}`);
    }
    return `Runs: ${ranges.join(", ")}`;
  };

  return (
    <div>
      <h2 style={{ color: "var(--text-primary)", fontSize: 20, marginBottom: 20 }}>Schedule</h2>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 300, background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
          <h3 style={{ color: "var(--text-primary)", marginBottom: 16 }}>Schedule Queue Start</h3>
          <label style={{ fontSize: 12, opacity: 0.5 }}>Start time:</label>
          <input
            type="datetime-local"
            value={inputTime}
            onChange={(e) => setInputTime(e.target.value)}
            style={{
              display: "block", width: "100%", boxSizing: "border-box" as const, marginTop: 4, marginBottom: 12,
              backgroundColor: "var(--bg-primary)", color: "var(--text-secondary)",
              border: "1px solid var(--border)", padding: 8, borderRadius: 4, fontSize: 14,
            }}
          />
          {scheduledTime && (
            <div style={{ marginBottom: 12 }}>
              <span style={{ color: "var(--success)" }}>
                Scheduled: {new Date(scheduledTime).toLocaleString()}
              </span>
              <button className="btn btn-secondary" onClick={handleCancel}
                style={{ marginLeft: 8, fontSize: 11, padding: "4px 8px" }}>Cancel</button>
            </div>
          )}
          <button className="btn btn-primary" onClick={handleSchedule}>Schedule</button>
        </div>

        <div style={{ flex: 1, minWidth: 300, background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
          <h3 style={{ color: "var(--text-primary)", marginBottom: 16 }}>Quick Actions</h3>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <button className="btn btn-secondary" style={{ textAlign: "left" }}
              onClick={() => { startQueue(); toast("Queue started", "success"); }}>Start queue now</button>
            <button className="btn btn-secondary" style={{ textAlign: "left" }}
              onClick={() => { pauseQueue(); toast("Queue paused"); }}>Pause after current job</button>
          </div>
        </div>
      </div>

      {/* Run Hours */}
      <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6, marginTop: 12 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
          <h3 style={{ color: "var(--text-primary)" }}>Run During These Hours Only</h3>
          <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>{runHoursEnabled ? "Enabled" : "Disabled"}</span>
            <input type="checkbox" checked={runHoursEnabled}
              onChange={(e) => setRunHoursEnabled(e.target.checked)}
              style={{ accentColor: "var(--accent)", width: 18, height: 18 }} />
          </label>
        </div>

        <div style={{ opacity: runHoursEnabled ? 1 : 0.4, transition: "opacity 0.2s" }}>
          <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 8 }}>
            Click or drag to toggle hours on/off:
          </div>

          {/* Clickable 24-hour grid */}
          <div
            style={{ display: "flex", gap: 2, marginBottom: 4, userSelect: "none" }}
            onMouseLeave={() => setIsDragging(false)}
            onMouseUp={() => setIsDragging(false)}
          >
            {activeHours.map((active, i) => (
              <div
                key={i}
                onMouseDown={(e) => {
                  e.preventDefault();
                  if (!runHoursEnabled) return;
                  const newVal = !active;
                  setDragValue(newVal);
                  setIsDragging(true);
                  toggleHour(i, newVal);
                }}
                onMouseEnter={() => {
                  if (isDragging && runHoursEnabled) toggleHour(i, dragValue);
                }}
                style={{
                  flex: 1, height: 36, borderRadius: 3,
                  background: active ? "var(--accent)" : "var(--bg-primary)",
                  opacity: active ? 0.9 : 0.3,
                  cursor: runHoursEnabled ? "pointer" : "default",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  transition: "background 0.1s, opacity 0.1s",
                  border: active ? "1px solid var(--accent-hover)" : "1px solid transparent",
                }}
              >
                <span style={{
                  fontSize: 9, fontWeight: active ? "bold" : "normal",
                  color: active ? "white" : "var(--text-muted)",
                }}>
                  {i}
                </span>
              </div>
            ))}
          </div>

          {/* Hour labels */}
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--text-muted)", marginBottom: 12, padding: "0 2px" }}>
            <span>12AM</span><span>6AM</span><span>12PM</span><span>6PM</span><span>11PM</span>
          </div>

          {/* Quick presets */}
          <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
            <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}
              disabled={!runHoursEnabled}
              onClick={() => setActiveHours(Array.from({ length: 24 }, (_, i) => i >= 22 || i < 8))}>
              Overnight (10PM-8AM)
            </button>
            <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}
              disabled={!runHoursEnabled}
              onClick={() => setActiveHours(Array.from({ length: 24 }, (_, i) => i >= 0 && i < 8))}>
              Night (12AM-8AM)
            </button>
            <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}
              disabled={!runHoursEnabled}
              onClick={() => setActiveHours(Array(24).fill(true))}>
              All day
            </button>
            <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}
              disabled={!runHoursEnabled}
              onClick={() => setActiveHours(Array(24).fill(false))}>
              Clear
            </button>
          </div>

          {/* Summary */}
          {runHoursEnabled && (
            <div style={{ fontSize: 12, color: "var(--text-muted)", background: "var(--bg-primary)", padding: 10, borderRadius: 4 }}>
              {buildSummary()} Pauses automatically outside selected hours.
            </div>
          )}

          <button className="btn btn-primary" style={{ marginTop: 12 }}
            onClick={async () => {
              const hours = activeHours.map((v, i) => v ? i : -1).filter(i => i >= 0);
              await setRunHours({ enabled: runHoursEnabled, hours });
              toast("Run hours saved", "success");
            }}>
            Save Run Hours
          </button>
        </div>
      </div>

      {/* Quiet Hours */}
      <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6, marginTop: 12 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          <h3 style={{ color: "var(--text-primary)" }}>Quiet Hours</h3>
          <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>{quietEnabled ? "Enabled" : "Disabled"}</span>
            <input type="checkbox" checked={quietEnabled}
              onChange={(e) => setQuietEnabled(e.target.checked)}
              style={{ accentColor: "var(--accent)", width: 18, height: 18 }} />
          </label>
        </div>
        <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 16 }}>
          Reduce encoding intensity during specified hours. Fewer parallel jobs and optionally lower process priority.
        </div>

        <div style={{ opacity: quietEnabled ? 1 : 0.4, transition: "opacity 0.2s" }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 16, marginBottom: 16 }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <label style={{ fontSize: 12, color: "var(--text-muted)" }}>Start hour</label>
              <select style={inputStyle} value={quietStart} disabled={!quietEnabled}
                onChange={e => setQuietStart(Number(e.target.value))}>
                {Array.from({ length: 24 }, (_, i) => <option key={i} value={i}>{i}:00</option>)}
              </select>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <label style={{ fontSize: 12, color: "var(--text-muted)" }}>End hour</label>
              <select style={inputStyle} value={quietEnd} disabled={!quietEnabled}
                onChange={e => setQuietEnd(Number(e.target.value))}>
                {Array.from({ length: 24 }, (_, i) => <option key={i} value={i}>{i}:00</option>)}
              </select>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <label style={{ fontSize: 12, color: "var(--text-muted)" }}>Max parallel jobs</label>
              <input type="number" style={inputStyle} min={1} max={16} disabled={!quietEnabled}
                value={quietParallel}
                onChange={e => setQuietParallel(Number(e.target.value))} />
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <label style={{ fontSize: 12, color: "var(--text-muted)" }}>Process priority</label>
              <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: quietEnabled ? "pointer" : "default", height: 36 }}>
                <input type="checkbox" checked={quietNice} disabled={!quietEnabled}
                  onChange={() => setQuietNice(!quietNice)}
                  style={{ accentColor: "var(--accent)" }} />
                <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>Lower priority (nice)</span>
              </label>
            </div>
          </div>

          {quietEnabled && (
            <div style={{ fontSize: 12, color: "var(--text-muted)", background: "var(--bg-primary)", padding: 10, borderRadius: 4, marginBottom: 12 }}>
              {quietStart > quietEnd
                ? `Active ${quietStart}:00 - ${quietEnd}:00 (overnight). `
                : `Active ${quietStart}:00 - ${quietEnd}:00. `}
              Max {quietParallel} parallel job{quietParallel !== 1 ? "s" : ""}.
              {quietNice ? " Processes run at lower priority." : ""}
            </div>
          )}

          <button className="btn btn-primary"
            onClick={async () => {
              await updateEncodingSettings({
                quiet_hours_enabled: quietEnabled,
                quiet_hours_start: String(quietStart),
                quiet_hours_end: String(quietEnd),
                quiet_hours_parallel: String(quietParallel),
                quiet_hours_nice: quietNice,
              });
              toast("Quiet hours saved", "success");
            }}>
            Save Quiet Hours
          </button>
        </div>
      </div>

      {/* Stream-Aware Scheduling */}
      <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6, marginTop: 12 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          <h3 style={{ color: "var(--text-primary)" }}>Plex / Jellyfin Stream-Aware Scheduling</h3>
          <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>{plexPauseEnabled ? "Enabled" : "Disabled"}</span>
            <input type="checkbox" checked={plexPauseEnabled}
              onChange={(e) => setPlexPauseEnabled(e.target.checked)}
              style={{ accentColor: "var(--accent)", width: 18, height: 18 }} />
          </label>
        </div>
        <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 16 }}>
          Pause encoding when Plex or Jellyfin users are actively streaming. Prevents competing for disk I/O and CPU, ensuring smooth playback. Encoding resumes automatically when streams end.
        </div>

        <div style={{ opacity: plexPauseEnabled ? 1 : 0.4, transition: "opacity 0.2s" }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 16, marginBottom: 16 }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <label style={{ fontSize: 12, color: "var(--text-muted)" }}>Pause when streams reach</label>
              <input type="number" min={1} max={20} style={inputStyle} disabled={!plexPauseEnabled}
                value={plexPauseThreshold}
                onChange={e => setPlexPauseThreshold(Number(e.target.value))} />
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <label style={{ fontSize: 12, color: "var(--text-muted)" }}>Count only</label>
              <select style={inputStyle} disabled={!plexPauseEnabled}
                value={plexPauseTranscodeOnly ? "transcode" : "all"}
                onChange={e => setPlexPauseTranscodeOnly(e.target.value === "transcode")}>
                <option value="transcode">Transcoding streams</option>
                <option value="all">All streams (including direct play)</option>
              </select>
            </div>
          </div>

          {plexPauseEnabled && (
            <div style={{ fontSize: 12, color: "var(--text-muted)", background: "var(--bg-primary)", padding: 10, borderRadius: 4, marginBottom: 12 }}>
              Encoding will pause when {plexPauseThreshold} or more {plexPauseTranscodeOnly ? "transcoding" : ""} stream{plexPauseThreshold !== 1 ? "s" : ""} {plexPauseTranscodeOnly ? "are" : plexPauseThreshold === 1 ? "is" : "are"} active.
              {plexPauseTranscodeOnly && " Direct play streams won't trigger a pause since they don't use server CPU."}
              {" "}Checks every 15 seconds.
            </div>
          )}

          <button className="btn btn-primary"
            onClick={async () => {
              await updateEncodingSettings({
                plex_pause_on_stream: plexPauseEnabled,
                plex_pause_stream_threshold: String(plexPauseThreshold),
                plex_pause_transcode_only: plexPauseTranscodeOnly,
              });
              toast("Streaming settings saved", "success");
            }}>
            Save Streaming Settings
          </button>
        </div>
      </div>
    </div>
  );
}
