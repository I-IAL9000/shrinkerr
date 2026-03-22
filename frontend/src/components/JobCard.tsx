import type { JobProgress } from "../types";
import ProgressBar from "./ProgressBar";

function formatEta(seconds: number | null): string {
  if (!seconds) return "";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return `${m} min`;
}

function formatBytes(bytes: number): string {
  const gb = bytes / (1024 ** 3);
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${(bytes / (1024 ** 2)).toFixed(0)} MB`;
}

export default function JobCard({ progress }: { progress: JobProgress }) {
  return (
    <div className="job-active">
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
        <span style={{ color: "white", fontWeight: "bold" }}>Now {progress.step || "Processing"}</span>
        <span style={{ color: "var(--accent)" }}>
          Job {progress.jobs_completed + 1} of {progress.jobs_total}
        </span>
      </div>
      <div style={{ marginBottom: 8, fontSize: 13 }}>{progress.file_name}</div>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 6 }}>
        <div style={{ flex: 1 }}><ProgressBar progress={progress.progress} /></div>
        <span style={{ color: "var(--success)", fontWeight: "bold" }}>
          {progress.progress.toFixed(1)}%
        </span>
      </div>
      <div style={{ display: "flex", gap: 16, fontSize: 11, opacity: 0.6 }}>
        <span>{progress.step}</span>
        {progress.fps && <span>{progress.fps.toFixed(0)} fps</span>}
        {progress.eta && <span>ETA: {formatEta(progress.eta)}</span>}
        {progress.total_saved > 0 && (
          <span>Saved: {formatBytes(progress.total_saved)}</span>
        )}
      </div>
    </div>
  );
}
