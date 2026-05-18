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
    </div>
  );
}
