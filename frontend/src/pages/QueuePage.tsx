import { useState, useEffect, useMemo, useRef } from "react";
import { getJobs, getJobStats, startQueue, pauseQueue, cancelJob, cancelCurrentJob, removeJob, retryJob, clearCompleted, clearPending, ignoreFile, bulkUpdateJobSettings, bulkMoveJobs, bulkIgnoreJobs, getEncodingSettings, getTracksByPath, reorderJobs, researchFilesBulk } from "../api";
import { fmtNum } from "../fmt";
import JobCard from "../components/JobCard";
import JobListItem from "../components/JobListItem";
import QueueControlPanel from "../components/QueueControlPanel";
import { useShiftSelect } from "../useShiftSelect";
import { useToast } from "../useToast";
import { useConfirm } from "../components/ConfirmModal";
import type { Job, JobProgress } from "../types";

interface QueuePageProps {
  jobProgressMap: Map<number, JobProgress>;
}

export default function QueuePage({ jobProgressMap }: QueuePageProps) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [stats, setStats] = useState<any>(null);
  const [tab, setTab] = useState<"pending" | "completed" | "failed">("pending");
  const toast = useToast();
  const confirm = useConfirm();

  const [initialLoading, setInitialLoading] = useState(true);
  const [tabLoading, setTabLoading] = useState(false);
  const [queueStarting, setQueueStarting] = useState(false);
  const [dragIdx, setDragIdx] = useState<number | null>(null);
  const [dropIdx, setDropIdx] = useState<number | null>(null);
  const [encodingDefaults, setEncodingDefaults] = useState<any>(null);

  // Load encoding defaults once
  useEffect(() => {
    getEncodingSettings().then(setEncodingDefaults).catch(() => {});
  }, []);

  const parseJobs = (data: any[]) =>
    (Array.isArray(data) ? data : []).map((job: any) => ({
      ...job,
      file_name: job.file_path.split("/").pop(),
      audio_tracks_to_remove: typeof job.audio_tracks_to_remove === "string"
        ? JSON.parse(job.audio_tracks_to_remove || "[]")
        : (job.audio_tracks_to_remove || []),
      subtitle_tracks_to_remove: typeof job.subtitle_tracks_to_remove === "string"
        ? JSON.parse(job.subtitle_tracks_to_remove || "[]")
        : (job.subtitle_tracks_to_remove || []),
    }));

  const loadingRef = useRef(false);

  const load = async () => {
    if (loadingRef.current) return; // Skip if already loading
    loadingRef.current = true;
    try {
      setTabLoading(true);
      const [s, runningData, tabData] = await Promise.all([
        getJobStats(),
        getJobs("running"),
        getJobs(tab),
      ]);
      setStats(s);
      const allJobs = [...parseJobs(runningData), ...parseJobs(tabData)];
      // Ensure pending jobs are available for spinner cards
      if (tab !== "pending" && runningData.length === 0 && s.pending > 0) {
        try {
          const pendingData = await getJobs("pending", 0, 10);
          allJobs.push(...parseJobs(pendingData));
        } catch {}
      }
      setJobs(allJobs);
      setInitialLoading(false);
      setTabLoading(false);
    } finally {
      loadingRef.current = false;
    }
  };

  // Load on mount and tab change
  useEffect(() => { load(); }, [tab]);

  // Poll every 10 seconds normally, every 2s while waiting for jobs to start
  useEffect(() => {
    const interval = setInterval(load, queueStarting ? 2000 : 10000);
    return () => clearInterval(interval);
  }, [tab, queueStarting]);

  const running = jobs.filter((j) => j.status === "running");

  const hasActiveJobs = queueStarting || jobProgressMap.size > 0 || running.length > 0;

  // Clear "starting" state once every running job has WebSocket progress
  const queueStartedAt = useRef<number>(0);
  useEffect(() => {
    if (queueStarting) queueStartedAt.current = Date.now();
  }, [queueStarting]);
  useEffect(() => {
    if (!queueStarting) return;
    // Don't clear for at least 3 seconds to ensure spinners are visible
    const elapsed = Date.now() - queueStartedAt.current;
    if (elapsed < 3000) return;
    const runningWithoutProgress = running.filter(j => !jobProgressMap.has(j.id)).length;
    if (running.length > 0 && runningWithoutProgress === 0) {
      setQueueStarting(false);
    }
  }, [queueStarting, running, jobProgressMap]);

  // Tab data is already filtered by status from the API
  const tabJobs = jobs.filter((j) => j.status === tab);
  // Use stats for counts (always accurate), not the filtered array
  const pendingCount = stats?.pending ?? 0;
  const completedCount = stats?.completed ?? 0;
  const failedCount = stats?.failed ?? 0;

  // Auto-switch to pending if failed tab becomes empty
  useEffect(() => {
    if (tab === "failed" && failedCount === 0) setTab("pending");
  }, [tab, failedCount]);

  // For pending tab: use tabJobs when on pending tab
  const pending = tab === "pending" ? tabJobs : jobs.filter(j => j.status === "pending");
  // Stable memo: only recalculate when the actual IDs change
  const pendingIdStr = pending.map((j) => j.id).join(",");
  const pendingIds = useMemo(() => pending.map((j) => j.id), [pendingIdStr]);
  const { selected: selectedJobIds, handleClick: handleJobClick, deselectAll, setSelected: setSelectedJobIds } = useShiftSelect(pendingIds);

  // Clear stale selections when pending list changes — only update if something was actually removed
  useEffect(() => {
    setSelectedJobIds((prev) => {
      if (prev.size === 0) return prev;
      const idSet = new Set(pendingIds);
      let changed = false;
      prev.forEach((id) => { if (!idSet.has(id)) changed = true; });
      if (!changed) return prev;
      const next = new Set<number>();
      prev.forEach((id) => { if (idSet.has(id as number)) next.add(id as number); });
      return next;
    });
  }, [pendingIdStr]);

  const selectedIds = Array.from(selectedJobIds) as number[];

  const handleBulkMove = async (position: "top" | "bottom" | "up" | "down") => {
    bulkMoveJobs(selectedIds, position).then(load);
    toast(`Moving ${selectedIds.length} job(s) ${position}`);
  };

  const handleBulkVideoPreset = async (preset: string, cq: number) => {
    // Optimistic UI update — apply immediately, then sync in background
    const selectedSet = new Set(selectedIds);
    setJobs(prev => prev.map(j =>
      selectedSet.has(j.id) ? { ...j, nvenc_preset: preset, nvenc_cq: cq } : j
    ));
    toast(`Video preset applied to ${selectedIds.length} job(s)`, "success");
    bulkUpdateJobSettings({ job_ids: selectedIds, nvenc_preset: preset, nvenc_cq: cq });
  };

  const handleBulkAudioPreset = async (codec: string, bitrate: number) => {
    const selectedSet = new Set(selectedIds);
    setJobs(prev => prev.map(j =>
      selectedSet.has(j.id) ? { ...j, audio_codec: codec, audio_bitrate: bitrate } : j
    ));
    bulkUpdateJobSettings({ job_ids: selectedIds, audio_codec: codec, audio_bitrate: bitrate });
    toast(`Audio preset applied to ${selectedIds.length} job(s)`, "success");
  };

  const handleBulkIgnore = async () => {
    if (!await confirm({ message: `Ignore ${selectedIds.length} selected job(s)?`, confirmLabel: "Ignore", danger: true })) return;
    const selectedSet = new Set(selectedIds);
    setJobs(prev => prev.filter(j => !selectedSet.has(j.id)));
    bulkIgnoreJobs(selectedIds as number[]);
    deselectAll();
    load();
    toast(`${selectedIds.length} job(s) ignored`);
  };

  const handleBulkRemove = async () => {
    if (!await confirm({ message: `Remove ${selectedIds.length} selected job(s) from queue?`, confirmLabel: "Remove", danger: true })) return;
    const selectedSet = new Set(selectedIds);
    setJobs(prev => prev.filter(j => !selectedSet.has(j.id)));
    for (const id of selectedIds) {
      removeJob(id as number).catch(() => {});
    }
    deselectAll();
    load();
    toast(`${selectedIds.length} job(s) removed`);
  };

  // Drag-and-drop reorder for pending queue
  const handleDragStart = (idx: number) => {
    setDragIdx(idx);
  };
  const handleDragOver = (e: React.DragEvent, idx: number) => {
    e.preventDefault();
    setDropIdx(idx);
  };
  const handleDrop = async (idx: number) => {
    if (dragIdx === null || dragIdx === idx) {
      setDragIdx(null);
      setDropIdx(null);
      return;
    }
    const reordered = [...tabJobs];
    const [moved] = reordered.splice(dragIdx, 1);
    reordered.splice(idx, 0, moved);
    // Optimistic update
    setJobs(prev => {
      const nonPending = prev.filter(j => j.status !== "pending");
      return [...nonPending, ...reordered];
    });
    setDragIdx(null);
    setDropIdx(null);
    // Persist to backend
    await reorderJobs(reordered.map(j => j.id));
  };
  const handleDragEnd = () => {
    setDragIdx(null);
    setDropIdx(null);
  };

  // Track cache for running jobs (keyed by file_path)
  const [trackCache, setTrackCache] = useState<Map<string, any[]>>(new Map());

  // Fetch track data for all running jobs
  useEffect(() => {
    for (const job of running) {
      if (!trackCache.has(job.file_path)) {
        getTracksByPath(job.file_path).then((data) => {
          setTrackCache(prev => {
            const next = new Map(prev);
            next.set(job.file_path, data.audio_tracks || []);
            return next;
          });
        }).catch(() => {});
      }
    }
  }, [running.map(j => j.file_path).join(",")]);

  if (initialLoading) {
    return (
      <div>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
          <h2 style={{ color: "var(--text-primary)", fontSize: 20 }}>Queue</h2>
        </div>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: 60 }}>
          <div className="spinner" />
          <div style={{ marginTop: 12, fontSize: 13, opacity: 0.5 }}>Loading queue...</div>
        </div>
      </div>
    );
  }

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
        <h2 style={{ color: "var(--text-primary)", fontSize: 20 }}>Queue</h2>
        {hasActiveJobs ? (
          <button className="btn btn-secondary" onClick={() => { pauseQueue(); toast("Queue paused"); }}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg> Pause
          </button>
        ) : (
          <button className="btn btn-primary" onClick={() => { setQueueStarting(true); startQueue().then(() => { load(); toast("Queue started", "success"); }); }}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21"/></svg> Start
          </button>
        )}
      </div>

      {/* Render a JobCard for each active job with progress */}
      {running.map((job, runIndex) => {
        const progress = jobProgressMap.get(job.id);
        if (!progress) return null; // shown as spinner below
        const tracks = trackCache.get(job.file_path) || [];
        const removeIndices = new Set(job.audio_tracks_to_remove || []);
        const removedLangs = tracks
          .filter((t: any) => removeIndices.has(t.stream_index))
          .map((t: any) => (t.language || "und").toLowerCase())
          .filter((v: string, i: number, a: string[]) => a.indexOf(v) === i);
        const LOSSLESS_CODECS = new Set(["truehd", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_bluray", "flac", "mlp", "pcm_dvd"]);
        const DTS_LOSSLESS_PROFILES = new Set(["dts-hd ma", "dts-hd hra"]);
        const hasLossless = tracks.some((t: any) => {
          const codec = (t.codec || "").toLowerCase();
          if (LOSSLESS_CODECS.has(codec)) return true;
          if (codec === "dts" && t.profile && DTS_LOSSLESS_PROFILES.has(t.profile.toLowerCase())) return true;
          return false;
        });
        return (
          <JobCard key={job.id} progress={progress}
            jobIndex={runIndex}
            fileSize={job.original_size}
            nvencPreset={job.nvenc_preset || encodingDefaults?.nvenc_preset || "p6"}
            nvencCq={job.nvenc_cq ?? encodingDefaults?.nvenc_cq ?? 20}
            encoder={job.encoder || encodingDefaults?.default_encoder || "nvenc"}
            libx265Preset={job.libx265_preset || encodingDefaults?.libx265_preset || "medium"}
            libx265Crf={job.libx265_crf ?? encodingDefaults?.libx265_crf ?? 20}
            jobType={job.job_type}
            audioCodec={job.audio_codec || encodingDefaults?.audio_codec || "copy"}
            audioBitrate={job.audio_bitrate ?? encodingDefaults?.audio_bitrate ?? 128}
            audioTracksToRemove={job.audio_tracks_to_remove}
            subtitleTracksToRemove={job.subtitle_tracks_to_remove}
            removedTrackLangs={removedLangs}
            losslessCodec={hasLossless && encodingDefaults?.auto_convert_lossless ? encodingDefaults?.lossless_target_codec : null}
            losslessBitrate={hasLossless && encodingDefaults?.auto_convert_lossless ? encodingDefaults?.lossless_target_bitrate : null}
            onCancel={() => {
              cancelCurrentJob(job.id).then(() => { toast("Conversion cancelled"); load(); });
            }}
          />
        );
      })}

      {/* Starting / loading next job placeholders */}
      {(() => {
        const parallelLimit = encodingDefaults?.parallel_jobs ?? 2;
        const jobCardsShowing = running.filter(j => jobProgressMap.has(j.id)).length;
        const runningSansProgress = running.filter(j => !jobProgressMap.has(j.id));
        const totalActive = jobCardsShowing + runningSansProgress.length;
        const hasPending = pendingCount > 0;

        // Show spinner for running jobs without WS progress
        const spinners = runningSansProgress.map(j => (
          <div key={`run-${j.id}`} className="job-active" style={{ display: "flex", alignItems: "center", gap: 12, padding: 20, marginBottom: 8 }}>
            <div className="spinner" style={{ width: 18, height: 18 }} />
            <span style={{ color: "var(--text-muted)", fontSize: 13 }}>{j.file_name}</span>
          </div>
        ));

        // Show "Starting..." placeholders when:
        // 1. Queue is initially starting, OR
        // 2. We have fewer active jobs than parallel limit and there are pending jobs
        const queueIsRunning = running.length > 0 || jobProgressMap.size > 0;
        const showPlaceholders = queueStarting || (queueIsRunning && hasPending && totalActive < parallelLimit);
        if (showPlaceholders) {
          const slotsNeeded = Math.max(0, parallelLimit - totalActive);
          for (let i = 0; i < slotsNeeded; i++) {
            spinners.push(
              <div key={`starting-${i}`} className="job-active" style={{ display: "flex", alignItems: "center", gap: 12, padding: 20, marginBottom: 8 }}>
                <div className="spinner" style={{ width: 18, height: 18 }} />
                <span style={{ color: "var(--text-muted)", fontSize: 13 }}>Starting...</span>
              </div>
            );
          }
        }

        return spinners;
      })()}

      {/* Tabs */}
      <div style={{ display: "flex", gap: 0, marginBottom: 16, borderBottom: "1px solid var(--border)" }}>
        <button
          onClick={() => setTab("pending")}
          style={{
            padding: "10px 20px", fontSize: 13, cursor: "pointer",
            background: "none", border: "none",
            color: tab === "pending" ? "var(--accent)" : "var(--text-muted)",
            borderBottom: tab === "pending" ? "2px solid var(--accent)" : "2px solid transparent",
          }}
        >
          Pending ({fmtNum(pendingCount)} remaining)
        </button>
        <button
          onClick={() => setTab("completed")}
          style={{
            padding: "10px 20px", fontSize: 13, cursor: "pointer",
            background: "none", border: "none",
            color: tab === "completed" ? "var(--success)" : "var(--text-muted)",
            borderBottom: tab === "completed" ? "2px solid var(--success)" : "2px solid transparent",
          }}
        >
          Completed ({fmtNum(completedCount)} &middot; saved {stats ? (() => { const gb = Math.max(0, stats.total_space_saved) / (1024**3); return gb >= 1000 ? (gb / 1024).toFixed(2) + " TB" : gb.toFixed(1) + " GB"; })() : "0 GB"})
        </button>
        {failedCount > 0 && (
          <button
            onClick={() => setTab("failed")}
            style={{
              padding: "10px 20px", fontSize: 13, cursor: "pointer",
              background: "none", border: "none",
              color: tab === "failed" ? "#e94560" : "var(--text-muted)",
              borderBottom: tab === "failed" ? "2px solid #e94560" : "2px solid transparent",
            }}
          >
            Failed ({fmtNum(failedCount)})
          </button>
        )}
      </div>

      {/* Pending tab */}
      {tab === "pending" && (
        <>
          {selectedJobIds.size > 0 && (
            <QueueControlPanel
              selectedCount={selectedJobIds.size}
              onMoveTop={() => handleBulkMove("top")}
              onMoveUp={() => handleBulkMove("up")}
              onMoveDown={() => handleBulkMove("down")}
              onMoveBottom={() => handleBulkMove("bottom")}
              onChangeVideoPreset={handleBulkVideoPreset}
              onChangeAudioPreset={handleBulkAudioPreset}
              onChangePriority={async (priority: number) => {
                await bulkUpdateJobSettings({ job_ids: selectedIds, priority });
                toast(`Priority set to ${["Normal", "High", "Highest"][priority]}`);
                load();
              }}
              onIgnore={handleBulkIgnore}
              onRemove={handleBulkRemove}
              onSelectAll={() => setSelectedJobIds(new Set(pendingIds))}
              onDeselectAll={deselectAll}
            />
          )}
          {tabJobs.length > 0 && (
            <>
              <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 8 }}>
                <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}
                  onClick={async () => { if (await confirm({ message: `Clear all ${pendingCount} pending items?`, confirmLabel: "Clear all", danger: true })) { clearPending().then(load); } }}>
                  Clear all
                </button>
              </div>
              <div style={{ background: "var(--bg-primary)", borderRadius: 6, overflow: "hidden" }}>
                {tabJobs.map((job, i) => (
                  <div
                    key={job.id}
                    draggable
                    onDragStart={() => handleDragStart(i)}
                    onDragOver={(e) => handleDragOver(e, i)}
                    onDrop={() => handleDrop(i)}
                    onDragEnd={handleDragEnd}
                    style={{
                      borderTop: dropIdx === i && dragIdx !== null && dragIdx !== i ? "2px solid var(--accent)" : "2px solid transparent",
                      opacity: dragIdx === i ? 0.4 : 1,
                      cursor: "grab",
                    }}
                  >
                    <JobListItem job={job}
                      checked={selectedJobIds.has(job.id)}
                      onCheck={(e) => handleJobClick(i, job.id, e)}
                      onCancel={(id) => { cancelJob(id).then(load); }}
                      onRemove={(id) => { removeJob(id).then(load); }}
                      onIgnore={async (id, filePath) => {
                        await ignoreFile(filePath);
                        await removeJob(id);
                        load();
                        toast("File ignored and removed from queue");
                      }}
                      encodingDefaults={encodingDefaults}
                    />
                  </div>
                ))}
              </div>
            </>
          )}
          {tabJobs.length === 0 && (
            <div style={{ textAlign: "center", padding: 40, opacity: 0.5 }}>
              {(initialLoading || tabLoading) ? (
                <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 10 }}>
                  <div className="spinner" style={{ width: 18, height: 18 }} />
                  <span>Loading queue...</span>
                </div>
              ) : "No pending jobs."}
            </div>
          )}
        </>
      )}

      {/* Completed tab */}
      {tab === "completed" && (
        <>
          {tabJobs.length > 0 && (
            <>
              <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 8 }}>
                <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}
                  onClick={() => { clearCompleted(); load(); }}>
                  Clear done
                </button>
              </div>
              <div style={{ background: "var(--bg-primary)", borderRadius: 6, overflow: "hidden" }}>
                {tabJobs.map((job) => (
                  <JobListItem key={job.id} job={job}
                    onCancel={() => {}}
                    onRemove={(id) => { removeJob(id).then(load); }}
                  />
                ))}
              </div>
            </>
          )}
          {tabJobs.length === 0 && (
            <div style={{ textAlign: "center", padding: 40, opacity: 0.5 }}>
              {(initialLoading || tabLoading) ? (
                <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 10 }}>
                  <div className="spinner" style={{ width: 18, height: 18 }} />
                  <span>Loading completed jobs...</span>
                </div>
              ) : "No completed jobs yet."}
            </div>
          )}
        </>
      )}

      {/* Failed tab */}
      {tab === "failed" && (
        <>
          {tabJobs.length > 0 && (
            <>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginBottom: 8 }}>
              <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}
                onClick={async () => {
                  for (const job of tabJobs) {
                    await retryJob(job.id);
                  }
                  load();
                  toast(`Retrying ${tabJobs.length} failed job(s)`, "success");
                  setTab("pending");
                }}>
                Retry all
              </button>
              <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px", color: "#e94560", borderColor: "#e94560" }}
                onClick={async () => {
                  const paths = tabJobs.map(j => j.file_path).filter(Boolean);
                  if (!paths.length) { toast("No file paths on these jobs", "error"); return; }
                  if (!await confirm({
                    message: `Re-request ${paths.length} file(s) from Sonarr/Radarr?\n\nThis will: blocklist the current release, delete the file from disk, and trigger a fresh search for each one.`,
                    confirmLabel: `Re-request ${paths.length}`,
                    danger: true,
                  })) return;
                  const res = await researchFilesBulk(paths, true);
                  load();
                  if (res.failed === 0) {
                    toast(`Re-requested ${res.succeeded} file(s) from Sonarr/Radarr`, "success");
                  } else {
                    toast(`${res.succeeded} re-requested, ${res.failed} failed (check logs)`, res.succeeded > 0 ? "success" : "error");
                  }
                }}>
                Re-request all (Sonarr/Radarr)
              </button>
              <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}
                onClick={async () => {
                  if (!await confirm({ message: `Clear all ${tabJobs.length} failed job(s)?`, confirmLabel: "Clear all", danger: true })) return;
                  for (const job of tabJobs) {
                    await removeJob(job.id);
                  }
                  load();
                  toast(`Cleared ${tabJobs.length} failed job(s)`);
                  setTab("pending");
                }}>
                Clear all
              </button>
            </div>
            <div style={{ background: "var(--bg-primary)", borderRadius: 6, overflow: "hidden" }}>
              {tabJobs.map((job) => (
                <JobListItem key={job.id} job={job}
                  onCancel={(id) => { cancelJob(id).then(load); }}
                  onRetry={(id) => {
                    retryJob(id).then(res => {
                      load();
                      if (res.message) toast(res.message, "success");
                    });
                  }}
                  onRemove={(id) => { removeJob(id).then(load); }}
                />
              ))}
            </div>
            </>
          )}
          {tabJobs.length === 0 && (
            <div style={{ textAlign: "center", padding: 40, opacity: 0.5 }}>
              {(initialLoading || tabLoading) ? (
                <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 10 }}>
                  <div className="spinner" style={{ width: 18, height: 18 }} />
                  <span>Loading failed jobs...</span>
                </div>
              ) : "No failed jobs."}
            </div>
          )}
        </>
      )}

    </div>
  );
}
