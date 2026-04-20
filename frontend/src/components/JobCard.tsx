import { memo } from "react";
import type { JobProgress } from "../types";
import ProgressBar from "./ProgressBar";
import { useConfirm } from "./ConfirmModal";
import { fmtNum } from "../fmt";

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
  encoder?: string | null;
  libx265Preset?: string | null;
  libx265Crf?: number | null;
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

function JobCardImpl({ progress, jobIndex, fileSize, nvencPreset, nvencCq, encoder, libx265Preset, libx265Crf, jobType, audioCodec, audioBitrate, audioTracksToRemove, subtitleTracksToRemove, removedTrackLangs, losslessCodec, losslessBitrate, onCancel }: JobCardProps) {
  const confirm = useConfirm();
  return (
    <div className="job-active">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
        <span style={{ color: "white", fontWeight: "bold" }}>Now {progress.step || "Processing"}</span>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <span style={{ color: "var(--accent)" }}>
            Job {fmtNum(progress.jobs_completed + (jobIndex ?? 0) + 1)} of {fmtNum(progress.jobs_total)}
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
      <div style={{ marginBottom: 8, fontSize: 13, display: "flex", alignItems: "baseline", gap: 8, minWidth: 0 }}>
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0, flex: 1 }}>{progress.file_name}</span>
        {fileSize != null && fileSize > 0 && (
          <span style={{ fontSize: 11, opacity: 0.5, flexShrink: 0 }}>{formatBytes(fileSize)}</span>
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
        {(jobType === "convert" || jobType === "combined") && (
          encoder === "libx265" ? (
            <span>{libx265Preset || "medium"} / CRF {libx265Crf ?? 20}</span>
          ) : nvencPreset ? (
            <span>{nvencPreset.toUpperCase()} / CQ {nvencCq}</span>
          ) : null
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
        {progress.node_name && (
          <span style={{ color: "var(--text-muted)" }}>on {progress.node_name}</span>
        )}
      </div>
    </div>
  );
}

// Memoize: the whole card only needs to re-render when the `progress` object
// changes (identity check — App.tsx creates a new JobProgress per message)
// or the display-only config props change. Parent callbacks (onCancel) are
// stable in behavior even if their identity changes each render.
const JobCard = memo(JobCardImpl, (prev, next) => {
  if (prev.progress !== next.progress) return false;
  if (prev.jobIndex !== next.jobIndex) return false;
  if (prev.fileSize !== next.fileSize) return false;
  if (prev.nvencPreset !== next.nvencPreset) return false;
  if (prev.nvencCq !== next.nvencCq) return false;
  if (prev.encoder !== next.encoder) return false;
  if (prev.libx265Preset !== next.libx265Preset) return false;
  if (prev.libx265Crf !== next.libx265Crf) return false;
  if (prev.jobType !== next.jobType) return false;
  if (prev.audioCodec !== next.audioCodec) return false;
  if (prev.audioBitrate !== next.audioBitrate) return false;
  if (prev.losslessCodec !== next.losslessCodec) return false;
  if (prev.losslessBitrate !== next.losslessBitrate) return false;
  // Track-removal arrays and removedTrackLangs come from QueuePage.parseJobs,
  // which JSON.parse()s fresh arrays on every 10s poll. A reference check
  // would force a re-render every poll; compare by length + element-wise
  // instead so identical contents short-circuit.
  if (!sameArr(prev.audioTracksToRemove, next.audioTracksToRemove)) return false;
  if (!sameArr(prev.subtitleTracksToRemove, next.subtitleTracksToRemove)) return false;
  if (!sameArr(prev.removedTrackLangs, next.removedTrackLangs)) return false;
  return true;
});

function sameArr<T>(a: readonly T[] | undefined, b: readonly T[] | undefined): boolean {
  if (a === b) return true;
  const al = a?.length ?? 0;
  const bl = b?.length ?? 0;
  if (al !== bl) return false;
  if (!a || !b) return al === 0;
  for (let i = 0; i < al; i++) {
    if (a[i] !== b[i]) return false;
  }
  return true;
}

export default JobCard;
