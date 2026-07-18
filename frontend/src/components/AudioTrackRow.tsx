import { useState } from "react";
import type { AudioTrack } from "../types";
import { LANGUAGES } from "../utils/languages";

interface AudioTrackRowProps {
  track: AudioTrack;
  onToggle: (streamIndex: number) => void;
  onSetLanguage?: (streamIndex: number, language: string) => void;
}

export default function AudioTrackRow({ track, onToggle, onSetLanguage }: AudioTrackRowProps) {
  const [editing, setEditing] = useState(false);
  const sizeLabel = track.size_estimate_bytes
    ? `(~${(track.size_estimate_bytes / (1024 * 1024)).toFixed(0)} MB)`
    : "";

  const channelLabel = track.channels === 6 ? "5.1" : track.channels === 8 ? "7.1" : `${track.channels}.0`;

  // v0.5.16: lock-rendering removed. Pre-v0.5.16 tracks in an
  // always-keep language rendered a lock icon and could not be
  // toggled — users couldn't delete duplicate-language tracks (issue
  // #11). Now every track is an editable checkbox; the backend
  // pre-checks the per-language-best track by default via
  // classify_audio_tracks(), and the user can override.
  const removeStyle = !track.keep ? { color: "var(--text-muted)", textDecoration: "line-through" as const } : {};

  return (
    <div className="audio-track-row">
      <input
        type="checkbox"
        checked={!track.keep}
        onChange={() => onToggle(track.stream_index)}
        onClick={(e) => e.stopPropagation()}
        style={{ accentColor: "var(--accent)", cursor: "pointer" }}
      />
      <span style={removeStyle}>{track.language}</span>
      <span style={removeStyle}>&mdash; {track.codec} {track.channels > 0 ? channelLabel : ""}</span>
      {track.title && <span style={{ opacity: 0.5, ...removeStyle }}>&quot;{track.title}&quot;</span>}
      <span className="track-size" style={removeStyle}>{sizeLabel}</span>
      {/* v0.9.35: a language was detected but the container (AVI etc.) can't
          store it in place — it applies when the file is converted to mkv. */}
      {track.detected_language && (track.language || "und").toLowerCase() === "und" && (
        <span style={{ color: "var(--accent)", fontSize: "0.85em", whiteSpace: "nowrap" }}>
          {track.detected_language.toUpperCase()} detected → convert to MKV to apply
        </span>
      )}
      {/* v0.9.44: why detection couldn't resolve this track. */}
      {track.detect_note && !track.detected_language && (track.language || "und").toLowerCase() === "und" && (
        <span style={{ color: "var(--warning)", fontSize: "0.85em", opacity: 0.8 }} title="Why auto-detection didn't set a language">
          {track.detect_note}
        </span>
      )}
      {/* v0.9.43: manual language override — for tracks detection can't
          resolve. Pencil toggles an inline picker; choosing a language saves. */}
      {onSetLanguage && !editing && (
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); setEditing(true); }}
          title="Set language manually"
          style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", padding: 0, fontSize: 12, display: "inline-flex", alignItems: "center" }}
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/>
          </svg>
        </button>
      )}
      {onSetLanguage && editing && (
        <select
          autoFocus
          defaultValue={(track.language || "und").toLowerCase()}
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => { const v = e.target.value; setEditing(false); if (v && v !== (track.language || "und").toLowerCase()) onSetLanguage(track.stream_index, v); }}
          onBlur={() => setEditing(false)}
          style={{ background: "var(--bg-tertiary)", color: "var(--text-primary)", border: "1px solid var(--accent)", borderRadius: 4, fontSize: 11, padding: "1px 4px" }}
        >
          {LANGUAGES.map((l) => <option key={l.code} value={l.code}>{l.name} ({l.code})</option>)}
        </select>
      )}
    </div>
  );
}
