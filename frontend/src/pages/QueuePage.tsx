import { useState, useEffect, useMemo, useRef } from "react";
import { displayNameForPath } from "../utils/displayName";
import { useTranslation } from "react-i18next";
import { mergeHead } from "../utils/mergeHead";
import { encoderSettingsLabel, jobEncoderSettings } from "../utils/encoderLabel";
import VirtualJobList from "../components/VirtualJobList";
import { getJobs, getJobIds, getJobStats, startQueue, pauseQueue, cancelJob, cancelCurrentJob, removeJob, retryJob, clearCompleted, clearPending, ignoreFile, bulkUpdateJobSettings, bulkMoveJobs, bulkIgnoreJobs, getEncodingSettings, getTracksByPath, reorderJobs, researchFilesBulk, getNodes } from "../api";
import { fmtNum } from "../fmt";
import JobCard from "../components/JobCard";
import JobListItem from "../components/JobListItem";
import QueueControlPanel from "../components/QueueControlPanel";
import { useShiftSelect } from "../useShiftSelect";
import { useToast } from "../useToast";
import { useVisibleInterval } from "../useVisibleInterval";
import { useConfirm } from "../components/ConfirmModal";
import type { Job, JobProgress } from "../types";

interface QueuePageProps {
  jobProgressMap: Map<number, JobProgress>;
}

export default function QueuePage({ jobProgressMap }: QueuePageProps) {
  const { t } = useTranslation(["queue", "common"]);
  const [jobs, setJobs] = useState<Job[]>([]);
  // Latest rendered jobs, for the poll's post-await merge (its closure is stale).
  const jobsRef = useRef(jobs);
  jobsRef.current = jobs;
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
  const [nodes, setNodes] = useState<any[]>([]);

  // Load encoding defaults once. Nodes are polled so placeholder logic can
  // react to pauses — a paused local node shouldn't claim an encoding slot.
  useEffect(() => {
    getEncodingSettings().then(setEncodingDefaults).catch(() => {});
  }, []);
  const loadNodes = () => {
    getNodes().then(r => setNodes(r?.nodes || [])).catch(() => {});
  };
  useEffect(loadNodes, []);
  useVisibleInterval(loadNodes, 10000);

  const parseJobs = (data: any[]) =>
    (Array.isArray(data) ? data : []).map((job: any) => ({
      ...job,
      file_name: displayNameForPath(job.file_path),
      audio_tracks_to_remove: typeof job.audio_tracks_to_remove === "string"
        ? JSON.parse(job.audio_tracks_to_remove || "[]")
        : (job.audio_tracks_to_remove || []),
      subtitle_tracks_to_remove: typeof job.subtitle_tracks_to_remove === "string"
        ? JSON.parse(job.subtitle_tracks_to_remove || "[]")
        : (job.subtitle_tracks_to_remove || []),
    }));

  const loadingRef = useRef(false);
  const loadGen = useRef(0);
  const PAGE_SIZE = 100; // infinite-scroll batch for completed/failed history

  // Filename search (server-side, so it covers the FULL history, not just the
  // loaded page). Applies to every tab. Debounced into appliedSearch. v0.9.130.
  const [search, setSearch] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  // Infinite-scroll paging for the completed/failed history tabs.
  const [tabOffset, setTabOffset] = useState(0);
  const [tabHasMore, setTabHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  // Last seen server count for the history tab, so a poll can tell when new
  // completions/failures arrived and merge them in. v0.9.136.
  const tabCountRef = useRef<number | null>(null);
  const loadingMoreRef = useRef(false);
  const lastFullLoadRef = useRef(0);

  const load = async (force = false) => {
    // A poll already in flight must not block a tab switch. Tab changes pass
    // force=true; a generation counter drops any stale in-flight result so a
    // slow poll can't clobber the newer tab's data. v0.9.129.
    if (loadingRef.current && !force) return;
    loadingRef.current = true;
    const myGen = ++loadGen.current;
    const tabAtStart = tab;
    const searchAtStart = appliedSearch;
    try {
      setTabLoading(true);
      // Pending is the live queue (drag-reorderable) so it loads in full;
      // completed/failed load a page at a time and grow via infinite scroll.
      const pageLimit = tabAtStart === "pending" ? 0 : PAGE_SIZE;
      const [s, runningData, tabData] = await Promise.all([
        getJobStats(),
        getJobs("running"),
        getJobs(tabAtStart, pageLimit, 0, searchAtStart),
      ]);
      if (myGen !== loadGen.current) return; // superseded by a newer load
      setStats(s);
      const allJobs = [...parseJobs(runningData), ...parseJobs(tabData)];
      // Ensure pending jobs are available for spinner cards
      if (tabAtStart !== "pending" && runningData.length === 0 && s.pending > 0) {
        try {
          // First 10 only. This was getJobs("pending", 0, 10) — limit 0
          // (= no limit) at offset 10 — which downloaded the ENTIRE pending
          // queue on every completed/failed load while idle. v0.9.138.
          const pendingData = await getJobs("pending", 10);
          allJobs.push(...parseJobs(pendingData));
        } catch {}
      }
      if (myGen !== loadGen.current) return;
      setJobs(allJobs);
      tabCountRef.current = (s as any)[tabAtStart] ?? null;
      lastFullLoadRef.current = Date.now();
      setTabOffset(tabData.length);
      setTabHasMore(tabAtStart !== "pending" && tabData.length === PAGE_SIZE);
      setInitialLoading(false);
      setTabLoading(false);
    } finally {
      if (myGen === loadGen.current) loadingRef.current = false;
    }
  };

  // Append the next page of completed/failed history (infinite scroll).
  const loadMore = async () => {
    if (loadingMore || !tabHasMore || tab === "pending") return;
    setLoadingMore(true);
    loadingMoreRef.current = true;
    const tabAtStart = tab;
    const searchAtStart = appliedSearch;
    const offset = tabOffset;
    try {
      const more = parseJobs(await getJobs(tabAtStart, PAGE_SIZE, offset, searchAtStart));
      if (tabAtStart !== tab || searchAtStart !== appliedSearch) return; // switched mid-fetch
      setJobs(prev => {
        const have = new Set(prev.map(j => j.id));
        return [...prev, ...more.filter(j => !have.has(j.id))];
      });
      setTabOffset(offset + more.length);
      setTabHasMore(more.length === PAGE_SIZE);
    } finally {
      setLoadingMore(false);
      loadingMoreRef.current = false;
    }
  };

  // Poll refresh: keep running + stats live. Pending (the live queue) refreshes
  // in full. Completed/failed keep their loaded pages (so a 10s poll doesn't
  // reset the infinite-scroll position) but, when the tab's count changed,
  // re-fetch the newest page and splice it over the top — pre-v0.9.136 new
  // completions never appeared until a manual reload.
  const pollRefresh = async () => {
    const tabAtStart = tab;
    const searchAtStart = appliedSearch;
    const genAtStart = loadGen.current;
    if (tabAtStart === "pending") {
      // The pending list can be thousands of jobs, so the poll fetches only
      // its ordered ids. Jobs that left the queue (started, finished,
      // removed) are dropped locally; the full list is re-downloaded only
      // when ids were added or reordered, plus a 60s safety refresh for
      // per-job setting edits made elsewhere. v0.9.138/v0.9.139.
      try {
        const [s, runningData, ids] = await Promise.all([
          getJobStats(), getJobs("running"), getJobIds("pending", searchAtStart),
        ]);
        if (genAtStart !== loadGen.current) return;
        if (Date.now() - lastFullLoadRef.current > 60000) { load(); return; }
        const current = jobsRef.current;
        const idSet = new Set(ids);
        const kept = current.filter(j => j.status === "pending" && idSet.has(j.id));
        const sameOrder = kept.length === ids.length && kept.every((j, k) => j.id === ids[k]);
        if (!sameOrder) { load(); return; }
        setStats(s);
        tabCountRef.current = s.pending;
        const running = parseJobs(runningData);
        setJobs([...running, ...current.filter(j => j.status !== "running" && j.status !== "pending"), ...kept]);
      } catch {}
      return;
    }
    try {
      const [s, runningData] = await Promise.all([getJobStats(), getJobs("running")]);
      const count = (s as any)[tabAtStart] ?? null;
      const head = count !== tabCountRef.current
        ? parseJobs(await getJobs(tabAtStart, PAGE_SIZE, 0, searchAtStart))
        : null;
      // A tab/search switch (load() bumps the generation) happened mid-poll —
      // its data wins.
      if (genAtStart !== loadGen.current) return;
      setStats(s);
      const running = parseJobs(runningData);
      // A page being appended by infinite scroll: merge on the next poll
      // instead (tabCountRef stays stale, so it will re-fetch the head).
      if (!head || loadingMoreRef.current) {
        setJobs(prev => [...running, ...prev.filter(j => j.status !== "running")]);
        return;
      }
      tabCountRef.current = count;
      const current = jobsRef.current;
      const next = mergeHead(current.filter(j => j.status === tabAtStart), head);
      setJobs([...running, ...current.filter(j => j.status !== "running" && j.status !== tabAtStart), ...next.rows]);
      setTabOffset(next.rows.length);
      if (next.reset) setTabHasMore(head.length === PAGE_SIZE);
    } catch {}
  };

  // Debounce the search box.
  useEffect(() => {
    const t = setTimeout(() => setAppliedSearch(search.trim()), 300);
    return () => clearTimeout(t);
  }, [search]);

  // Load on mount, tab change, and applied-search change (reset to first page).
  useEffect(() => { load(true); }, [tab, appliedSearch]);

  // Infinite scroll: fetch the next page when the bottom sentinel scrolls into
  // view. Re-created when the paging state changes so it closes over fresh values.
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el || !tabHasMore) return;
    const obs = new IntersectionObserver((entries) => {
      if (entries[0]?.isIntersecting) loadMore();
    }, { rootMargin: "300px" });
    obs.observe(el);
    return () => obs.disconnect();
  }, [tab, appliedSearch, tabHasMore, tabOffset, loadingMore]);

  // Poll every 10 seconds normally, every 2s while waiting for jobs to start.
  // Visibility-aware so we don't burn CPU while the tab is backgrounded.
  useVisibleInterval(pollRefresh, queueStarting ? 2000 : 10000);

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
  // Memoized so its reference is stable across the frequent job_progress
  // re-renders (jobProgressMap changes every WS tick). `jobs` only changes on
  // an actual load, so tabJobs — and the row lists derived from it below —
  // don't get rebuilt on every progress tick. v0.9.88.
  const tabJobs = useMemo(() => jobs.filter((j) => j.status === tab), [jobs, tab]);
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
    bulkMoveJobs(selectedIds, position).then(() => load());
    toast(t("queue:toasts.moving", { count: selectedIds.length, position: t(`queue:positions.${position}`) }));
  };

  const handleBulkVideoPreset = async (preset: string, cq: number) => {
    // Optimistic UI update — apply immediately, then sync in background.
    // Route to libx265_* vs nvenc_* based on the configured default encoder
    // so CPU-only installs pick up the CRF slider instead of NVENC's CQ.
    const isCpu = encodingDefaults?.default_encoder === "libx265";
    const selectedSet = new Set(selectedIds);
    setJobs(prev => prev.map(j => {
      if (!selectedSet.has(j.id)) return j;
      return isCpu
        ? { ...j, libx265_preset: preset, libx265_crf: cq }
        : { ...j, nvenc_preset: preset, nvenc_cq: cq };
    }));
    toast(t("queue:toasts.videoPresetApplied", { count: selectedIds.length }), "success");
    bulkUpdateJobSettings(isCpu
      ? { job_ids: selectedIds, libx265_preset: preset, libx265_crf: cq }
      : { job_ids: selectedIds, nvenc_preset: preset, nvenc_cq: cq });
  };

  const handleBulkAudioPreset = async (codec: string, bitrate: number) => {
    const selectedSet = new Set(selectedIds);
    setJobs(prev => prev.map(j =>
      selectedSet.has(j.id) ? { ...j, audio_codec: codec, audio_bitrate: bitrate } : j
    ));
    bulkUpdateJobSettings({ job_ids: selectedIds, audio_codec: codec, audio_bitrate: bitrate });
    toast(t("queue:toasts.audioPresetApplied", { count: selectedIds.length }), "success");
  };

  const handleBulkIgnore = async () => {
    if (!await confirm({ message: t("queue:confirm.ignoreSelected", { count: selectedIds.length }), confirmLabel: t("common:actions.ignore"), danger: true })) return;
    const selectedSet = new Set(selectedIds);
    setJobs(prev => prev.filter(j => !selectedSet.has(j.id)));
    bulkIgnoreJobs(selectedIds as number[]);
    deselectAll();
    load();
    toast(t("queue:toasts.ignored", { count: selectedIds.length }));
  };

  const handleBulkRemove = async () => {
    if (!await confirm({ message: t("queue:confirm.removeSelected", { count: selectedIds.length }), confirmLabel: t("common:actions.remove"), danger: true })) return;
    const selectedSet = new Set(selectedIds);
    setJobs(prev => prev.filter(j => !selectedSet.has(j.id)));
    for (const id of selectedIds) {
      removeJob(id as number).catch(() => {});
    }
    deselectAll();
    load();
    toast(t("queue:toasts.removed", { count: selectedIds.length }));
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
  // Lossless flag cache, also keyed by file_path. Sourced from the
  // backend's pre-computed has_lossless_audio (handles DTS-HD profile
  // variants that the old client-side detection was missing).
  // v0.3.106+.
  const [losslessCache, setLosslessCache] = useState<Map<string, boolean>>(new Map());

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
          setLosslessCache(prev => {
            const next = new Map(prev);
            next.set(job.file_path, !!data.has_lossless_audio);
            return next;
          });
        }).catch(() => {});
      }
    }
  }, [running.map(j => j.file_path).join(",")]);

  // Row renderers for the virtualized lists (v0.9.138). Only rows in view are
  // mounted, so a job_progress WebSocket tick (which re-renders this page via
  // jobProgressMap) or a selection/drag change costs ~a screenful of
  // JobListItem memo checks instead of one per job. The v0.9.88/v0.9.90
  // useMemo'd full row arrays this replaces still froze Firefox at ~5K jobs.
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set());
  const toggleExpanded = (id: number) => setExpandedIds(prev => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const renderPendingRow = (i: number) => {
    const job = tabJobs[i];
    return (
      <div
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
          onCancel={(id) => { cancelJob(id).then(() => load()); }}
          onRemove={(id) => { removeJob(id).then(() => load()); }}
          onIgnore={async (id, filePath) => {
            await ignoreFile(filePath);
            await removeJob(id);
            load();
            toast(t("queue:toasts.fileIgnored"));
          }}
          encodingDefaults={encodingDefaults}
        />
      </div>
    );
  };

  const renderCompletedRow = (i: number) => (
    <JobListItem job={tabJobs[i]}
      expanded={expandedIds.has(tabJobs[i].id)}
      onToggleExpand={toggleExpanded}
      onCancel={() => {}}
      onRemove={(id) => { removeJob(id).then(() => load()); }}
    />
  );

  const renderFailedRow = (i: number) => (
    <JobListItem job={tabJobs[i]}
      expanded={expandedIds.has(tabJobs[i].id)}
      onToggleExpand={toggleExpanded}
      onCancel={(id) => { cancelJob(id).then(() => load()); }}
      onRetry={(id) => {
        retryJob(id).then(res => {
          load();
          if (res.message) toast(res.message, "success");
        });
      }}
      onRemove={(id) => { removeJob(id).then(() => load()); }}
    />
  );
  const jobKey = (i: number) => tabJobs[i].id;

  if (initialLoading) {
    return (
      <div>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
          <h2 style={{ color: "var(--text-primary)", fontSize: 20 }}>{t("queue:title")}</h2>
        </div>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: 60 }}>
          <div className="spinner" />
          <div style={{ marginTop: 12, fontSize: 13, opacity: 0.5 }}>{t("queue:loadingQueue")}</div>
        </div>
      </div>
    );
  }

  // Stream-aware pause banner. v0.4.6+. When a Plex/Jellyfin/Emby user
  // is streaming and we've SIGSTOPped active ffmpeg jobs, surface that
  // explicitly so the user understands why the progress bars are frozen.
  const streamPause = stats?.stream_pause;
  const streamPauseActive = !!streamPause?.active;
  const streamPauseServers: string[] = streamPause?.servers || [];
  const streamPauseFrozen: number = streamPause?.frozen_jobs || 0;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
        <h2 style={{ color: "var(--text-primary)", fontSize: 20 }}>{t("queue:title")}</h2>
        {hasActiveJobs ? (
          <button className="btn btn-secondary" onClick={() => { pauseQueue(); toast(t("queue:toasts.queuePaused")); }}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg> {t("common:actions.pause")}
          </button>
        ) : (
          <button className="btn btn-primary" onClick={() => { setQueueStarting(true); startQueue().then(() => { load(); toast(t("queue:toasts.queueStarted"), "success"); }); }}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21"/></svg> {t("common:actions.start")}
          </button>
        )}
      </div>

      {streamPauseActive && (
        <div style={{
          background: "var(--accent-soft, rgba(99, 102, 241, 0.12))",
          border: "1px solid var(--accent, #6366f1)",
          borderRadius: 6,
          padding: "10px 14px",
          marginBottom: 16,
          display: "flex",
          alignItems: "center",
          gap: 10,
          color: "var(--text-primary)",
          fontSize: 13,
        }}>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" style={{ flexShrink: 0 }}>
            <rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>
          </svg>
          <div>
            <strong>{t("queue:streamPause.title")}</strong>
            {streamPauseServers.length > 0 && (
              <>{" "}{t("queue:streamPause.activeStreams", { count: streamPauseServers.length, servers: streamPauseServers.join(" + ") })}</>
            )}
            {streamPauseFrozen > 0 && (
              <span style={{ color: "var(--text-muted)" }}>
                {" "}{t("queue:streamPause.frozen", { count: streamPauseFrozen })}
              </span>
            )}
          </div>
        </div>
      )}

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
        // "Lossless → EAC3" badge: only show when a track that's
        // ACTUALLY GOING TO BE KEPT is lossless. Pre-v0.3.124 the
        // backend's has_lossless_audio was a file-level flag, so a
        // file with a TrueHD/DTS-HD MA secondary that was being
        // removed still triggered the badge — implying a transcode
        // that would never happen. The per-track `is_lossless` flag
        // (also v0.3.124) lets the UI intersect against the job's
        // removal list. Falls back to the file-level flag when the
        // per-track field isn't present (older /tracks-by-path
        // responses that haven't been re-fetched yet).
        const hasLossless = tracks.length > 0
          ? tracks.some((t: any) => t.is_lossless && !removeIndices.has(t.stream_index))
          : !!losslessCache.get(job.file_path);
        return (
          <JobCard key={job.id} progress={progress}
            jobIndex={runIndex}
            fileSize={job.original_size}
            // The encoder the node actually runs (it may have swapped the
            // job's encoder for one it has — v0.9.134), else the job's own.
            encoderLabel={encoderSettingsLabel(
              progress.encoder || job.encoder || encodingDefaults?.default_encoder,
              jobEncoderSettings(job, encodingDefaults),
            )}
            jobType={job.job_type}
            audioCodec={job.audio_codec || encodingDefaults?.audio_codec || "copy"}
            audioBitrate={job.audio_bitrate ?? encodingDefaults?.audio_bitrate ?? 128}
            audioTracksToRemove={job.audio_tracks_to_remove}
            subtitleTracksToRemove={job.subtitle_tracks_to_remove}
            removedTrackLangs={removedLangs}
            losslessCodec={hasLossless && encodingDefaults?.auto_convert_lossless ? encodingDefaults?.lossless_target_codec : null}
            losslessBitrate={hasLossless && encodingDefaults?.auto_convert_lossless ? encodingDefaults?.lossless_target_bitrate : null}
            onCancel={() => {
              cancelCurrentJob(job.id).then(() => { toast(t("queue:toasts.conversionCancelled")); load(); });
            }}
          />
        );
      })}

      {/* Starting / loading next job placeholders */}
      {(() => {
        // Effective capacity = sum of max_jobs across nodes that can actually
        // pick up work right now. Previously this used the global
        // `parallel_jobs` setting, which ignored paused / offline nodes —
        // producing phantom "Starting…" cards when e.g. the local node was
        // paused but a remote worker was busy.
        const availableNodes = nodes.filter(n =>
          !n.paused && (n.status === "online" || n.status === "working")
        );
        const effectiveCapacity = availableNodes.length > 0
          ? availableNodes.reduce((s: number, n: any) => s + (n.max_jobs || 1), 0)
          : (encodingDefaults?.parallel_jobs ?? 2); // no nodes API data yet — fall back to legacy global
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
        // 2. We have fewer active jobs than available capacity and there
        //    are pending jobs that could actually be dispatched.
        const queueIsRunning = running.length > 0 || jobProgressMap.size > 0;
        const showPlaceholders = queueStarting || (queueIsRunning && hasPending && totalActive < effectiveCapacity);
        if (showPlaceholders) {
          const slotsNeeded = Math.max(0, effectiveCapacity - totalActive);
          for (let i = 0; i < slotsNeeded; i++) {
            spinners.push(
              <div key={`starting-${i}`} className="job-active" style={{ display: "flex", alignItems: "center", gap: 12, padding: 20, marginBottom: 8 }}>
                <div className="spinner" style={{ width: 18, height: 18 }} />
                <span style={{ color: "var(--text-muted)", fontSize: 13 }}>{t("queue:starting")}</span>
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
          {t("queue:tabs.pendingRemaining", { count: pendingCount, formatted: fmtNum(pendingCount) })}
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
          {t("queue:tabs.completedSaved", { formatted: fmtNum(completedCount), saved: stats ? (() => { const gb = Math.max(0, stats.total_space_saved) / (1024**3); return gb >= 1000 ? (gb / 1024).toFixed(2) + " TB" : gb.toFixed(1) + " GB"; })() : "0 GB" })}
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
            {t("queue:tabs.failedCount", { formatted: fmtNum(failedCount) })}
          </button>
        )}
      </div>


      {/* Toolbar: filename search (server-side, covers the full history on
          every tab; same pill style as the Scanner's) left, the tab's bulk
          actions right. */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", marginBottom: 12 }}>
        <div style={{ position: "relative", width: 300, maxWidth: "100%" }}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
            style={{ position: "absolute", left: 10, top: "50%", transform: "translateY(-50%)", pointerEvents: "none" }}>
            <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
          </svg>
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("queue:search.placeholder", { tab: t(`common:status.${tab}`).toLowerCase() })}
            style={{ width: "100%", padding: "6px 28px 6px 30px", fontSize: 12, lineHeight: "1.4", background: "var(--bg-card)", color: "var(--text-secondary)", border: "1px solid var(--border)", borderRadius: 16, outline: "none", boxSizing: "border-box" }}
          />
          {search && (
            <button
              onClick={() => setSearch("")}
              aria-label={t("common:actions.clearSearch")}
              style={{ position: "absolute", right: 10, top: "50%", transform: "translateY(-50%)", background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer", fontSize: 14, lineHeight: 1 }}
            >&times;</button>
          )}
        </div>
        <div style={{ display: "flex", gap: 8, marginLeft: "auto" }}>
          {tab === "pending" && tabJobs.length > 0 && (<>
                <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}
                  onClick={async () => { if (await confirm({ message: t("queue:confirm.clearPending", { count: pendingCount }), confirmLabel: t("common:actions.clearAll"), danger: true })) { clearPending().then(() => load()); } }}>
                  {t("common:actions.clearAll")}
                </button>
          </>)}
          {tab === "completed" && tabJobs.length > 0 && (<>
                <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}
                  onClick={() => { clearCompleted(); load(); }}>
                  {t("queue:actions.clearDone")}
                </button>
          </>)}
          {tab === "failed" && tabJobs.length > 0 && (<>
              <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}
                onClick={async () => {
                  for (const job of tabJobs) {
                    await retryJob(job.id);
                  }
                  load();
                  toast(t("queue:toasts.retrying", { count: tabJobs.length }), "success");
                  setTab("pending");
                }}>
                {t("queue:actions.retryAll")}
              </button>
              <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px", color: "#e94560", borderColor: "#e94560" }}
                onClick={async () => {
                  const paths = tabJobs.map(j => j.file_path).filter(Boolean);
                  if (!paths.length) { toast(t("queue:toasts.noFilePaths"), "error"); return; }
                  if (!await confirm({
                    message: t("queue:confirm.rerequest", { count: paths.length }),
                    confirmLabel: t("queue:confirm.rerequestLabel", { count: paths.length }),
                    danger: true,
                  })) return;
                  const res = await researchFilesBulk(paths, true);
                  load();
                  if (res.failed === 0) {
                    toast(t("queue:toasts.rerequested", { count: res.succeeded }), "success");
                  } else {
                    toast(t("queue:toasts.rerequestPartial", { succeeded: res.succeeded, failed: res.failed }), res.succeeded > 0 ? "success" : "error");
                  }
                }}>
                {t("queue:actions.rerequestAll")}
              </button>
              <button className="btn btn-secondary" style={{ fontSize: 11, padding: "4px 10px" }}
                onClick={async () => {
                  if (!await confirm({ message: t("queue:confirm.clearFailed", { count: tabJobs.length }), confirmLabel: t("common:actions.clearAll"), danger: true })) return;
                  for (const job of tabJobs) {
                    await removeJob(job.id);
                  }
                  load();
                  toast(t("queue:toasts.clearedFailed", { count: tabJobs.length }));
                  setTab("pending");
                }}>
                {t("common:actions.clearAll")}
              </button>
          </>)}
        </div>
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
              defaultEncoder={encodingDefaults?.default_encoder}
              onChangePriority={async (priority: number) => {
                await bulkUpdateJobSettings({ job_ids: selectedIds, priority });
                toast(t("queue:toasts.prioritySet", { priority: t(`queue:priority.${["normal", "high", "highest"][priority]}`) }));
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
              <div style={{ background: "var(--bg-primary)", borderRadius: 6, overflow: "hidden" }}>
                <VirtualJobList count={tabJobs.length} getKey={jobKey} renderRow={renderPendingRow} />
              </div>
            </>
          )}
          {tabJobs.length === 0 && (
            <div style={{ textAlign: "center", padding: 40, opacity: 0.5 }}>
              {(initialLoading || tabLoading) ? (
                <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 10 }}>
                  <div className="spinner" style={{ width: 18, height: 18 }} />
                  <span>{t("queue:loadingQueue")}</span>
                </div>
              ) : (appliedSearch ? t("queue:empty.pendingSearch", { search: appliedSearch }) : t("queue:empty.pending"))}
            </div>
          )}
        </>
      )}

      {/* Completed tab */}
      {tab === "completed" && (
        <>
          {tabJobs.length > 0 && (
            <>
              <div style={{ background: "var(--bg-primary)", borderRadius: 6, overflow: "hidden" }}>
                <VirtualJobList count={tabJobs.length} getKey={jobKey} renderRow={renderCompletedRow} />
              </div>
              {tabHasMore && <div ref={sentinelRef} style={{ height: 1 }} />}
              {loadingMore && (
                <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 8, padding: 16, opacity: 0.5 }}>
                  <div className="spinner" style={{ width: 16, height: 16 }} /> <span>{t("common:status.loadingMore")}</span>
                </div>
              )}
            </>
          )}
          {tabJobs.length === 0 && (
            <div style={{ textAlign: "center", padding: 40, opacity: 0.5 }}>
              {(initialLoading || tabLoading) ? (
                <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 10 }}>
                  <div className="spinner" style={{ width: 18, height: 18 }} />
                  <span>{t("queue:empty.loadingCompleted")}</span>
                </div>
              ) : (appliedSearch ? t("queue:empty.completedSearch", { search: appliedSearch }) : t("queue:empty.completed"))}
            </div>
          )}
        </>
      )}

      {/* Failed tab */}
      {tab === "failed" && (
        <>
          {tabJobs.length > 0 && (
            <>
            <div style={{ background: "var(--bg-primary)", borderRadius: 6, overflow: "hidden" }}>
              <VirtualJobList count={tabJobs.length} getKey={jobKey} renderRow={renderFailedRow} />
            </div>
            {tabHasMore && <div ref={sentinelRef} style={{ height: 1 }} />}
            {loadingMore && (
              <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 8, padding: 16, opacity: 0.5 }}>
                <div className="spinner" style={{ width: 16, height: 16 }} /> <span>{t("common:status.loadingMore")}</span>
              </div>
            )}
            </>
          )}
          {tabJobs.length === 0 && (
            <div style={{ textAlign: "center", padding: 40, opacity: 0.5 }}>
              {(initialLoading || tabLoading) ? (
                <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 10 }}>
                  <div className="spinner" style={{ width: 18, height: 18 }} />
                  <span>{t("queue:empty.loadingFailed")}</span>
                </div>
              ) : (appliedSearch ? t("queue:empty.failedSearch", { search: appliedSearch }) : t("queue:empty.failed"))}
            </div>
          )}
        </>
      )}

    </div>
  );
}
