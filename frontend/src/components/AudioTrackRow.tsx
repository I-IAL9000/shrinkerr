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
        <span className="lock-icon">&#x1f512;</span>
        <span>{track.language}</span>
        <span>&mdash; {track.codec} {track.channels > 0 ? channelLabel : ""}</span>
        {track.title && <span style={{ opacity: 0.5 }}>&quot;{track.title}&quot;</span>}
        <span style={{ opacity: 0.4, fontSize: 10 }}>(always keep)</span>
      </div>
    );
  }

  return (
    <div className="audio-track-row">
      <input
        type="checkbox"
        checked={!track.keep}
        onChange={() => onToggle(track.stream_index)}
        style={{ accentColor: "var(--accent)" }}
      />
      <span>{track.language}</span>
      <span>&mdash; {track.codec} {track.channels > 0 ? channelLabel : ""}</span>
      {track.title && <span style={{ opacity: 0.5 }}>&quot;{track.title}&quot;</span>}
      <span className="track-size">{sizeLabel}</span>
    </div>
  );
}
