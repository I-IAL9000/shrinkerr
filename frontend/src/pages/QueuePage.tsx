import { useState, useEffect } from "react";
import { getJobs, getJobStats, startQueue, pauseQueue, resumeQueue, cancelJob, removeJob, retryJob, clearCompleted } from "../api";
import JobCard from "../components/JobCard";
import JobListItem from "../components/JobListItem";
import ProgressBar from "../components/ProgressBar";
import type { Job, JobProgress } from "../types";

interface QueuePageProps {
  jobProgress: JobProgress | null;
}

export default function QueuePage({ jobProgress }: QueuePageProps) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [stats, setStats] = useState<any>(null);

  const load = async () => {
    const [jobData, s] = await Promise.all([getJobs(), getJobStats()]);
    const jobList = Array.isArray(jobData) ? jobData : [];
    setJobs(jobList.map((job: any) => ({
      ...job,
      file_name: job.file_path.split("/").pop(),
      audio_tracks_to_remove: typeof job.audio_tracks_to_remove === "string"
        ? JSON.parse(job.audio_tracks_to_remove || "[]")
        : (job.audio_tracks_to_remove || []),
    })));
    setStats(s);
  };

  // Reload when WebSocket progress arrives
  useEffect(() => { load(); }, [jobProgress]);

  // Poll every 3 seconds so the page stays current even without WebSocket
  useEffect(() => {
    const interval = setInterval(load, 3000);
    return () => clearInterval(interval);
  }, []);

  const running = jobs.filter((j) => j.status === "running");
  const pending = jobs.filter((j) => j.status === "pending");
  const completed = jobs.filter((j) => j.status === "completed");
  const failed = jobs.filter((j) => j.status === "failed");

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
        <h2 style={{ color: "white", fontSize: 20 }}>Queue</h2>
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn btn-primary" onClick={() => { startQueue(); load(); }}>Start</button>
          <button className="btn btn-secondary" onClick={() => { pauseQueue(); }}>Pause</button>
          <button className="btn btn-secondary" onClick={() => { resumeQueue(); }}>Resume</button>
          <button className="btn btn-secondary" onClick={() => { clearCompleted(); load(); }}>Clear done</button>
        </div>
      </div>

      {jobProgress && <JobCard progress={jobProgress} />}

      {!jobProgress && running.length > 0 && running.map((job) => (
        <div key={job.id} className="job-active" style={{ marginBottom: 16 }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
            <span style={{ color: "white", fontWeight: "bold" }}>Now Converting</span>
            <span style={{ fontSize: 12, opacity: 0.6 }}>
              {stats ? `Job ${stats.completed + 1} of ${stats.total_jobs}` : ""}
            </span>
          </div>
          <div style={{ marginBottom: 8, fontSize: 13 }}>{job.file_name}</div>
          <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 6 }}>
            <div style={{ flex: 1 }}><ProgressBar progress={job.progress || 0} /></div>
            <span style={{ color: "var(--success)", fontWeight: "bold" }}>
              {(job.progress || 0).toFixed(1)}%
            </span>
          </div>
          <div style={{ display: "flex", gap: 16, fontSize: 11, opacity: 0.6 }}>
            {job.fps != null && <span>{job.fps.toFixed(0)} fps</span>}
            {job.eta_seconds != null && job.eta_seconds > 0 && (
              <span>ETA: {job.eta_seconds >= 3600
                ? `${Math.floor(job.eta_seconds / 3600)}h ${Math.floor((job.eta_seconds % 3600) / 60)}m`
                : `${Math.floor(job.eta_seconds / 60)} min`}
              </span>
            )}
          </div>
        </div>
      ))}

      {pending.length > 0 && (
        <>
          <div style={{ fontSize: 12, opacity: 0.5, marginBottom: 8 }}>
            PENDING ({pending.length} remaining)
          </div>
          <div style={{ background: "var(--bg-primary)", borderRadius: 6, overflow: "hidden", marginBottom: 16 }}>
            {pending.map((job) => (
              <JobListItem key={job.id} job={job}
                onCancel={(id) => { cancelJob(id).then(load); }}
                onRemove={(id) => { removeJob(id).then(load); }}
              />
            ))}
          </div>
        </>
      )}

      {failed.length > 0 && (
        <>
          <div style={{ fontSize: 12, opacity: 0.5, marginBottom: 8, color: "var(--accent)" }}>
            FAILED ({failed.length})
          </div>
          <div style={{ background: "var(--bg-primary)", borderRadius: 6, overflow: "hidden", marginBottom: 16 }}>
            {failed.map((job) => (
              <JobListItem key={job.id} job={job}
                onCancel={(id) => { cancelJob(id).then(load); }}
                onRetry={(id) => { retryJob(id).then(load); }}
                onRemove={(id) => { removeJob(id).then(load); }}
              />
            ))}
          </div>
        </>
      )}

      {completed.length > 0 && (
        <>
          <div style={{ fontSize: 12, opacity: 0.5, marginBottom: 8 }}>
            COMPLETED ({completed.length} &middot; saved {stats ? (stats.total_space_saved / (1024**3)).toFixed(1) : 0} GB)
          </div>
          <div style={{ background: "var(--bg-primary)", borderRadius: 6, overflow: "hidden", opacity: 0.7 }}>
            {completed.map((job) => (
              <JobListItem key={job.id} job={job}
                onCancel={() => {}}
                onRemove={(id) => { removeJob(id).then(load); }}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
