import type { AudioTrack } from "../types";

interface AudioTrackRowProps {
  track: AudioTrack;
  onToggle: (streamIndex: number) => void;
}

export default function AudioTrackRow({ track, onToggle }: AudioTrackRowProps) {
  const sizeLabel = track.size_estimate_bytes
    ? `(~${(track.size_estimate_bytes / (1024 * 1024)).toFixed(0)} MB)`
    : "";

  const channelLabel = track.channels === 6 ? "5.1" : track.channels === 8 ? "7.1" : `${track.channels}.0`;

  if (track.locked) {
    return (
      <div className="audio-track-row">
        <span style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 16, flexShrink: 0 }}>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ opacity: 0.5 }}>
            <rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/>
          </svg>
        </span>
        <span>{track.language}</span>
        <span>&mdash; {track.codec} {track.channels > 0 ? channelLabel : ""}</span>
        {track.title && <span style={{ opacity: 0.5 }}>&quot;{track.title}&quot;</span>}
        <span style={{ opacity: 0.4, fontSize: 10 }}>(always keep)</span>
      </div>
    );
  }

  const removeStyle = !track.keep ? { color: "var(--text-muted)", textDecoration: "line-through" as const } : {};

  return (
    <div className="audio-track-row">
      <input
        type="checkbox"
        checked={!track.keep}
        readOnly
        onClick={(e) => { e.stopPropagation(); onToggle(track.stream_index); }}
        style={{ accentColor: "var(--accent)" }}
      />
      <span style={removeStyle}>{track.language}</span>
      <span style={removeStyle}>&mdash; {track.codec} {track.channels > 0 ? channelLabel : ""}</span>
      {track.title && <span style={{ opacity: 0.5, ...removeStyle }}>&quot;{track.title}&quot;</span>}
      <span className="track-size" style={removeStyle}>{sizeLabel}</span>
    </div>
  );
}
