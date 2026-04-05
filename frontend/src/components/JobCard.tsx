import type { JobProgress } from "../types";
import ProgressBar from "./ProgressBar";
import { useConfirm } from "./ConfirmModal";

function formatEta(seconds: number | null): string {
  if (!seconds) return "";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m} min`;
}

function formatBytes(bytes: number): string {
  if (bytes >= 1024 ** 4) return `${(bytes / (1024 ** 4)).toFixed(2)} TB`;
  const gb = bytes / (1024 ** 3);
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${(bytes / (1024 ** 2)).toFixed(0)} MB`;
}

interface JobCardProps {
  progress: JobProgress;
  jobIndex?: number;
  fileSize?: number;
  nvencPreset?: string | null;
  nvencCq?: number | null;
  jobType?: string | null;
  audioCodec?: string | null;
  audioBitrate?: number | null;
  audioTracksToRemove?: number[];
  subtitleTracksToRemove?: number[];
  removedTrackLangs?: string[];
  losslessCodec?: string | null;
  losslessBitrate?: number | null;
  onCancel?: () => void;
}

export default function JobCard({ progress, jobIndex, fileSize, nvencPreset, nvencCq, jobType, audioCodec, audioBitrate, audioTracksToRemove, subtitleTracksToRemove, removedTrackLangs, losslessCodec, losslessBitrate, onCancel }: JobCardProps) {
  const confirm = useConfirm();
  return (
    <div className="job-active">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
        <span style={{ color: "white", fontWeight: "bold" }}>Now {progress.step || "Processing"}</span>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ color: "var(--accent)" }}>
            Job {progress.jobs_completed + (jobIndex ?? 0) + 1} of {progress.jobs_total}
          </span>
          {onCancel && (
            <button
              onClick={async () => {
                const ok = await confirm({ message: "Cancel the current conversion? The temp file will be deleted and the original kept.", confirmLabel: "Cancel conversion", danger: true });
                if (ok) onCancel();
              }}
              style={{
                background: "none", border: "1px solid rgba(233,69,96,0.4)",
                color: "#e94560", cursor: "pointer", borderRadius: 4,
                padding: "2px 8px", fontSize: 11,
              }}
              title="Cancel current conversion"
            >
              Cancel
            </button>
          )}
        </div>
      </div>
      <div style={{ marginBottom: 8, fontSize: 13, display: "flex", alignItems: "center", gap: 8 }}>
        <span>{progress.file_name}</span>
        {fileSize != null && fileSize > 0 && (
          <span style={{ fontSize: 11, opacity: 0.5 }}>{formatBytes(fileSize)}</span>
        )}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 6 }}>
        <div style={{ flex: 1 }}><ProgressBar progress={progress.progress} /></div>
        <span style={{ color: "var(--success)", fontWeight: "bold" }}>
          {progress.step === "vmaf analysis" ? `VMAF ${progress.progress.toFixed(0)}%` : `${progress.progress.toFixed(1)}%`}
        </span>
      </div>
      <div style={{ display: "flex", gap: 16, fontSize: 11, opacity: 0.6, flexWrap: "wrap" }}>
        <span>{progress.step === "vmaf analysis" ? "Analyzing quality..." : progress.step}</span>
        {nvencPreset && (jobType === "convert" || jobType === "combined") && (
          <span>{nvencPreset.toUpperCase()} / CQ {nvencCq}</span>
        )}
        {audioCodec && audioCodec !== "copy" && (jobType === "audio" || jobType === "combined") && (
          <span>Audio: {audioCodec.toUpperCase()} / {audioBitrate}k</span>
        )}
        {losslessCodec && losslessCodec !== "copy" && (
          <span>Lossless → {losslessCodec.toUpperCase()} / {losslessBitrate}k</span>
        )}
        {audioTracksToRemove && audioTracksToRemove.length > 0 && (
          <span style={{ color: "#ff6b9d" }}>
            Removing {audioTracksToRemove.length} audio track{audioTracksToRemove.length !== 1 ? "s" : ""}
            {removedTrackLangs && removedTrackLangs.length > 0
              ? ` (${removedTrackLangs.join(", ")})`
              : ` (streams: ${audioTracksToRemove.join(", ")})`}
          </span>
        )}
        {subtitleTracksToRemove && subtitleTracksToRemove.length > 0 && (
          <span style={{ color: "#ffa94d" }}>
            Removing {subtitleTracksToRemove.length} subtitle{subtitleTracksToRemove.length !== 1 ? "s" : ""}
          </span>
        )}
        {progress.fps && <span>{progress.fps.toFixed(0)} fps</span>}
        {progress.eta && <span>ETA: {formatEta(progress.eta)}</span>}
        {progress.total_saved > 0 && (
          <span>Saved: {formatBytes(progress.total_saved)}</span>
        )}
      </div>
    </div>
  );
}
