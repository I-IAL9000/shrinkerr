import type { ScannedFile } from "../types";
import AudioTrackRow from "./AudioTrackRow";

interface FileDetailProps {
  file: ScannedFile;
  onToggleTrack: (filePath: string, streamIndex: number) => void;
}

export default function FileDetail({ file, onToggleTrack }: FileDetailProps) {
  const convSavings = file.needs_conversion ? file.file_size * 0.3 : 0;

  return (
    <div className="file-detail">
      <div style={{ color: "var(--text-muted)", marginBottom: 4 }}>
        {file.video_codec} &middot; {file.file_size_gb} GB
      </div>
      {file.needs_conversion && (
        <div style={{ color: "var(--success)", marginBottom: 6 }}>
          Convert to x265 10-bit (est. save ~{(convSavings / (1024**3)).toFixed(1)} GB)
        </div>
      )}
      <div style={{ marginBottom: 2 }}>Audio tracks:</div>
      <div style={{ paddingLeft: 12 }}>
        {file.audio_tracks.map((track) => (
          <AudioTrackRow
            key={track.stream_index}
            track={track}
            onToggle={(idx) => onToggleTrack(file.file_path, idx)}
          />
        ))}
      </div>
      <div style={{ color: "var(--success)", marginTop: 6, fontSize: 11 }}>
        Total est. savings: ~{file.estimated_savings_gb} GB
      </div>
    </div>
  );
}
