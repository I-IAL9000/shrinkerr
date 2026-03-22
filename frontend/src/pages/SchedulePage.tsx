import { useState, useEffect } from "react";
import { getSchedule, setSchedule, cancelSchedule, startQueue, pauseQueue, clearCompleted } from "../api";

export default function SchedulePage() {
  const [scheduledTime, setScheduledTime] = useState<string | null>(null);
  const [inputTime, setInputTime] = useState("");

  useEffect(() => {
    getSchedule().then((r: any) => {
      if (r.scheduled_start) setScheduledTime(r.scheduled_start);
    });
  }, []);

  const handleSchedule = async () => {
    if (!inputTime) return;
    await setSchedule(new Date(inputTime).toISOString());
    setScheduledTime(inputTime);
  };

  const handleCancel = async () => {
    await cancelSchedule();
    setScheduledTime(null);
  };

  return (
    <div>
      <h2 style={{ color: "white", fontSize: 20, marginBottom: 20 }}>Schedule</h2>
      <div style={{ display: "flex", gap: 16 }}>
        <div style={{ flex: 1, background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
          <h3 style={{ color: "white", marginBottom: 16 }}>Schedule Queue Start</h3>
          <label style={{ fontSize: 12, opacity: 0.5 }}>Start time:</label>
          <input
            type="datetime-local"
            value={inputTime}
            onChange={(e) => setInputTime(e.target.value)}
            style={{
              display: "block", width: "100%", marginTop: 4, marginBottom: 12,
              background: "var(--bg-primary)", color: "var(--text-secondary)",
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

        <div style={{ flex: 1, background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
          <h3 style={{ color: "white", marginBottom: 16 }}>Quick Actions</h3>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <button className="btn btn-secondary" style={{ textAlign: "left" }}
              onClick={() => startQueue()}>Start queue now</button>
            <button className="btn btn-secondary" style={{ textAlign: "left" }}
              onClick={() => pauseQueue()}>Pause after current job</button>
            <button className="btn btn-secondary" style={{ textAlign: "left" }}
              onClick={() => clearCompleted()}>Clear completed jobs</button>
          </div>
        </div>
      </div>
    </div>
  );
}
