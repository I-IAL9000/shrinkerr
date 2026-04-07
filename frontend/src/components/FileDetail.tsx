import { useState, useEffect } from "react";
import type { ScannedFile, AudioTrack, SubtitleTrack } from "../types";
import { getTracksByPath } from "../api";
import AudioTrackRow from "./AudioTrackRow";

interface FileDetailProps {
  file: ScannedFile;
  onToggleTrack: (filePath: string, streamIndex: number) => void;
  onToggleSubTrack?: (filePath: string, streamIndex: number) => void;
}

export default function FileDetail({ file, onToggleTrack, onToggleSubTrack }: FileDetailProps) {
  const [fetchedAudio, setFetchedAudio] = useState<AudioTrack[]>([]);
  const [fetchedSubs, setFetchedSubs] = useState<SubtitleTrack[]>([]);
  const [loading, setLoading] = useState(!file.audio_tracks?.length);

  useEffect(() => {
    if (file.audio_tracks?.length) return; // Already have tracks from parent
    setLoading(true);
    getTracksByPath(file.file_path).then((data) => {
      setFetchedAudio(data.audio_tracks || []);
      setFetchedSubs(data.subtitle_tracks || []);
      setLoading(false);
    }).catch(() => setLoading(false));
  }, [file.file_path]);

  // Use parent-provided tracks (which update on toggle), fallback to fetched
  const audioTracks = file.audio_tracks?.length ? file.audio_tracks : fetchedAudio;
  const subtitleTracks = file.subtitle_tracks?.length ? file.subtitle_tracks : fetchedSubs;

  const convSavings = file.needs_conversion ? file.file_size * 0.3 : 0;

  return (
    <div className="file-detail">
      <div style={{ color: "var(--text-muted)", marginBottom: 4 }}>
        {file.video_codec} &middot; {file.file_size_gb} GB
      </div>
      <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6 }}>
        Native language: <strong style={{ color: file.language_source === "api" ? "var(--success)" : "var(--text-secondary)" }}>
          {file.native_language.toUpperCase()}
        </strong>
        <span style={{
          fontSize: 9, marginLeft: 4, padding: "1px 4px", borderRadius: 3,
          background: file.language_source === "api" ? "rgba(0,200,100,0.15)" : "var(--border)",
          color: file.language_source === "api" ? "var(--success)" : "var(--text-muted)",
        }}>
          {file.language_source === "api" ? "from API" : "heuristic"}
        </span>
      </div>
      {file.needs_conversion && (
        <div style={{ color: "var(--success)", marginBottom: 6 }}>
          Convert to x265 10-bit (est. save ~{(convSavings / (1024**3)).toFixed(1)} GB)
        </div>
      )}
      {loading ? (
        <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 0" }}>
          <div className="spinner" style={{ width: 14, height: 14 }} />
          <span style={{ fontSize: 12, color: "var(--text-muted)" }}>Loading tracks...</span>
        </div>
      ) : (
        <>
          {audioTracks.length > 0 && (
            <>
              <div style={{ marginBottom: 2 }}>Audio tracks:</div>
              <div style={{ paddingLeft: 12 }}>
                {audioTracks.map((track) => (
                  <AudioTrackRow
                    key={track.stream_index}
                    track={track}
                    onToggle={(idx) => onToggleTrack(file.file_path, idx)}
                  />
                ))}
              </div>
            </>
          )}
          {subtitleTracks.length > 0 && (
            <>
              <div style={{ marginTop: 8, marginBottom: 2 }}>Subtitle tracks:</div>
              <div style={{ paddingLeft: 12 }}>
                {subtitleTracks.map((track) => (
                  <div key={track.stream_index} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, padding: "2px 0" }}>
                    {track.locked ? (
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, opacity: 0.5 }}>
                        <rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/>
                      </svg>
                    ) : (
                      <input
                        type="checkbox"
                        checked={!track.keep}
                        readOnly
                        onClick={(e) => { e.stopPropagation(); onToggleSubTrack?.(file.file_path, track.stream_index); }}
                        style={{ accentColor: "var(--accent)" }}
                      />
                    )}
                    <span style={{ color: track.keep ? "var(--text-secondary)" : "var(--text-muted)", textDecoration: track.keep ? "none" : "line-through" }}>
                      {track.language.toUpperCase()} — {track.codec}
                      {track.title && ` — ${track.title}`}
                      {track.forced && <span style={{ fontSize: 9, color: "var(--warning)", marginLeft: 4 }}>FORCED</span>}
                    </span>
                    {track.locked && <span style={{ opacity: 0.4, fontSize: 10 }}>(always keep)</span>}
                  </div>
                ))}
              </div>
            </>
          )}
        </>
      )}
      <div style={{ color: "var(--success)", marginTop: 6, fontSize: 11 }}>
        Total est. savings: ~{file.estimated_savings_gb} GB
      </div>
    </div>
  );
}
