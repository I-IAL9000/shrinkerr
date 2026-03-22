import type { Job } from "../types";

interface JobListItemProps {
  job: Job;
  onCancel: (id: number) => void;
  onRetry?: (id: number) => void;
  onRemove: (id: number) => void;
}

function formatBytes(bytes: number): string {
  const gb = bytes / (1024 ** 3);
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${(bytes / (1024 ** 2)).toFixed(0)} MB`;
}

export default function JobListItem({ job, onCancel, onRetry, onRemove }: JobListItemProps) {
  const fileName = job.file_path.split("/").pop() || job.file_path;
  const typeBadge = job.job_type === "combined" ? "Convert + Audio"
    : job.job_type === "convert" ? "Convert" : "Audio only";

  return (
    <div className="job-row">
      {job.status === "completed" && <span style={{ color: "var(--success)", width: 20 }}>&#x2713;</span>}
      {job.status === "failed" && <span style={{ color: "var(--accent)", width: 20 }}>&#x2717;</span>}
      {job.status === "pending" && <span style={{ width: 20, opacity: 0.3 }}>&middot;</span>}
      <span style={{ flex: 1 }}>{fileName}</span>
      {job.status === "completed" && (
        <>
          <span style={{ color: "var(--success)", fontSize: 11 }}>saved {formatBytes(job.space_saved)}</span>
          <button onClick={() => onRemove(job.id)}
            style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", marginLeft: 8 }}>
            &times;
          </button>
        </>
      )}
      {job.status === "failed" && (
        <>
          {onRetry && (
            <button className="btn btn-secondary" style={{ fontSize: 11, padding: "2px 8px" }}
              onClick={() => onRetry(job.id)}>Retry</button>
          )}
          <button onClick={() => onRemove(job.id)}
            style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", marginLeft: 8 }}>
            &times;
          </button>
        </>
      )}
      {job.status === "pending" && (
        <>
          <span className="job-type-badge" style={{ background: "var(--border)" }}>{typeBadge}</span>
          <button onClick={() => onCancel(job.id)}
            style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", marginLeft: 8 }}>
            &times;
          </button>
        </>
      )}
    </div>
  );
}
