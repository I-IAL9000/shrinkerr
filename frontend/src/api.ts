import { useEffect, useRef } from "react";
import type { WSMessage } from "./types";

const API_BASE = "/api";

// API key stored in sessionStorage for the current browser session
export function getStoredApiKey(): string {
  return sessionStorage.getItem("squeezarr_api_key") || "";
}
export function setStoredApiKey(key: string) {
  sessionStorage.setItem("squeezarr_api_key", key);
}

async function apiFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const apiKey = getStoredApiKey();
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (apiKey) headers["X-Api-Key"] = apiKey;
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: { ...headers, ...(options?.headers || {}) },
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
  return res.json();
}

export const checkAuth = () =>
  fetch("/api/auth/check", {
    headers: getStoredApiKey() ? { "X-Api-Key": getStoredApiKey() } : {},
  }).then(r => r.json()) as Promise<{ auth_required: boolean; authenticated: boolean }>;

// Scan
export const startScan = (paths: string[]) =>
  apiFetch("/scan/start", { method: "POST", body: JSON.stringify({ paths }) });
export const cancelScan = () => apiFetch("/scan/cancel", { method: "POST" });
export const refreshMetadata = () => apiFetch("/scan/refresh-metadata", { method: "POST" });
export const cancelMetadata = () => apiFetch("/scan/cancel-metadata", { method: "POST" });
export const getScanStatus = () => apiFetch<{ scanning: boolean }>("/scan/status");
export const getNewFileCount = () => apiFetch<{ count: number }>("/scan/new-count");
export const clearNewFileCount = () => apiFetch("/scan/clear-new", { method: "POST" });
export const getScanResults = () => apiFetch<any[]>("/scan/results");
export const getScanStats = () => apiFetch<any>("/scan/scan-stats");
export const getScanTree = (filter: string = "all") =>
  apiFetch<{ folders: { path: string; file_count: number; total_size: number; newest_mtime: number }[] }>(`/scan/tree?filter=${encodeURIComponent(filter)}`);
export const getScanFiles = (folder: string, filter: string = "all") =>
  apiFetch<any[]>(`/scan/files?folder=${encodeURIComponent(folder)}&filter=${encodeURIComponent(filter)}`);
export const getScanResultsVersion = () => apiFetch<{ count: number; max_id: number }>("/scan/results-version");
export const removeScanResult = (id: number) =>
  apiFetch(`/scan/results/${id}`, { method: "DELETE" });
export const updateAudioTracks = (id: number, audioTracksJson: string) =>
  apiFetch(`/scan/results/${id}/tracks`, { method: "PUT", body: JSON.stringify({ audio_tracks_json: audioTracksJson }) });
export const updateSubtitleTracks = (id: number, subtitleTracksJson: string) =>
  apiFetch(`/scan/results/${id}/subtitle-tracks`, { method: "PUT", body: JSON.stringify({ subtitle_tracks_json: subtitleTracksJson }) });
export const getTracksByPath = (filePath: string) =>
  apiFetch<{ audio_tracks: any[]; subtitle_tracks: any[] }>(`/scan/tracks-by-path?file_path=${encodeURIComponent(filePath)}`);
export const rescanFolder = (paths: string[]) =>
  apiFetch("/scan/rescan-folder", { method: "POST", body: JSON.stringify({ paths }) });
export const deleteFileFromDisk = (filePath: string) =>
  apiFetch<{ status: string; file_deleted: boolean }>("/scan/delete-file", { method: "POST", body: JSON.stringify({ file_path: filePath }) });
export const importSettings = (data: any) =>
  apiFetch<any>("/settings/import", { method: "POST", body: JSON.stringify(data) });
export const getBackups = () =>
  apiFetch<{ backups: any[]; total_size: number; total_count: number }>("/settings/backups");
export const deleteBackups = (paths?: string[], olderThanDays?: number) =>
  apiFetch<{ deleted: number; freed: number }>("/settings/backups/delete", {
    method: "POST", body: JSON.stringify({ paths: paths || [], older_than_days: olderThanDays }),
  });

// Jobs
export const addJob = (job: {
  file_path: string;
  job_type: string;
  encoder?: string;
  audio_tracks_to_remove?: number[];
}) => {
  const params = new URLSearchParams({
    file_path: job.file_path,
    job_type: job.job_type,
  });
  if (job.encoder) params.set("encoder", job.encoder);
  if (job.audio_tracks_to_remove) {
    job.audio_tracks_to_remove.forEach((i) =>
      params.append("audio_tracks_to_remove", String(i))
    );
  }
  return apiFetch(`/jobs/add?${params}`, { method: "POST" });
};
export const addBulkJobs = (jobs: any[]) =>
  apiFetch("/jobs/add-bulk", { method: "POST", body: JSON.stringify({ jobs }) });
export const addJobsFromScan = (filePaths: string[], priority: number = 0, overrideRules: boolean = false, extra?: Record<string, any>) =>
  apiFetch<{ job_ids: number[]; added: number }>("/jobs/add-from-scan", { method: "POST", body: JSON.stringify({ file_paths: filePaths, priority, override_rules: overrideRules, ...extra }) });
export const addAllFromScan = (filter: string, priority: number = 0, overrideRules: boolean = false) =>
  apiFetch<{ job_ids: number[]; added: number }>("/jobs/add-from-scan", { method: "POST", body: JSON.stringify({ select_all: true, filter, priority, override_rules: overrideRules }) });
export const getJobs = (status?: string, limit?: number, offset?: number) => {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (limit) params.set("limit", String(limit));
  if (offset) params.set("offset", String(offset));
  const qs = params.toString();
  return apiFetch<any[]>(`/jobs/${qs ? `?${qs}` : ""}`);
};
export const getJobStats = () => apiFetch<any>("/jobs/stats");
export const getStatsSummary = () => apiFetch<any>("/stats/summary");
export const getStatsTimeline = (days: number = 30) => apiFetch<any>(`/stats/timeline?days=${days}`);
export const getDashboardData = () => apiFetch<any>("/stats/dashboard");
export const testNotifications = () => apiFetch<any>("/stats/notifications/test", { method: "POST" });
export const startTestEncode = (filePath: string, encoder?: string, cq?: number, preset?: string) =>
  apiFetch<any>("/jobs/test-encode", { method: "POST", body: JSON.stringify({ file_path: filePath, encoder, cq, preset }) });
export const getVmafStatus = () =>
  apiFetch<{ vmaf_available: boolean }>("/jobs/vmaf-status");
export const getSystemMetrics = () =>
  apiFetch<any>("/stats/system");
export const resolvePosterMetadata = (paths: string[]) =>
  apiFetch<Record<string, { title: string; year: string | null; poster_url: string | null; source: string }>>(
    "/posters/resolve", { method: "POST", body: JSON.stringify({ paths }) }
  );
export const startPosterPrefetch = () =>
  apiFetch<{ status: string }>("/posters/prefetch", { method: "POST" });
export const getPosterPrefetchStatus = () =>
  apiFetch<{ status: string; total: number; resolved: number; cached: number }>("/posters/prefetch-status");
export const getRecentConversions = (limit: number = 20) =>
  apiFetch<any[]>(`/jobs/recent-conversions?limit=${limit}`);
export const undoConversion = (jobId: number) =>
  apiFetch<any>(`/jobs/${jobId}/undo`, { method: "POST" });
export const getJobLog = (jobId: number) =>
  apiFetch<any>(`/jobs/${jobId}/log`);
export const dismissSetup = () => apiFetch<any>("/stats/setup/dismiss", { method: "POST" });

// Estimation & Export
export const estimateJobs = (filePaths: string[], overrideRules: boolean = false, overrides?: Record<string, any>) =>
  apiFetch<any>("/jobs/estimate", { method: "POST", body: JSON.stringify({ file_paths: filePaths, override_rules: overrideRules, ...overrides }) });
export const estimateJobsWithPriority = (filePaths: string[], priority: number) =>
  apiFetch<any>("/jobs/estimate", { method: "POST", body: JSON.stringify({ file_paths: filePaths, priority }) });
export const getVersion = () => apiFetch<{ current: string; latest: string | null; update_available: boolean }>("/stats/version");
export const startQueue = () => apiFetch("/jobs/start", { method: "POST" });
export const pauseQueue = () => apiFetch("/jobs/pause", { method: "POST" });
export const resumeQueue = () => apiFetch("/jobs/resume", { method: "POST" });
export const cancelJob = (id: number) =>
  apiFetch(`/jobs/${id}/cancel`, { method: "POST" });
export const cancelCurrentJob = (jobId?: number) =>
  apiFetch(`/jobs/cancel-current${jobId ? `?job_id=${jobId}` : ""}`, { method: "POST" });
export const removeJob = (id: number) =>
  apiFetch(`/jobs/${id}`, { method: "DELETE" });
export const retryJob = (id: number) =>
  apiFetch(`/jobs/${id}/retry`, { method: "POST" });
export const reorderJobs = (jobIds: number[]) =>
  apiFetch("/jobs/reorder", { method: "POST", body: JSON.stringify({ job_ids: jobIds }) });
export const clearCompleted = () =>
  apiFetch("/jobs/clear-completed", { method: "POST" });
export const clearPending = () =>
  apiFetch("/jobs/clear-pending", { method: "POST" });

export const bulkUpdateJobSettings = (data: {
  job_ids: number[];
  nvenc_preset?: string;
  nvenc_cq?: number;
  audio_codec?: string;
  audio_bitrate?: number;
  priority?: number;
}) => apiFetch("/jobs/bulk-update-settings", { method: "POST", body: JSON.stringify(data) });

export const bulkMoveJobs = (job_ids: number[], position: "top" | "bottom" | "up" | "down") =>
  apiFetch("/jobs/bulk-move", { method: "POST", body: JSON.stringify({ job_ids, position }) });

export const bulkIgnoreJobs = (job_ids: number[]) =>
  apiFetch("/jobs/bulk-ignore", { method: "POST", body: JSON.stringify({ job_ids }) });

// Schedule
export const setSchedule = (startTime: string) =>
  apiFetch("/schedule/set", {
    method: "POST",
    body: JSON.stringify({ start_time: startTime }),
  });
export const cancelSchedule = () =>
  apiFetch("/schedule/cancel", { method: "DELETE" });
export const getSchedule = () => apiFetch<any>("/schedule/");
export const setRunHours = (data: { enabled: boolean; hours?: number[]; start?: number; end?: number }) =>
  apiFetch("/schedule/run-hours", { method: "POST", body: JSON.stringify(data) });

// Ignored files
export const getIgnoredFiles = () => apiFetch<string[]>("/settings/ignored");
export const unignoreFile = (filePath: string) =>
  apiFetch(`/settings/ignored/${encodeURIComponent(filePath)}`, { method: "DELETE" });
export const clearIgnored = () =>
  apiFetch("/settings/ignored", { method: "DELETE" });
export const ignoreFile = (filePath: string) =>
  apiFetch("/settings/ignored", { method: "POST", body: JSON.stringify({ file_path: filePath }) });


// Encoding rules
export const getEncodingRules = () => apiFetch<any[]>("/rules/");
export const createEncodingRule = (rule: any) =>
  apiFetch("/rules/", { method: "POST", body: JSON.stringify(rule) });
export const updateEncodingRule = (id: number, data: any) =>
  apiFetch(`/rules/${id}`, { method: "PUT", body: JSON.stringify(data) });
export const deleteEncodingRule = (id: number) =>
  apiFetch(`/rules/${id}`, { method: "DELETE" });
export const reorderEncodingRules = (ruleIds: number[]) =>
  apiFetch("/rules/reorder", { method: "PUT", body: JSON.stringify({ rule_ids: ruleIds }) });
export const syncPlexRuleMetadata = () =>
  apiFetch<any>("/rules/sync-plex", { method: "POST" });
export const getPlexOptions = () =>
  apiFetch<{ labels: string[]; collections: string[]; genres: string[]; libraries: any[] }>("/rules/plex-options");

// Settings
export const getMediaDirs = () => apiFetch<{ dirs: any[] }>("/settings/dirs");
export const addMediaDir = (path: string, label: string) =>
  apiFetch("/settings/dirs", {
    method: "POST",
    body: JSON.stringify({ path, label }),
  });
export const removeMediaDir = (id: number) =>
  apiFetch(`/settings/dirs/${id}`, { method: "DELETE" });
export const browseDirectory = (path: string = "/") =>
  apiFetch<{ path: string; parent: string | null; dirs: { name: string; path: string }[]; error?: string }>(
    `/settings/browse?path=${encodeURIComponent(path)}`
  );
export const getEncodingSettings = () => apiFetch<any>("/settings/encoding");
export const updateEncodingSettings = (settings: any) =>
  apiFetch("/settings/encoding", {
    method: "PUT",
    body: JSON.stringify(settings),
  });

// API key testing
export const testApiKey = (service: "tmdb" | "tvdb" | "plex" | "sonarr" | "radarr") =>
  apiFetch<{ success: boolean; error: string | null; version?: string }>("/settings/test-api", {
    method: "POST", body: JSON.stringify({ service })
  });

// WebSocket hook
export function useWebSocket(onMessage: (msg: WSMessage) => void) {
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const apiKey = getStoredApiKey();
    const wsUrl = `${protocol}//${window.location.host}/ws${apiKey ? `?api_key=${encodeURIComponent(apiKey)}` : ""}`;
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data) as WSMessage;
        onMessage(msg);
      } catch {}
    };

    ws.onclose = () => {
      // Reconnect after 3 seconds
      setTimeout(() => {
        if (wsRef.current?.readyState === WebSocket.CLOSED) {
          const newWs = new WebSocket(wsUrl);
          newWs.onmessage = ws.onmessage;
          newWs.onclose = ws.onclose;
          wsRef.current = newWs;
        }
      }, 3000);
    };

    return () => ws.close();
  }, [onMessage]);
}
