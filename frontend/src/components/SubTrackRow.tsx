import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { SubtitleTrack } from "../types";
import { languageOptions } from "../utils/languages";
import { detectNote } from "../i18n/server";

export default function SubTrackRow({ track, filePath, onToggle, isExternal, onSetLanguage, busy }: {
  track: SubtitleTrack;
  filePath: string;
  onToggle?: (filePath: string, streamIndex: number) => void;
  isExternal?: boolean;
  onSetLanguage?: (streamIndex: number, language: string) => void;
  busy?: boolean;
}) {
  const { t } = useTranslation(["fileDetail", "common"]);
  const [editing, setEditing] = useState(false);
  const basename = isExternal && track.external_path
    ? track.external_path.split("/").pop() || track.title
    : null;

  // v0.5.16: lock branch removed (issue #11). Every track renders as an
  // editable checkbox so users can override always-keep defaults. See
  // AudioTrackRow.tsx + scanner.classify_audio_tracks for the smart-
  // selection logic that picks defaults.
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, padding: "2px 0" }}>
      <input
        type="checkbox"
        checked={!track.keep}
        onChange={() => onToggle?.(filePath, track.stream_index)}
        onClick={(e) => e.stopPropagation()}
        style={{ accentColor: "var(--accent)", cursor: "pointer" }}
      />
      <span style={{ color: track.keep ? "var(--text-secondary)" : "var(--text-muted)", textDecoration: track.keep ? "none" : "line-through" }}>
        {track.language.toUpperCase()} — {track.codec}
        {track.title && !isExternal && ` — ${track.title}`}
        {track.forced && <span style={{ fontSize: 9, color: "var(--warning)", marginLeft: 4 }}>{t("fileDetail:subtitles.forced")}</span>}
      </span>
      {isExternal && basename && (
        <span style={{ fontSize: 10, color: "var(--text-muted)" }}>{basename}</span>
      )}
      {track.manual && <span className="track-manual" title={t("fileDetail:tracks.manualTitle")}>{t("fileDetail:tracks.manual")}</span>}
      {track.detect_note && !track.detected_language && (track.language || "und").toLowerCase() === "und" && (
        <span style={{ fontSize: 10, color: "var(--warning)" }} title={t("fileDetail:tracks.detectNoteTitle")}>
          {detectNote(track)}
        </span>
      )}
      {onSetLanguage && !editing && (
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); setEditing(true); }}
          disabled={busy}
          title={t("fileDetail:tracks.setLanguageManually")}
          style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: busy ? "wait" : "pointer", padding: 0, display: "inline-flex", alignItems: "center", opacity: busy ? 0.5 : 1 }}
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/>
          </svg>
        </button>
      )}
      {onSetLanguage && editing && (
        <select aria-label={t("fileDetail:tracks.setLanguageManually")}
          autoFocus
          defaultValue={(track.language || "und").toLowerCase()}
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => { const v = e.target.value; setEditing(false); if (v && v !== (track.language || "und").toLowerCase()) onSetLanguage(track.stream_index, v); }}
          onBlur={() => setEditing(false)}
          style={{ background: "var(--bg-tertiary)", color: "var(--text-primary)", border: "1px solid var(--accent)", borderRadius: 4, fontSize: 11, padding: "1px 4px" }}
        >
          {languageOptions().map((l) => <option key={l.code} value={l.code}>{l.name} ({l.code})</option>)}
        </select>
      )}
    </div>
  );
}
