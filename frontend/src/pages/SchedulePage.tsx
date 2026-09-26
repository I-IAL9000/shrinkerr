import { useState, useEffect } from "react";
import { Trans, useTranslation } from "react-i18next";
import { getSchedule, setSchedule, cancelSchedule, startQueue, pauseQueue, setRunHours, getEncodingSettings, updateEncodingSettings } from "../api";
import { useToast } from "../useToast";
import { fmtDateTime } from "../fmt";

// Use `backgroundColor` (not the `background` shorthand) so the global
// `select { background-image: <chevron-svg> }` rule from theme.css survives.
// The shorthand wipes background-image and the dropdown arrow disappears.
const inputStyle: React.CSSProperties = {
  backgroundColor: "var(--bg-primary)", color: "var(--text-secondary)",
  border: "1px solid var(--border)", padding: "8px 10px", borderRadius: 4, fontSize: 13,
  height: 36, boxSizing: "border-box" as const,
};

export default function SchedulePage() {
  const { t } = useTranslation(["schedule", "common"]);
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
  const [jellyfinPauseEnabled, setJellyfinPauseEnabled] = useState(false);
  const [jellyfinPauseThreshold, setJellyfinPauseThreshold] = useState(1);
  const [jellyfinPauseTranscodeOnly, setJellyfinPauseTranscodeOnly] = useState(true);
  const [embyPauseEnabled, setEmbyPauseEnabled] = useState(false);
  const [embyPauseThreshold, setEmbyPauseThreshold] = useState(1);
  const [embyPauseTranscodeOnly, setEmbyPauseTranscodeOnly] = useState(true);
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
        setJellyfinPauseEnabled(enc.jellyfin_pause_on_stream ?? false);
        setJellyfinPauseThreshold(enc.jellyfin_pause_stream_threshold ?? 1);
        setJellyfinPauseTranscodeOnly(enc.jellyfin_pause_transcode_only ?? true);
        setEmbyPauseEnabled(enc.emby_pause_on_stream ?? false);
        setEmbyPauseThreshold(enc.emby_pause_stream_threshold ?? 1);
        setEmbyPauseTranscodeOnly(enc.emby_pause_transcode_only ?? true);
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
    toast(t("schedule:start.toastScheduled"), "success");
  };

  const handleCancel = async () => {
    await cancelSchedule();
    setScheduledTime(null);
    toast(t("schedule:start.toastCancelled"));
  };

  const formatHour = (h: number) => {
    if (h === 0) return t("schedule:hour.am", { hour: 12 });
    if (h === 12) return t("schedule:hour.pm", { hour: 12 });
    return h < 12 ? t("schedule:hour.am", { hour: h }) : t("schedule:hour.pm", { hour: h - 12 });
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
    if (activeCount === 0) return t("schedule:runHours.summaryNone");
    if (activeCount === 24) return t("schedule:runHours.summaryAll");

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
    return t("schedule:runHours.summaryRanges", { ranges: ranges.join(", ") });
  };

  return (
    <div>
      <h2 style={{ color: "var(--text-primary)", fontSize: 20, marginBottom: 20 }}>{t("schedule:title")}</h2>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
        <div style={{ flex: 1, minWidth: 300, background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
          <h3 style={{ color: "var(--text-primary)", marginBottom: 16 }}>{t("schedule:start.title")}</h3>
          <label style={{ fontSize: 12, opacity: 0.5 }}>{t("schedule:start.timeLabel")}</label>
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
                {t("schedule:start.scheduled", { time: fmtDateTime(scheduledTime) })}
              </span>
              <button className="btn btn-secondary" onClick={handleCancel}
                style={{ marginLeft: 8, fontSize: 11, padding: "4px 8px" }}>{t("common:actions.cancel")}</button>
            </div>
          )}
          <button className="btn btn-primary" onClick={handleSchedule}>{t("schedule:start.submit")}</button>
        </div>

        <div style={{ flex: 1, minWidth: 300, background: "var(--bg-card)", padding: 20, borderRadius: 6 }}>
          <h3 style={{ color: "var(--text-primary)", marginBottom: 16 }}>{t("schedule:quick.title")}</h3>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <button className="btn btn-secondary" style={{ textAlign: "left" }}
              onClick={() => { startQueue(); toast(t("schedule:quick.toastStarted"), "success"); }}>{t("schedule:quick.startNow")}</button>
            <button className="btn btn-secondary" style={{ textAlign: "left" }}
              onClick={() => { pauseQueue(); toast(t("schedule:quick.toastPaused")); }}>{t("schedule:quick.pauseAfterCurrent")}</button>
          </div>
        </div>
      </div>

      {/* Run Hours */}
      <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6, marginTop: 12 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
          <h3 style={{ color: "var(--text-primary)" }}>{t("schedule:runHours.title")}</h3>
          <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>{runHoursEnabled ? t("schedule:enabled") : t("schedule:disabled")}</span>
            <input type="checkbox" checked={runHoursEnabled}
              onChange={(e) => setRunHoursEnabled(e.target.checked)}
              style={{ accentColor: "var(--accent)", width: 18, height: 18 }} />
          </label>
        </div>

        <div style={{ opacity: runHoursEnabled ? 1 : 0.4, transition: "opacity 0.2s" }}>
          <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 8 }}>
            {t("schedule:runHours.hint")}
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
            <span>{formatHour(0)}</span><span>{formatHour(6)}</span><span>{formatHour(12)}</span><span>{formatHour(18)}</span><span>{formatHour(23)}</span>
          </div>

          {/* Quick presets */}
          <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
            <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}
              disabled={!runHoursEnabled}
              onClick={() => setActiveHours(Array.from({ length: 24 }, (_, i) => i >= 22 || i < 8))}>
              {t("schedule:runHours.presetOvernight", { range: `${formatHour(22)}-${formatHour(8)}` })}
            </button>
            <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}
              disabled={!runHoursEnabled}
              onClick={() => setActiveHours(Array.from({ length: 24 }, (_, i) => i >= 0 && i < 8))}>
              {t("schedule:runHours.presetNight", { range: `${formatHour(0)}-${formatHour(8)}` })}
            </button>
            <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}
              disabled={!runHoursEnabled}
              onClick={() => setActiveHours(Array(24).fill(true))}>
              {t("schedule:runHours.presetAllDay")}
            </button>
            <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}
              disabled={!runHoursEnabled}
              onClick={() => setActiveHours(Array(24).fill(false))}>
              {t("common:actions.clear")}
            </button>
          </div>

          {/* Summary */}
          {runHoursEnabled && (
            <div style={{ fontSize: 12, color: "var(--text-muted)", background: "var(--bg-primary)", padding: 10, borderRadius: 4 }}>
              {buildSummary()} {t("schedule:runHours.pausesOutside")}
            </div>
          )}

          <button className="btn btn-primary" style={{ marginTop: 12 }}
            onClick={async () => {
              const hours = activeHours.map((v, i) => v ? i : -1).filter(i => i >= 0);
              await setRunHours({ enabled: runHoursEnabled, hours });
              toast(t("schedule:runHours.toastSaved"), "success");
            }}>
            {t("schedule:runHours.save")}
          </button>
        </div>
      </div>

      {/* Quiet Hours */}
      <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6, marginTop: 12 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
          <h3 style={{ color: "var(--text-primary)" }}>{t("schedule:quiet.title")}</h3>
          <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>{quietEnabled ? t("schedule:enabled") : t("schedule:disabled")}</span>
            <input type="checkbox" checked={quietEnabled}
              onChange={(e) => setQuietEnabled(e.target.checked)}
              style={{ accentColor: "var(--accent)", width: 18, height: 18 }} />
          </label>
        </div>
        <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 16 }}>
          {t("schedule:quiet.description")}
        </div>

        <div style={{ opacity: quietEnabled ? 1 : 0.4, transition: "opacity 0.2s" }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 16, marginBottom: 16 }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <label style={{ fontSize: 12, color: "var(--text-muted)" }}>{t("schedule:quiet.startHour")}</label>
              <select style={inputStyle} value={quietStart} disabled={!quietEnabled}
                onChange={e => setQuietStart(Number(e.target.value))}>
                {Array.from({ length: 24 }, (_, i) => <option key={i} value={i}>{i}:00</option>)}
              </select>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <label style={{ fontSize: 12, color: "var(--text-muted)" }}>{t("schedule:quiet.endHour")}</label>
              <select style={inputStyle} value={quietEnd} disabled={!quietEnabled}
                onChange={e => setQuietEnd(Number(e.target.value))}>
                {Array.from({ length: 24 }, (_, i) => <option key={i} value={i}>{i}:00</option>)}
              </select>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <label style={{ fontSize: 12, color: "var(--text-muted)" }}>{t("schedule:quiet.maxParallel")}</label>
              <input type="number" style={inputStyle} min={1} max={16} disabled={!quietEnabled}
                value={quietParallel}
                onChange={e => setQuietParallel(Number(e.target.value))} />
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <label style={{ fontSize: 12, color: "var(--text-muted)" }}>{t("schedule:quiet.priority")}</label>
              <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: quietEnabled ? "pointer" : "default", height: 36 }}>
                <input type="checkbox" checked={quietNice} disabled={!quietEnabled}
                  onChange={() => setQuietNice(!quietNice)}
                  style={{ accentColor: "var(--accent)" }} />
                <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>{t("schedule:quiet.lowerPriority")}</span>
              </label>
            </div>
          </div>

          {quietEnabled && (
            <div style={{ fontSize: 12, color: "var(--text-muted)", background: "var(--bg-primary)", padding: 10, borderRadius: 4, marginBottom: 12 }}>
              {quietStart > quietEnd
                ? t("schedule:quiet.summaryActiveOvernight", { start: quietStart, end: quietEnd })
                : t("schedule:quiet.summaryActive", { start: quietStart, end: quietEnd })}
              {t("schedule:quiet.summaryParallel", { count: quietParallel })}
              {quietNice ? t("schedule:quiet.summaryNice") : ""}
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
              toast(t("schedule:quiet.toastSaved"), "success");
            }}>
            {t("schedule:quiet.save")}
          </button>
        </div>
      </div>

      {/* Stream-Aware Scheduling */}
      <div style={{ background: "var(--bg-card)", padding: 20, borderRadius: 6, marginTop: 12 }}>
        <h3 style={{ color: "var(--text-primary)", marginBottom: 8 }}>{t("schedule:streams.title")}</h3>
        <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 16 }}>
          <Trans t={t} i18nKey="schedule:streams.description" components={{ em: <em /> }} />
        </div>

        {/* Plex */}
        <div style={{ borderTop: "1px solid var(--border)", paddingTop: 14, marginBottom: 14 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <span style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>Plex</span>
            <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
              <span style={{ fontSize: 13, color: "var(--text-muted)" }}>{plexPauseEnabled ? t("schedule:enabled") : t("schedule:disabled")}</span>
              <input type="checkbox" checked={plexPauseEnabled}
                onChange={(e) => setPlexPauseEnabled(e.target.checked)}
                style={{ accentColor: "var(--accent)", width: 18, height: 18 }} />
            </label>
          </div>
          <div style={{ opacity: plexPauseEnabled ? 1 : 0.4, transition: "opacity 0.2s" }}>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 16, marginBottom: 10 }}>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <label style={{ fontSize: 12, color: "var(--text-muted)" }}>{t("schedule:streams.threshold")}</label>
                <input type="number" min={1} max={20} style={inputStyle} disabled={!plexPauseEnabled}
                  value={plexPauseThreshold}
                  onChange={e => setPlexPauseThreshold(Number(e.target.value))} />
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <label style={{ fontSize: 12, color: "var(--text-muted)" }}>{t("schedule:streams.countOnly")}</label>
                <select style={inputStyle} disabled={!plexPauseEnabled}
                  value={plexPauseTranscodeOnly ? "transcode" : "all"}
                  onChange={e => setPlexPauseTranscodeOnly(e.target.value === "transcode")}>
                  <option value="transcode">{t("schedule:streams.optionTranscode")}</option>
                  <option value="all">{t("schedule:streams.optionAll")}</option>
                </select>
              </div>
            </div>
            {plexPauseEnabled && (
              <div style={{ fontSize: 12, color: "var(--text-muted)", background: "var(--bg-primary)", padding: 10, borderRadius: 4 }}>
                {t(plexPauseTranscodeOnly ? "schedule:streams.summaryTranscode" : "schedule:streams.summaryAll", { count: plexPauseThreshold })}
                {plexPauseTranscodeOnly && t("schedule:streams.directPlayNote")}
                {" "}{t("schedule:streams.checksEvery")}
              </div>
            )}
          </div>
        </div>

        {/* Jellyfin */}
        <div style={{ borderTop: "1px solid var(--border)", paddingTop: 14, marginBottom: 14 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <span style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>Jellyfin</span>
            <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
              <span style={{ fontSize: 13, color: "var(--text-muted)" }}>{jellyfinPauseEnabled ? t("schedule:enabled") : t("schedule:disabled")}</span>
              <input type="checkbox" checked={jellyfinPauseEnabled}
                onChange={(e) => setJellyfinPauseEnabled(e.target.checked)}
                style={{ accentColor: "var(--accent)", width: 18, height: 18 }} />
            </label>
          </div>
          <div style={{ opacity: jellyfinPauseEnabled ? 1 : 0.4, transition: "opacity 0.2s" }}>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 16, marginBottom: 10 }}>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <label style={{ fontSize: 12, color: "var(--text-muted)" }}>{t("schedule:streams.threshold")}</label>
                <input type="number" min={1} max={20} style={inputStyle} disabled={!jellyfinPauseEnabled}
                  value={jellyfinPauseThreshold}
                  onChange={e => setJellyfinPauseThreshold(Number(e.target.value))} />
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <label style={{ fontSize: 12, color: "var(--text-muted)" }}>{t("schedule:streams.countOnly")}</label>
                <select style={inputStyle} disabled={!jellyfinPauseEnabled}
                  value={jellyfinPauseTranscodeOnly ? "transcode" : "all"}
                  onChange={e => setJellyfinPauseTranscodeOnly(e.target.value === "transcode")}>
                  <option value="transcode">{t("schedule:streams.optionTranscode")}</option>
                  <option value="all">{t("schedule:streams.optionAll")}</option>
                </select>
              </div>
            </div>
            {jellyfinPauseEnabled && (
              <div style={{ fontSize: 12, color: "var(--text-muted)", background: "var(--bg-primary)", padding: 10, borderRadius: 4 }}>
                {t(jellyfinPauseTranscodeOnly ? "schedule:streams.summaryTranscode" : "schedule:streams.summaryAll", { count: jellyfinPauseThreshold })}
                {jellyfinPauseTranscodeOnly && t("schedule:streams.directPlayNote")}
                {" "}{t("schedule:streams.checksEvery")}
              </div>
            )}
          </div>
        </div>

        {/* Emby */}
        <div style={{ borderTop: "1px solid var(--border)", paddingTop: 14, marginBottom: 16 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <span style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>Emby</span>
            <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
              <span style={{ fontSize: 13, color: "var(--text-muted)" }}>{embyPauseEnabled ? t("schedule:enabled") : t("schedule:disabled")}</span>
              <input type="checkbox" checked={embyPauseEnabled}
                onChange={(e) => setEmbyPauseEnabled(e.target.checked)}
                style={{ accentColor: "var(--accent)", width: 18, height: 18 }} />
            </label>
          </div>
          <div style={{ opacity: embyPauseEnabled ? 1 : 0.4, transition: "opacity 0.2s" }}>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))", gap: 16, marginBottom: 10 }}>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <label style={{ fontSize: 12, color: "var(--text-muted)" }}>{t("schedule:streams.threshold")}</label>
                <input type="number" min={1} max={20} style={inputStyle} disabled={!embyPauseEnabled}
                  value={embyPauseThreshold}
                  onChange={e => setEmbyPauseThreshold(Number(e.target.value))} />
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <label style={{ fontSize: 12, color: "var(--text-muted)" }}>{t("schedule:streams.countOnly")}</label>
                <select style={inputStyle} disabled={!embyPauseEnabled}
                  value={embyPauseTranscodeOnly ? "transcode" : "all"}
                  onChange={e => setEmbyPauseTranscodeOnly(e.target.value === "transcode")}>
                  <option value="transcode">{t("schedule:streams.optionTranscode")}</option>
                  <option value="all">{t("schedule:streams.optionAll")}</option>
                </select>
              </div>
            </div>
            {embyPauseEnabled && (
              <div style={{ fontSize: 12, color: "var(--text-muted)", background: "var(--bg-primary)", padding: 10, borderRadius: 4 }}>
                {t(embyPauseTranscodeOnly ? "schedule:streams.summaryTranscode" : "schedule:streams.summaryAll", { count: embyPauseThreshold })}
                {embyPauseTranscodeOnly && t("schedule:streams.directPlayNote")}
                {" "}{t("schedule:streams.checksEvery")}
              </div>
            )}
          </div>
        </div>

        <button className="btn btn-primary"
          onClick={async () => {
            await updateEncodingSettings({
              plex_pause_on_stream: plexPauseEnabled,
              plex_pause_stream_threshold: String(plexPauseThreshold),
              plex_pause_transcode_only: plexPauseTranscodeOnly,
              jellyfin_pause_on_stream: jellyfinPauseEnabled,
              jellyfin_pause_stream_threshold: String(jellyfinPauseThreshold),
              jellyfin_pause_transcode_only: jellyfinPauseTranscodeOnly,
              emby_pause_on_stream: embyPauseEnabled,
              emby_pause_stream_threshold: String(embyPauseThreshold),
              emby_pause_transcode_only: embyPauseTranscodeOnly,
            });
            toast(t("schedule:streams.toastSaved"), "success");
          }}>
          {t("schedule:streams.save")}
        </button>
      </div>
    </div>
  );
}
