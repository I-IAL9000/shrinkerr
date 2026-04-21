import { useEffect, useRef } from "react";
import type { WSMessage } from "./types";

const API_BASE = "/api";

// API key stored in sessionStorage for the current browser session.
// Reads the legacy `squeezarr_api_key` key as a fallback so users upgrading
// from the old app name don't have to log in again when they reload. New
// writes always go to the canonical `shrinkerr_api_key` key.
export function getStoredApiKey(): string {
  return (
    sessionStorage.getItem("shrinkerr_api_key") ||
    sessionStorage.getItem("squeezarr_api_key") ||
    ""
  );
}
export function setStoredApiKey(key: string) {
  sessionStorage.setItem("shrinkerr_api_key", key);
  // Remove the legacy key if present so we don't have two copies drifting.
  sessionStorage.removeItem("squeezarr_api_key");
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
  }).then(r => r.json()) as Promise<{ auth_required: boolean; authenticated: boolean; method: string | null }>;

export const login = (username: string, password: string) =>
  fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  }).then(r => {
    if (!r.ok) throw new Error("Login failed");
    return r.json();
  });

export const logout = () =>
  fetch("/api/auth/logout", { method: "POST" }).then(r => r.json());

// Scan
export const startScan = (paths: string[]) =>
  apiFetch("/scan/start", { method: "POST", body: JSON.stringify({ paths }) });
export const cancelScan = () => apiFetch("/scan/cancel", { method: "POST" });
export const refreshMetadata = () => apiFetch("/scan/refresh-metadata", { method: "POST" });
export const cancelMetadata = () => apiFetch("/scan/cancel-metadata", { method: "POST" });
export const getScanStatus = () => apiFetch<{ scanning: boolean }>("/scan/status");
export const getNewFileCount = () => apiFetch<{ count: number }>("/scan/new-count");
export const getFailedJobCount = () => apiFetch<{ count: number }>("/jobs/failed-count");
export const clearNewFileCount = () => apiFetch("/scan/clear-new", { method: "POST" });
export const getScanResults = () => apiFetch<any[]>("/scan/results");
export const getScanStats = () => apiFetch<any>("/scan/scan-stats");
export const getScanTree = (filter: string = "all", signal?: AbortSignal) =>
  apiFetch<{ folders: { path: string; file_count: number; total_size: number; newest_mtime: number }[] }>(`/scan/tree?filter=${encodeURIComponent(filter)}`, { signal });
export const getScanFiles = (folder: string, filter: string = "all", signal?: AbortSignal) =>
  apiFetch<any[]>(`/scan/files?folder=${encodeURIComponent(folder)}&filter=${encodeURIComponent(filter)}`, { signal });
export const getFilesByTitle = (prefix: string, filter: string = "all", signal?: AbortSignal) =>
  apiFetch<any[]>(`/scan/files-by-title?prefix=${encodeURIComponent(prefix)}&filter=${encodeURIComponent(filter)}`, { signal });
export const getScanFilesByPaths = (filePaths: string[], filter: string = "all", signal?: AbortSignal) =>
  apiFetch<any[]>("/scan/files-by-paths", {
    method: "POST",
    body: JSON.stringify({ file_paths: filePaths, filter }),
    signal,
  });
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
export interface FileEvent {
  id: number;
  file_path: string;
  event_type: string;
  occurred_at: string;
  summary: string;
  details: any;
}
export const getFileHistory = (path: string, limit = 100) =>
  apiFetch<{ events: FileEvent[] }>(`/files/history?path=${encodeURIComponent(path)}&limit=${limit}`);
export const getActivity = (params: { event_type?: string; search?: string; since?: string; until?: string; limit?: number; offset?: number } = {}) => {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v != null && v !== "") qs.set(k, String(v));
  }
  return apiFetch<{ total: number; limit: number; offset: number; events: FileEvent[] }>(`/activity?${qs}`);
};
export interface SearchProperty {
  label: string;
  group: string;
  type: "string" | "number" | "bool" | "enum";
  ops: string[];
  examples?: any[];
  options?: any[];
  option_labels?: Record<string, string>;
}
export interface SearchPredicate {
  property: string;
  op: string;
  value?: any;
  value2?: any;
}
export const getSearchProperties = () =>
  apiFetch<Record<string, SearchProperty>>("/scan/search/properties");
export const advancedSearch = (predicates: SearchPredicate[], matchMode: "all" | "any" = "all", limit = 5000) =>
  apiFetch<{ total: number; file_paths: string[]; limit: number }>("/scan/search", {
    method: "POST",
    body: JSON.stringify({ predicates, match_mode: matchMode, limit }),
  });

export const queueHealthChecks = (filePaths: string[], mode: "quick" | "thorough" = "quick", filter?: string, selectAll?: boolean) =>
  apiFetch<{ added: number; job_ids: number[]; mode: string }>("/jobs/health-check", {
    method: "POST",
    body: JSON.stringify({ file_paths: filePaths, mode, filter: filter || "all", select_all: !!selectAll }),
  });
export const resetHealthStatus = (opts: { file_paths?: string[]; reset_all_corrupt?: boolean; unignore?: boolean } = {}) =>
  apiFetch<{ reset: number; unignored: number; targeted?: number }>("/jobs/health-check/reset", {
    method: "POST",
    body: JSON.stringify({
      file_paths: opts.file_paths || [],
      reset_all_corrupt: opts.reset_all_corrupt || false,
      unignore: opts.unignore !== false,
    }),
  });
export const clearPendingHealthChecks = () =>
  apiFetch<{ deleted: number }>("/jobs/health-check/clear-pending", { method: "POST" });
// Worker nodes
import type { WorkerNode } from "./types";
export const getNodes = () => apiFetch<{ nodes: WorkerNode[] }>("/nodes");
export interface NodeMetrics {
  gpu: {
    gpu_util: number;
    memory_used_mb: number;
    memory_total_mb: number;
    memory_percent: number;
    temperature_c: number;
    power_draw_w: number;
    power_limit_w: number;
    name: string;
    encoder_util: number | null;
    decoder_util: number | null;
  } | null;
  cpu: {
    cpu_percent: number;
    cpu_count: number;
    load_avg: number[];
    cpu_freq_mhz: number | null;
  };
  memory: {
    ram_total_gb: number;
    ram_used_gb: number;
    ram_percent: number;
    swap_used_gb: number;
    swap_percent: number;
  };
  disk_io: { read_mbps: number; write_mbps: number };
  network: { download_mbps: number; upload_mbps: number };
  timestamp: number;
}
export interface NodeMetricsEntry {
  node_id: string;
  name: string;
  hostname: string;
  status: string;
  gpu_name: string | null;
  // NVIDIA driver version (e.g. "535.183.01"), null on non-NVIDIA hosts.
  driver_version: string | null;
  // Short human-readable reason NVENC isn't advertised on this node, or null
  // if the node has NVENC working. Shown on the Monitor card as actionable
  // copy so the user knows what to fix (old driver, missing GPU, etc.).
  nvenc_unavailable_reason: string | null;
  os_info: string | null;
  current_job_id: number | null;
  capabilities: string[];
  metrics: NodeMetrics | null;
  age_seconds: number | null;
}
export const getNodeMetrics = () => apiFetch<{ nodes: NodeMetricsEntry[] }>("/nodes/metrics");
export const removeNode = (nodeId: string) => apiFetch(`/nodes/${nodeId}`, { method: "DELETE" });
export const cancelNodeJob = (nodeId: string) => apiFetch(`/nodes/${nodeId}/cancel`, { method: "POST" });
export const resetNode = (nodeId: string) => apiFetch(`/nodes/${nodeId}/reset`, { method: "POST" });
import type { NodeSettings } from "./types";
export const updateNodeSettings = (nodeId: string, settings: NodeSettings) =>
  apiFetch(`/nodes/${nodeId}/settings`, { method: "PATCH", body: JSON.stringify(settings) });
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
export interface TMDBSearchResult {
  tmdb_id: number;
  media_type: "movie" | "tv";
  title: string;
  year: string | null;
  poster_url: string | null;
  overview: string;
  rating: number | null;
}
export const searchTMDB = (query: string, year?: string) =>
  apiFetch<{ results: TMDBSearchResult[] }>("/posters/search", {
    method: "POST",
    body: JSON.stringify({ query, year }),
  });
export const overridePoster = (folderPath: string, tmdbId: number, mediaType: "movie" | "tv") =>
  apiFetch<{ title: string; year: string | null; poster_url: string | null; media_type: string; rating: number | null; genres: string | null; country: string | null; source: string }>(
    "/posters/override", {
      method: "POST",
      body: JSON.stringify({ folder_path: folderPath, tmdb_id: tmdbId, media_type: mediaType }),
    }
  );

// ── Rename ──────────────────────────────────────────────────────────────────
export interface RenameSettings {
  enabled_auto: boolean;
  rename_folders: boolean;
  movie_file_pattern: string;
  movie_folder_pattern: string;
  tv_file_pattern: string;
  tv_folder_pattern: string;
  season_folder_pattern: string;
  separator: "space" | "dot" | "dash" | "underscore";
  case_mode: "default" | "lower" | "upper";
  remove_illegal: boolean;
}
export interface RenameToken {
  token: string;
  example: string;
  desc: string;
}
export interface RenameTokenCategory {
  category: string;
  media_type: "movie" | "tv" | "both";
  tokens: RenameToken[];
}
export interface RenamePlan {
  old_path: string;
  new_path: string;
  old_folder?: string | null;
  new_folder?: string | null;
  old_season_folder?: string | null;
  new_season_folder?: string | null;
  reason: string;
  changed: boolean;
  error?: string;
}
export const getRenameSettings = () =>
  apiFetch<RenameSettings>("/rename/settings");
export const saveRenameSettings = (s: Partial<RenameSettings>) =>
  apiFetch<RenameSettings>("/rename/settings", { method: "PUT", body: JSON.stringify(s) });
export const getRenameTokens = () =>
  apiFetch<{ categories: RenameTokenCategory[] }>("/rename/tokens");
export const previewRename = (filePaths: string[], override?: Partial<RenameSettings>) =>
  apiFetch<{ plans: RenamePlan[] }>("/rename/preview", {
    method: "POST",
    body: JSON.stringify({ file_paths: filePaths, settings_override: override || null }),
  });
export const applyRename = (filePaths: string[], opts?: { rescan_arr?: boolean; rescan_plex?: boolean; override?: Partial<RenameSettings> }) =>
  apiFetch<{ results: any[]; rescans: any }>("/rename/apply", {
    method: "POST",
    body: JSON.stringify({
      file_paths: filePaths,
      settings_override: opts?.override || null,
      rescan_arr: opts?.rescan_arr ?? true,
      rescan_plex: opts?.rescan_plex ?? true,
    }),
  });
export const previewRenamePattern = (pattern: string, filePath?: string, settings?: Partial<RenameSettings>) =>
  apiFetch<{ rendered: string; metadata: any }>("/rename/preview-pattern", {
    method: "POST",
    body: JSON.stringify({ pattern, file_path: filePath, settings: settings || {} }),
  });
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

// *arr actions
export interface ResearchResult {
  success: boolean;
  service?: "sonarr" | "radarr";
  series?: string;
  movie?: string;
  episode_ids?: number[];
  movie_id?: number;
  blocklisted?: boolean;
  blocklist_error?: string | null;
  deleted?: boolean;
  searched?: boolean;
  error?: string;
}
export const researchFile = (filePath: string, deleteFile: boolean = true) =>
  apiFetch<ResearchResult>("/arr/research", {
    method: "POST",
    body: JSON.stringify({ file_path: filePath, delete_file: deleteFile }),
  });
export const researchFilesBulk = (filePaths: string[], deleteFile: boolean = true) =>
  apiFetch<{ total: number; succeeded: number; failed: number; results: (ResearchResult & { file_path: string })[] }>(
    "/arr/research/bulk",
    { method: "POST", body: JSON.stringify({ file_paths: filePaths, delete_file: deleteFile }) },
  );

// Unified *arr actions: replace | upgrade | missing
export type ArrAction = "replace" | "upgrade" | "missing";

export interface ArrActionSingleResult extends ResearchResult {
  action?: ArrAction;
}

export interface ArrMissingDetail {
  series_id: number;
  series_title: string;
  missing_count: number;
  searched: boolean;
  note?: string | null;
}

export interface ArrMissingResult {
  success: boolean;
  service?: "sonarr";
  action: "missing";
  series_searched?: number;
  series_resolved?: number;
  total_episode_ids?: number;
  skipped_movie?: number;
  skipped_unknown?: number;
  unresolved?: number;
  details?: ArrMissingDetail[];
  error?: string;
}

export interface ArrActionBulkResult {
  total: number;
  succeeded: number;
  failed: number;
  action: ArrAction;
  results: (ArrActionSingleResult & { file_path: string })[];
}

export const arrAction = (filePath: string, action: ArrAction, deleteFile: boolean = true) =>
  apiFetch<ArrActionSingleResult | ArrMissingResult>(
    "/arr/action",
    {
      method: "POST",
      body: JSON.stringify({ file_path: filePath, action, delete_file: deleteFile }),
    },
  );

export const arrActionBulk = (filePaths: string[], action: ArrAction, deleteFile: boolean = true) =>
  apiFetch<ArrActionBulkResult | ArrMissingResult>(
    "/arr/action/bulk",
    {
      method: "POST",
      body: JSON.stringify({ file_paths: filePaths, action, delete_file: deleteFile }),
    },
  );

// Plex Connect (PIN-based OAuth)
export interface PlexUser {
  email: string;
  username: string;
  title: string;
  thumb: string;
}
export interface PlexConnection {
  uri: string;
  address: string;
  port: number;
  local: boolean;
  relay: boolean;
  protocol: string;
  reachable?: boolean | null;
}
export interface PlexServer {
  name: string;
  client_identifier: string;
  owned: boolean;
  product_version: string;
  platform: string;
  connections: PlexConnection[];
  recommended_uri?: string;
}
export interface PlexAuthStatus {
  connected: boolean;
  server_url: string;
  server_name: string;
  user: PlexUser | null;
}

export const plexAuthStart = () =>
  apiFetch<{ pin_id: number; code: string; client_id: string; auth_url: string }>(
    "/plex/auth/start",
    { method: "POST" },
  );
export const plexAuthCheck = (pinId: number) =>
  apiFetch<{ token: string | null; expired: boolean; user?: PlexUser | null }>(
    "/plex/auth/check",
    { method: "POST", body: JSON.stringify({ pin_id: pinId }) },
  );
export const plexAuthResources = (token: string) =>
  apiFetch<{ servers: PlexServer[] }>(
    "/plex/auth/resources",
    { method: "POST", body: JSON.stringify({ token }) },
  );
export const plexAuthSave = (token: string, serverUrl: string, serverName?: string, serverClientId?: string) =>
  apiFetch<{ success: boolean }>(
    "/plex/auth/save",
    {
      method: "POST",
      body: JSON.stringify({
        token,
        server_url: serverUrl,
        server_name: serverName || "",
        server_client_id: serverClientId || "",
      }),
    },
  );
export const plexAuthDisconnect = () =>
  apiFetch<{ success: boolean }>("/plex/auth/disconnect", { method: "POST" });
export const plexAuthStatus = () =>
  apiFetch<PlexAuthStatus>("/plex/auth/status");

// Estimation & Export
export const estimateJobs = (filePaths: string[], overrideRules: boolean = false, overrides?: Record<string, any>) =>
  apiFetch<any>("/jobs/estimate", { method: "POST", body: JSON.stringify({ file_paths: filePaths, override_rules: overrideRules, ...overrides }) });
export const estimateJobsWithPriority = (filePaths: string[], priority: number) =>
  apiFetch<any>("/jobs/estimate", { method: "POST", body: JSON.stringify({ file_paths: filePaths, priority }) });
export const getVersion = () => apiFetch<{ current: string; latest: string | null; update_available: boolean }>("/stats/version");

export interface ChangelogEntry {
  version: string;
  date: string | null;
  intro: string;
  sections: Record<string, string[]>;
}
export const getChangelog = (limit = 0) =>
  apiFetch<{ current: string; entries: ChangelogEntry[] }>(
    `/stats/changelog${limit > 0 ? `?limit=${limit}` : ""}`,
  );
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
  apiFetch<{ status: string; job_id: number; message?: string; new_path?: string }>(`/jobs/${id}/retry`, { method: "POST" });
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
export const getConditionOptions = () =>
  apiFetch<{ sources: string[]; resolutions: string[]; video_codecs: string[]; audio_codecs: string[]; media_types: string[]; release_groups: string[]; arr_tags: { label: string; source: string }[] }>("/rules/condition-options");

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
export const testApiKey = (service: "tmdb" | "plex" | "jellyfin" | "sonarr" | "radarr") =>
  apiFetch<{ success: boolean; error: string | null; version?: string }>("/settings/test-api", {
    method: "POST", body: JSON.stringify({ service })
  });

// Jellyfin sync
export const syncJellyfinMetadata = () =>
  apiFetch<any>("/rules/sync-jellyfin", { method: "POST" });

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

// Backups
export const listBackups = () => apiFetch<{ name: string; size: number; created_at: string }[]>("/settings/backup/list");
export const createBackup = () => apiFetch<{ name: string; size: number; created_at: string }>("/settings/backup", { method: "POST" });
export const deleteBackup = (name: string) => apiFetch(`/settings/backup/${encodeURIComponent(name)}`, { method: "DELETE" });
export const downloadBackupUrl = (name: string) => `${API_BASE}/settings/backup/download/${encodeURIComponent(name)}`;
export async function restoreBackup(file: File) {
  const form = new FormData();
  form.append("file", file);
  const headers: Record<string, string> = {};
  const apiKey = getStoredApiKey();
  if (apiKey) headers["X-Api-Key"] = apiKey;
  const resp = await fetch(`${API_BASE}/settings/backup/restore`, { method: "POST", body: form, headers, credentials: "include" });
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}
